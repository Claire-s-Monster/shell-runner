"""Sandboxed subprocess executor for shell-runner.

Runs commands via /bin/bash with:
- cwd validation (must exist, must be a directory, symlinks resolved)
- env stripping (only allowlisted vars passed through)
- timeout enforcement
- output truncation with overflow files
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import threading
import time
import tomllib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .models import MAX_TIMEOUT_S

if TYPE_CHECKING:
    from .persistence import Persistence

OUTPUT_HEAD_BYTES = 4096
OUTPUT_TAIL_BYTES = 4096
DEFAULT_TIMEOUT_S = 30
# Window a timed-out child gets to run its own cleanup (e.g. lockfile removal)
# after SIGTERM before we escalate to SIGKILL. Module-level so tests can
# monkeypatch it to keep timeout tests fast.
TERMINATE_GRACE_S = 3.0

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


def resolve_cwd(cwd: str) -> tuple[Path | None, str | None]:
    """Resolve cwd once and validate it, returning (resolved, None) on success
    or (None, error_message) on failure.

    Callers MUST use the returned path rather than re-deriving it with their own
    realpath() call. Re-deriving opens a TOCTOU window: a symlink component
    swapped between validation and use would let the process run in a directory
    that was never validated. Resolving once and threading the validated value
    through is what closes it.
    """
    resolved = Path(os.path.realpath(cwd))  # noqa: PTH113
    with _cwd_roots_lock:
        current_roots = list(_CWD_ROOTS)
    if not any(resolved.is_relative_to(r) for r in current_roots):
        sorted_roots = sorted(str(r) for r in current_roots)
        return None, f"cwd escapes allowed root(s) {sorted_roots}: {cwd}"
    if not resolved.exists():
        return None, f"cwd does not exist: {cwd}"
    if not resolved.is_dir():
        return None, f"cwd is not a directory: {cwd}"
    return resolved, None


def validate_cwd(cwd: str) -> str | None:
    """Return a human-readable error message if cwd is unusable, else None.

    Shared by execute(), execute_background(), and the T3/T4 prompt-creation
    path in server.py so that a command whose cwd can never be honoured is
    rejected at classify/prompt time rather than after an approval token has
    already been spent on it (issue #36).

    This is the error-only view of resolve_cwd() for callers that only need
    the failure message and do not need the resolved path itself.
    """
    _, error = resolve_cwd(cwd)
    return error


def _signal_group(pid: int, sig: int) -> None:
    """Send sig to the process group of pid, falling back to the bare pid.

    Never signals shell-runner's own process group: if the child's pgid
    matches ours (e.g. start_new_session failed to take effect), falls back
    to signalling the bare child pid instead of killpg, to avoid the server
    SIGTERM/SIGKILL-ing itself. Swallows ProcessLookupError/PermissionError
    at every signal site since the process (or its group) may already have
    exited by the time we signal it.
    """
    try:
        pgid: int | None = os.getpgid(pid)
    except ProcessLookupError:
        pgid = None
    if pgid is not None and pgid == os.getpgid(0):
        logger.warning(
            "Child pid %d shares shell-runner's own process group (%d); "
            "signalling pid directly instead of killpg",
            pid,
            pgid,
        )
        pgid = None
    try:
        if pgid is not None:
            os.killpg(pgid, sig)
        else:
            os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _terminate_group_sync(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM the process group led by proc, then SIGKILL it if still alive
    after TERMINATE_GRACE_S.

    Gives a child (e.g. git) a window to run its own cleanup — such as
    removing a lockfile — before being force-killed. Used on the synchronous
    foreground execute() path. proc.wait(timeout=...) both detects exit and
    reaps the child; polling with os.kill(pid, 0) instead would report a
    reaped-but-not-yet-collected zombie as "still alive" for the whole grace
    window. Callers still call communicate() afterwards to drain any
    buffered pipe output.
    """
    _signal_group(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=TERMINATE_GRACE_S)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal_group(proc.pid, signal.SIGKILL)


async def _terminate_group_async(proc: asyncio.subprocess.Process) -> None:
    """Async counterpart of _terminate_group_sync for the background job path.

    SIGTERM the process group led by proc, then SIGKILL it if still alive
    after TERMINATE_GRACE_S. Callers must still await proc.wait() afterwards
    to finish reaping.
    """
    _signal_group(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=TERMINATE_GRACE_S)
        return
    except TimeoutError:
        pass
    _signal_group(proc.pid, signal.SIGKILL)


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
    resolved, cwd_error = resolve_cwd(cwd)
    if cwd_error is not None:
        return ExecutionResult(
            exit_code=-3,
            stdout="",
            stderr=cwd_error,
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

    try:
        proc = subprocess.Popen(  # noqa: S603
            ["/bin/bash", "-c", command],
            cwd=str(resolved),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except Exception as exc:  # noqa: BLE001
        exit_code = -2
        raw_stdout = b""
        raw_stderr = str(exc).encode()
    else:
        try:
            raw_stdout, raw_stderr = proc.communicate(timeout=min(timeout_s, MAX_TIMEOUT_S))
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = -1
            _terminate_group_sync(proc)
            raw_stdout, raw_stderr = proc.communicate()
            raw_stdout = raw_stdout or b""
            raw_stderr = raw_stderr or b""
        except Exception as exc:  # noqa: BLE001
            exit_code = -2
            raw_stdout = b""
            raw_stderr = str(exc).encode()
            _terminate_group_sync(proc)
            with contextlib.suppress(Exception):
                proc.communicate()
        except BaseException:
            _terminate_group_sync(proc)
            with contextlib.suppress(Exception):
                proc.communicate()
            raise

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


def _job_dir_base() -> Path:
    default = str(Path.home() / ".local/share/shell-runner/jobs")
    return Path(os.environ.get("SHELL_RUNNER_JOB_DIR", default))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def execute_background(
    *,
    command: str,
    cwd: str,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    telemetry_id: str,
    agent_id: str,
    persistence: Persistence,
    env_passthrough: list[str] | None = None,
) -> str:
    """Spawn command in background, persist a jobs row, return job_id immediately.

    The asyncio watcher task handles timeout and final status update.
    Foreground execution path is completely unchanged.
    """
    # Validate cwd — same logic as foreground execute()
    resolved, cwd_error = resolve_cwd(cwd)
    if cwd_error is not None:
        raise ValueError(cwd_error)

    job_id = str(uuid.uuid4())
    job_dir = _job_dir_base() / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    stdout_path = job_dir / "stdout.log"
    stderr_path = job_dir / "stderr.log"

    allowed = env_passthrough if env_passthrough is not None else DEFAULT_ENV_PASSTHROUGH
    env = {k: v for k, v in os.environ.items() if k in allowed}

    effective_timeout = min(timeout_s, MAX_TIMEOUT_S)

    stdout_file = stdout_path.open("wb")
    stderr_file = stderr_path.open("wb")

    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash",
            "-c",
            command,
            cwd=str(resolved),
            stdout=stdout_file,
            stderr=stderr_file,
            env=env,
            start_new_session=True,
        )
    except Exception:
        stdout_file.close()
        stderr_file.close()
        raise

    stdout_file.close()
    stderr_file.close()

    try:
        persistence.create_job(
            job_id=job_id,
            telemetry_id=telemetry_id,
            raw_cmd=command,
            cwd=cwd,
            agent_id=agent_id,
            timeout_s=timeout_s,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            pid=proc.pid,
        )
    except Exception:
        logger.exception(
            "Failed to persist job row for job %s; killing orphaned process (pid %d)",
            job_id,
            proc.pid,
        )
        await _terminate_group_async(proc)
        try:
            await proc.wait()
        except Exception:
            logger.exception("Error reaping killed process for job %s", job_id)
        raise

    async def _watch() -> None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=effective_timeout)
            rc = proc.returncode
            status = "completed" if rc == 0 else "failed"
            persistence.update_job_status(
                job_id,
                status=status,
                exit_code=rc,
                finished_at=_now_iso(),
            )
        except TimeoutError:
            await _terminate_group_async(proc)
            await proc.wait()
            persistence.update_job_status(
                job_id,
                status="timed_out",
                exit_code=proc.returncode,
                finished_at=_now_iso(),
            )
        except Exception:
            logger.exception("Unexpected error in background watcher for job %s", job_id)

    asyncio.create_task(_watch(), name=f"bg-watch-{job_id}")

    return job_id
