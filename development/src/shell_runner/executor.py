"""Sandboxed subprocess executor for shell-runner.

Runs commands via /bin/bash with:
- cwd validation (must exist, must be a directory, symlinks resolved)
- env stripping (only allowlisted vars passed through)
- timeout enforcement
- output truncation with overflow files
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

import tomllib

OUTPUT_HEAD_BYTES = 4096
OUTPUT_TAIL_BYTES = 4096
DEFAULT_TIMEOUT_S = 30
MAX_TIMEOUT_S = 600

DEFAULT_ENV_PASSTHROUGH = ["HOME", "USER", "PATH", "LANG", "TERM"]

_OVERFLOW_DIR = Path("/tmp/shell-runner-overflow")  # noqa: S108

logger = logging.getLogger(__name__)

_cwd_roots_lock = threading.Lock()
_CWD_ROOTS: list[Path] = []


def _load_cwd_roots() -> list[Path]:
    """Load the allowed cwd roots from all configured sources and return a deduplicated list.

    Precedence (union, later sources do not override — all are merged):
      1. TOML file at ${XDG_CONFIG_HOME:-$HOME/.config}/shell-runner/cwd-roots.toml
      2. SHELL_RUNNER_CWD_ROOTS env var (colon-separated)
      3. SHELL_RUNNER_CWD_ROOT env var (legacy single-path)
      4. Fallback: [cwd] when all three are empty
    """
    paths: list[Path] = []

    # 1. TOML config file
    xdg_config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    toml_path = Path(xdg_config) / "shell-runner" / "cwd-roots.toml"
    if toml_path.exists():
        try:
            with toml_path.open("rb") as fh:
                data = tomllib.load(fh)
            for raw in data.get("roots", []):
                paths.append(Path(raw).resolve())
        except Exception:  # noqa: BLE001
            logger.warning("Failed to parse cwd-roots TOML at %s; ignoring", toml_path)

    # 2. SHELL_RUNNER_CWD_ROOTS (colon-separated list)
    roots_env = os.environ.get("SHELL_RUNNER_CWD_ROOTS", "")
    if roots_env:
        for part in roots_env.split(":"):
            part = part.strip()
            if part:
                paths.append(Path(part).resolve())

    # 3. SHELL_RUNNER_CWD_ROOT (legacy single-path)
    root_env = os.environ.get("SHELL_RUNNER_CWD_ROOT", "")
    if root_env:
        paths.append(Path(root_env).resolve())

    # 4. Fallback to cwd when all sources are empty
    if not paths:
        paths.append(Path(os.getcwd()).resolve())

    # Deduplicate while preserving order
    seen: set[Path] = set()
    result: list[Path] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result


def reload_cwd_roots() -> list[Path]:
    """Reload allowed cwd roots from all configured sources.

    Thread-safe via module-level lock. Safe to call from a signal handler
    dispatched onto the asyncio event loop (not a raw OS signal handler).
    """
    global _CWD_ROOTS
    new_roots = _load_cwd_roots()
    with _cwd_roots_lock:
        _CWD_ROOTS = new_roots
    return new_roots


# Initialise on import
_CWD_ROOTS = _load_cwd_roots()


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int  # -1 timeout, -2 signal, -3 cwd_jail violation
    stdout: str
    stderr: str
    stdout_full_path: str | None
    stderr_full_path: str | None
    duration_ms: int
    timed_out: bool


def _truncate_output(
    raw: bytes,
    head: int,
    tail: int,
    overflow_path: Path | None,
) -> tuple[str, str | None]:
    if len(raw) <= head + tail:
        return raw.decode("utf-8", errors="replace"), None
    if overflow_path is not None:
        overflow_path.write_bytes(raw)
    head_str = raw[:head].decode("utf-8", errors="replace")
    tail_str = raw[-tail:].decode("utf-8", errors="replace")
    omitted = len(raw) - head - tail
    truncated = f"{head_str}\n...truncated {omitted} bytes...\n{tail_str}"
    return truncated, str(overflow_path) if overflow_path is not None else None


def execute(
    *,
    command: str,
    cwd: str,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    env_passthrough: list[str] | None = None,
    truncate_threshold: int = OUTPUT_HEAD_BYTES + OUTPUT_TAIL_BYTES,
    overflow_dir: str = str(_OVERFLOW_DIR),
) -> ExecutionResult:
    """Run command via /bin/bash with cwd jail, env stripping, timeout, output truncation."""
    import time

    # Validate cwd — resolve symlinks and enforce containment under any configured root.
    resolved = Path(os.path.realpath(cwd))  # noqa: PTH113
    with _cwd_roots_lock:
        current_roots = list(_CWD_ROOTS)
    if not any(resolved.is_relative_to(r) for r in current_roots):
        sorted_roots = sorted(str(r) for r in current_roots)
        return ExecutionResult(
            exit_code=-3,
            stdout="",
            stderr=f"cwd escapes allowed root(s) {sorted_roots}: {cwd}",
            stdout_full_path=None,
            stderr_full_path=None,
            duration_ms=0,
            timed_out=False,
        )
    if not resolved.exists():
        return ExecutionResult(
            exit_code=-3,
            stdout="",
            stderr=f"cwd does not exist: {cwd}",
            stdout_full_path=None,
            stderr_full_path=None,
            duration_ms=0,
            timed_out=False,
        )
    if not resolved.is_dir():
        return ExecutionResult(
            exit_code=-3,
            stdout="",
            stderr=f"cwd is not a directory: {cwd}",
            stdout_full_path=None,
            stderr_full_path=None,
            duration_ms=0,
            timed_out=False,
        )

    # Build restricted environment
    allowed = env_passthrough if env_passthrough is not None else DEFAULT_ENV_PASSTHROUGH
    env = {k: v for k, v in os.environ.items() if k in allowed}

    t0 = time.monotonic()
    timed_out = False
    proc = None

    try:
        proc = subprocess.run(  # noqa: S603
            ["/bin/bash", "-c", command],
            cwd=str(resolved),
            env=env,
            capture_output=True,
            timeout=min(timeout_s, MAX_TIMEOUT_S),
        )
        exit_code = proc.returncode
        raw_stdout = proc.stdout
        raw_stderr = proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = -1
        raw_stdout = exc.stdout or b""
        raw_stderr = exc.stderr or b""
    except Exception as exc:  # noqa: BLE001
        exit_code = -2
        raw_stdout = b""
        raw_stderr = str(exc).encode()

    duration_ms = int((time.monotonic() - t0) * 1000)

    # Determine overflow paths only if output exceeds threshold
    overflow = Path(overflow_dir)
    run_id = str(uuid.uuid4())

    needs_overflow_out = len(raw_stdout) > truncate_threshold
    needs_overflow_err = len(raw_stderr) > truncate_threshold

    if needs_overflow_out or needs_overflow_err:
        overflow.mkdir(parents=True, exist_ok=True)

    stdout_overflow = overflow / f"{run_id}.out" if needs_overflow_out else None
    stderr_overflow = overflow / f"{run_id}.err" if needs_overflow_err else None

    stdout, stdout_full_path = _truncate_output(
        raw_stdout, OUTPUT_HEAD_BYTES, OUTPUT_TAIL_BYTES, stdout_overflow
    )
    stderr, stderr_full_path = _truncate_output(
        raw_stderr, OUTPUT_HEAD_BYTES, OUTPUT_TAIL_BYTES, stderr_overflow
    )

    return ExecutionResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        stdout_full_path=stdout_full_path,
        stderr_full_path=stderr_full_path,
        duration_ms=duration_ms,
        timed_out=timed_out,
    )
