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

DEFAULT_INLINE_BUDGET_S = 20


def inline_budget_s() -> int:
    """Seconds a command may block the inline/foreground path before it is
    diverted to the background job path (issue #46). 0 disables diverting."""
    raw = os.environ.get("SHELL_RUNNER_INLINE_BUDGET_S", str(DEFAULT_INLINE_BUDGET_S))
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_INLINE_BUDGET_S


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


def _build_truncated(raw: bytes, head: int, tail: int) -> tuple[str, bool]:
    """Decode raw bytes to text, truncating with a marker if it exceeds
    head+tail. Returns (text, was_truncated). Pure — performs no I/O — so
    execute() (overflow file) and job_result() (existing job log path) can
    share identical marker text while differing in what they point
    stdout_full_path/stderr_full_path at.
    """
    if len(raw) <= head + tail:
        return raw.decode("utf-8", errors="replace"), False
    head_str = raw[:head].decode("utf-8", errors="replace")
    tail_str = raw[-tail:].decode("utf-8", errors="replace")
    omitted = len(raw) - head - tail
    truncated = f"{head_str}\n...truncated {omitted} bytes...\n{tail_str}"
    return truncated, True


def _truncate_output(
    raw: bytes,
    head: int,
    tail: int,
    overflow_path: Path | None,
) -> tuple[str, str | None]:
    text, was_truncated = _build_truncated(raw, head, tail)
    if not was_truncated:
        return text, None
    if overflow_path is not None:
        overflow_path.write_bytes(raw)
    return text, str(overflow_path) if overflow_path is not None else None


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
            logger.info(
                "Inline command hit its executor timeout (%ss); "
                "terminating SIGTERM->grace->SIGKILL",
                min(timeout_s, MAX_TIMEOUT_S),
            )
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


_job_completion: dict[str, asyncio.Event] = {}


def _job_is_terminal(persistence: Persistence, job_id: str) -> bool:
    """True if the persisted job status is anything other than 'running'.

    Used by _watch() to avoid clobbering a terminal status (e.g. 'killed',
    set concurrently by shell_kill) with its own 'completed'/'failed'/
    'timed_out' outcome. Any exception reading the row is treated as
    non-terminal so the watcher still records its own outcome rather than
    getting stuck.
    """
    try:
        row = persistence.get_job(job_id)
    except Exception:
        logger.exception(
            "Failed to read job %s status before update; proceeding with watcher's own status",
            job_id,
        )
        return False
    return row is not None and row.get("status") != "running"


async def execute_background(
    *,
    command: str,
    cwd: str,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    telemetry_id: str,
    agent_id: str,
    persistence: Persistence,
    env_passthrough: list[str] | None = None,
    track_completion: bool = False,
) -> str:
    """Spawn command in background, persist a jobs row, return job_id immediately.

    The asyncio watcher task handles timeout and final status update.
    Foreground execution path is completely unchanged.

    If track_completion is True, an asyncio.Event is registered for job_id
    before the watcher task is scheduled, so a caller (e.g. the inline path
    diverting per issue #46) can await wait_for_job(job_id, ...) for it.
    Callers that pass track_completion=True MUST call discard_job_tracking()
    in a finally block to keep the tracking dict bounded.
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

    # Captured before spawn so _watch() can compute a real duration_ms when it
    # reconciles the shell_calls row (issue #49).
    spawn_started_at = time.monotonic()

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

    if track_completion:
        _job_completion[job_id] = asyncio.Event()

    async def _watch() -> None:
        try:
            try:
                await asyncio.wait_for(proc.wait(), timeout=effective_timeout)
                rc = proc.returncode
                status = "completed" if rc == 0 else "failed"
                logger.debug(
                    "Background job %s exited on its own (exit_code=%s)", job_id, rc
                )
                if _job_is_terminal(persistence, job_id):
                    return
                persistence.update_job_status(
                    job_id,
                    status=status,
                    exit_code=rc,
                    finished_at=_now_iso(),
                )
            except TimeoutError:
                logger.info(
                    "Background job %s hit its executor timeout (%ss); "
                    "terminating SIGTERM->grace->SIGKILL",
                    job_id,
                    effective_timeout,
                )
                await _terminate_group_async(proc)
                await proc.wait()
                if _job_is_terminal(persistence, job_id):
                    return
                persistence.update_job_status(
                    job_id,
                    status="timed_out",
                    exit_code=proc.returncode,
                    finished_at=_now_iso(),
                )
            except Exception:
                logger.exception("Unexpected error in background watcher for job %s", job_id)
        finally:
            # Reconcile the shell_calls row pre-written as decision="running"
            # (issue #49): every dispatch through execute_background() writes
            # that row up front — jobs.telemetry_id has an immediate FK on
            # shell_calls(id) and Persistence opens every connection with
            # PRAGMA foreign_keys=ON — but nothing used to update it once the
            # job actually finished, so a completed job stayed recorded as
            # "running" forever, corrupting health_stats() denominators and
            # issue #41's decision-aware retention.
            #
            # This MUST run before event.set() below. For a job that finishes
            # within the inline budget, server.py's own unconditional
            # finalize_call runs (via _run_with_inline_budget) only after
            # wait_for_job() observes event.set() — so reconciling here first
            # means the watcher's write always lands before the server's, and
            # only_if_running=True makes the watcher's write a safe no-op in
            # that case: the server's unconditional write deterministically
            # wins. Reversing this ordering would let the watcher's write win
            # instead, silently dropping the server's more specific
            # decision_path (e.g. "inline_budget_divert").
            try:
                job_row = persistence.get_job(job_id)
                if job_row is not None:
                    duration_ms = int((time.monotonic() - spawn_started_at) * 1000)
                    persistence.finalize_call(
                        telemetry_id,
                        decision="executed",  # the command did run, even if timed_out/killed
                        exit_code=job_row.get("exit_code"),
                        stdout_bytes=_log_size_bytes(job_row.get("stdout_path")),
                        stderr_bytes=_log_size_bytes(job_row.get("stderr_path")),
                        duration_ms=duration_ms,
                        decision_path=None,  # preserve the prewritten path
                        only_if_running=True,
                    )
            except Exception:
                logger.exception(
                    "Failed to reconcile shell_calls row for job %s (telemetry_id=%s)",
                    job_id,
                    telemetry_id,
                )
            event = _job_completion.get(job_id)
            if event is not None:
                event.set()

    asyncio.create_task(_watch(), name=f"bg-watch-{job_id}")

    return job_id


async def wait_for_job(job_id: str, timeout_s: float) -> bool:
    """Await terminal state for a job registered via execute_background(...,
    track_completion=True).

    Returns True if the job reached a terminal state within timeout_s, False
    on timeout or if job_id was never tracked (issue #46: lets the inline
    execute() path divert a slow command to the background job path and
    still get a fast result if it finishes quickly after all).
    """
    event = _job_completion.get(job_id)
    if event is None:
        return False
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout_s)
    except TimeoutError:
        return False
    return True


def discard_job_tracking(job_id: str) -> None:
    """Remove job_id's completion Event, if any.

    Callers that pass track_completion=True to execute_background() MUST
    call this in a finally block so _job_completion stays bounded.
    """
    _job_completion.pop(job_id, None)


def _log_size_bytes(path: str | None) -> int:
    """Best-effort byte count of a job log file via stat(), 0 on OSError.

    Deliberately does NOT read the file into memory the way _read_log_bytes()
    does for the inline job_result() path — a background job may have run
    for an hour and produced a very large log, so reconciling shell_calls
    (see _watch()) must stay O(1) in log size. st_size is also the more
    accurate figure for the output_bytes_stdout/output_bytes_stderr columns:
    it is the bytes actually produced, whereas the inline path records
    len(exec_result.stdout), the length of the (possibly truncated) decoded
    string.
    """
    if not path:
        return 0
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def _read_log_bytes(path: str | None) -> bytes:
    """Best-effort read of a job log file.

    Missing/unreadable files are treated as empty rather than raising, since
    job_result() must not fail just because a log file was cleaned up or
    never flushed.
    """
    if not path:
        return b""
    try:
        return Path(path).read_bytes()
    except OSError:
        return b""


def job_result(*, job_id: str, persistence: Persistence) -> ExecutionResult | None:
    """Build an ExecutionResult for a finished background job.

    Reads the jobs row and the persisted stdout/stderr log files, applying
    the exact same truncation rule and marker text as execute() (via
    _build_truncated) so callers cannot tell inline and diverted-background
    results apart by their output formatting.

    Returns None if the job row does not exist or is still running.
    stdout_full_path/stderr_full_path point at the existing job log file
    (never a fresh overflow file — the log already holds the full output)
    and are set only when that stream was actually truncated, matching
    execute()'s contract. duration_ms is always 0; the server computes its
    own from job timestamps.
    """
    row = persistence.get_job(job_id)
    if row is None or row.get("status") == "running":
        return None

    raw_stdout = _read_log_bytes(row.get("stdout_path"))
    raw_stderr = _read_log_bytes(row.get("stderr_path"))

    stdout, stdout_truncated = _build_truncated(raw_stdout, OUTPUT_HEAD_BYTES, OUTPUT_TAIL_BYTES)
    stderr, stderr_truncated = _build_truncated(raw_stderr, OUTPUT_HEAD_BYTES, OUTPUT_TAIL_BYTES)

    exit_code = row.get("exit_code")

    return ExecutionResult(
        exit_code=exit_code if exit_code is not None else -1,
        stdout=stdout,
        stderr=stderr,
        stdout_full_path=row.get("stdout_path") if stdout_truncated else None,
        stderr_full_path=row.get("stderr_path") if stderr_truncated else None,
        duration_ms=0,
        timed_out=row.get("status") == "timed_out",
    )
