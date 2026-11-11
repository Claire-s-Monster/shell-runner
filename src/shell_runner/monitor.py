"""shell_monitor (issue #68): watch a systemd user unit and/or follow a log file.

A monitor is a background job with no subprocess: an asyncio task (kept in
`_tasks` so it is neither garbage collected nor out of reach of shell_kill)
that appends one JSON object per line to events.jsonl. The job row's
stdout_path points at that file, so shell_status / shell_wait work unchanged.

The terminal event is ALWAYS the last line written, and the job row never stays
'running' once the task ends (finish, timeout, cancel or unexpected exception).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import stat
import time
import tomllib
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from . import executor, restart
from .models import ShellMonitorRequest, ShellMonitorResponse
from .persistence import Persistence

logger = logging.getLogger(__name__)

UNIT_POLL_INTERVAL_S = 1.0
LOG_POLL_INTERVAL_S = 0.5
SYSTEMCTL_TIMEOUT_S = 10.0
MAX_SYSTEMCTL_ERRORS = 3
CANCEL_WAIT_S = 5.0

MAX_UNIT_LEN = 256
MAX_PATTERNS = 20
MAX_PATTERN_LEN = 500
MAX_LINE_CHARS = 2000
MAX_IDLE_S = 21600.0
READ_CHUNK = 1 << 20
MAX_READS_PER_TICK = 4  # ~4 MiB per tick
MAX_PARTIAL_BYTES = 1 << 16
MAX_EVENTS_READ = 200

_UNIT_RE = re.compile(r"[A-Za-z0-9@._:-]+")
_UNIT_SUFFIXES = (".service", ".scope", ".timer", ".socket", ".target", ".path")
_ACTIVE_STATES = frozenset({"active", "activating", "deactivating", "reloading"})
_SHOW_PROPS = "LoadState,ActiveState,SubState,Result,ExecMainStatus,ExecMainCode"

BUILTIN_PRESETS: dict[str, list[str]] = {
    "errors": [r"(?i)\b(error|fatal|traceback|exception|failed)\b"],
    "pytest": [r"^=+ .*(passed|failed|error).* =+$"],
}

# Credential directories (as path components) and system trees that are never
# readable through shell_monitor, even when they sit inside an allowed root.
_DENY_COMPONENTS = frozenset({".ssh", ".gnupg", ".aws", ".kube", ".docker", ".password-store"})
_DENY_SEQUENCES = ((".config", "gh"),)
_DENY_FILE_NAMES = frozenset(
    {
        ".credentials.json",
        ".netrc",
        ".pgpass",
        ".git-credentials",
        ".npmrc",
        ".pypirc",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "id_dsa",
    }
)
_DENY_FILE_PREFIXES = (".env",)
_DENY_FILE_SUFFIXES = (".pem", ".key")
_DENY_ROOTS = tuple(Path(p) for p in ("/etc", "/proc", "/sys", "/dev", "/root", "/boot"))

TerminalState = Literal["completed", "failed", "timed_out", "killed", "unit_missing"]

# job_id -> running monitor task. Strong references (the loop only keeps weak ones).
_tasks: dict[str, asyncio.Task[None]] = {}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Configuration, presets, log_path allowlist
# ---------------------------------------------------------------------------


def load_config() -> dict[str, Any]:
    """Read ${XDG_CONFIG_HOME:-~/.config}/shell-runner/monitor.toml (reloaded on every call)."""
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    path = Path(xdg) / "shell-runner" / "monitor.toml"
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        logger.warning("Failed to parse monitor TOML at %s; ignoring", path)
        return {}


def preset_table(config: dict[str, Any]) -> dict[str, list[str]]:
    """Built-in presets overlaid with `[presets.<name>] patterns = [...]` from monitor.toml."""
    table = {name: list(pats) for name, pats in BUILTIN_PRESETS.items()}
    user = config.get("presets")
    if isinstance(user, dict):
        for name, body in user.items():
            pats = body.get("patterns") if isinstance(body, dict) else None
            if isinstance(pats, list) and all(isinstance(p, str) for p in pats):
                table[str(name)] = list(pats)
    return table


def allowed_roots(config: dict[str, Any]) -> list[Path]:
    """cwd roots, /tmp, SHELL_RUNNER_MONITOR_LOG_DIRS and monitor.toml `log_dirs`."""
    raw: list[str] = [str(r) for r in executor.current_cwd_roots()]
    raw.append("/tmp")  # noqa: S108  # nosec B108 - an allowlist root, not a temp file
    raw.extend(
        p for p in os.environ.get("SHELL_RUNNER_MONITOR_LOG_DIRS", "").split(os.pathsep) if p
    )
    toml_dirs = config.get("log_dirs")
    if isinstance(toml_dirs, list):
        raw.extend(os.path.expanduser(p) for p in toml_dirs if isinstance(p, str) and p)
    roots: list[Path] = []
    for p in raw:
        resolved = Path(os.path.realpath(p))
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _is_denied(resolved: Path) -> bool:
    if any(resolved.is_relative_to(root) for root in _DENY_ROOTS):
        return True
    return is_credential_path(resolved)


def is_credential_path(resolved: Path) -> bool:
    """True if the (already resolved) path names or sits inside a credential store.

    Shared with the normalizer, which uses it to keep credential paths out of the
    auto-execute rules. System trees (_DENY_ROOTS) are NOT included here.
    """
    name = resolved.name
    if (
        name in _DENY_FILE_NAMES
        or name.startswith(_DENY_FILE_PREFIXES)
        or name.endswith(_DENY_FILE_SUFFIXES)
    ):
        return True
    parts = resolved.parts
    if any(part in _DENY_COMPONENTS for part in parts):
        return True
    return any(
        parts[i : i + len(seq)] == seq
        for seq in _DENY_SEQUENCES
        for i in range(len(parts) - len(seq) + 1)
    )


def check_log_path(raw: str, config: dict[str, Any]) -> tuple[Path | None, str | None]:
    """Resolve *raw* and apply the allowlist; return (resolved, None) or (None, error).

    The deny-list wins even inside an allowed root. This is called again whenever
    the file is (re)opened, so a symlink created after submission cannot escape.
    """
    if not os.path.isabs(raw):
        return None, f"log_path must be absolute: {raw}"
    resolved = Path(os.path.realpath(raw))
    roots = allowed_roots(config)
    if _is_denied(resolved) or not any(resolved.is_relative_to(r) for r in roots):
        return None, f"log_path not allowed: {raw}; allowed roots: {sorted(str(r) for r in roots)}"
    return resolved, None


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


class _SpecError(Exception):
    def __init__(self, message: str, allowed: list[str] | None = None) -> None:
        super().__init__(message)
        self.allowed = allowed or []


@dataclass(frozen=True)
class MonitorSpec:
    unit: str | None
    log_raw: str | None  # as submitted; re-resolved on every poll so rotation via symlink works
    log_path: Path | None  # resolved at submission, for display
    patterns: list[tuple[str, re.Pattern[str]]]
    until: str
    until_kind: Literal["unit_exit", "pattern", "log_idle"]
    until_re: re.Pattern[str] | None
    idle_s: float
    from_start: bool
    timeout_s: int
    max_events: int
    unit_grace_s: int


def _normalise_unit(unit: str) -> str:
    if len(unit) > MAX_UNIT_LEN or unit.startswith("-") or not _UNIT_RE.fullmatch(unit):
        raise _SpecError(
            f"invalid unit name (allowed [A-Za-z0-9@._:-], max {MAX_UNIT_LEN}): {unit!r}"
        )
    return unit if unit.endswith(_UNIT_SUFFIXES) else f"{unit}.service"


def _compile(pattern: str, where: str) -> re.Pattern[str]:
    if not pattern or len(pattern) > MAX_PATTERN_LEN:
        raise _SpecError(f"{where}: regex must be 1-{MAX_PATTERN_LEN} chars")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise _SpecError(f"{where}: invalid regex: {exc}") from exc


def _compile_patterns(
    req: ShellMonitorRequest, config: dict[str, Any]
) -> list[tuple[str, re.Pattern[str]]]:
    if len(req.patterns) > MAX_PATTERNS:
        raise _SpecError(f"patterns exceeds {MAX_PATTERNS} items")
    sources = list(req.patterns)
    if req.preset is not None:
        table = preset_table(config)
        if req.preset not in table:
            raise _SpecError(f"unknown preset {req.preset!r}; available: {sorted(table)}")
        sources.extend(table[req.preset])
    return [(src, _compile(src, f"patterns[{i}]")) for i, src in enumerate(sources)]


def _parse_until(
    until: str | None, *, has_unit: bool, has_log: bool
) -> tuple[str, Literal["unit_exit", "pattern", "log_idle"], re.Pattern[str] | None, float]:
    until = until or ("unit_exit" if has_unit else None)
    if until is None:
        raise _SpecError("until is required when no unit is given")
    if until == "unit_exit":
        if not has_unit:
            raise _SpecError("until=unit_exit requires a unit")
        return until, "unit_exit", None, 0.0
    kind, _, arg = until.partition(":")
    if kind not in ("pattern", "log_idle"):
        raise _SpecError(f"invalid until {until!r}: use unit_exit, pattern:<regex> or log_idle:<s>")
    if not has_log:
        raise _SpecError(f"until={kind} requires log_path")
    if kind == "pattern":
        return until, "pattern", _compile(arg, "until"), 0.0
    try:
        idle_s = float(arg)
    except ValueError:
        idle_s = 0.0
    if not 0 < idle_s <= MAX_IDLE_S:
        raise _SpecError(f"log_idle needs seconds in (0, {MAX_IDLE_S:g}]")
    return until, "log_idle", None, idle_s


def _build_spec(req: ShellMonitorRequest, config: dict[str, Any]) -> MonitorSpec:
    if req.unit is None and req.log_path is None:
        raise _SpecError("at least one of unit or log_path is required")
    unit = _normalise_unit(req.unit) if req.unit is not None else None
    patterns = _compile_patterns(req, config)
    until, kind, until_re, idle_s = _parse_until(
        req.until, has_unit=unit is not None, has_log=req.log_path is not None
    )
    resolved: Path | None = None
    if req.log_path is not None:
        resolved, err = check_log_path(req.log_path, config)
        if err is not None:
            raise _SpecError(err, [str(r) for r in allowed_roots(config)])
    return MonitorSpec(
        unit=unit,
        log_raw=req.log_path,
        log_path=resolved,
        patterns=patterns,
        until=until,
        until_kind=kind,
        until_re=until_re,
        idle_s=idle_s,
        from_start=req.from_start,
        timeout_s=req.timeout_s,
        max_events=req.max_events,
        unit_grace_s=req.unit_grace_s,
    )


# ---------------------------------------------------------------------------
# The watcher
# ---------------------------------------------------------------------------


@dataclass
class _Terminal:
    state: TerminalState
    reason: str
    exit_code: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class _LogRejectedError(Exception):
    """The log path (re-checked when the file is opened) is no longer allowed."""


class _ShowResult(Protocol):
    """The slice of subprocess.CompletedProcess used here (avoids importing subprocess)."""

    returncode: int
    stdout: str
    stderr: str


def _systemctl_show(unit: str) -> _ShowResult:
    return restart.run_argv(
        ["systemctl", "--user", "show", "-p", _SHOW_PROPS, unit],
        cwd=None,
        env=restart.systemd_env(),
        timeout=SYSTEMCTL_TIMEOUT_S,
    )


def _parse_show(stdout: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props[key] = value
    return props


def _unit_exit_terminal(props: dict[str, str]) -> _Terminal:
    result = props.get("Result", "")
    status_raw = props.get("ExecMainStatus", "")
    status = int(status_raw) if status_raw.isdigit() else None
    extra: dict[str, Any] = {"result": result}
    if status is not None:
        extra["exec_main_status"] = status
    code = props.get("ExecMainCode", "")
    if code.isdigit():
        extra["exec_main_code"] = int(code)
    if result == "success" and status in (None, 0):
        return _Terminal("completed", "unit_exited", status, extra)
    return _Terminal("failed", "unit_failed", status, extra)


class _Monitor:
    def __init__(self, spec: MonitorSpec, config: dict[str, Any], events_path: Path) -> None:
        self.spec = spec
        self._config = config
        self._events = events_path.open("a", encoding="utf-8")
        self._clock = time.monotonic
        self._line_events = 0
        self._truncated = False
        # unit state
        self._seen_active = False
        self._last_unit_state: tuple[str, str] | None = None
        self._systemctl_errors = 0
        # log state
        self._fh: io.BufferedReader | None = None
        self._ident: tuple[int, int] | None = None
        self._pos = 0
        self._partial = b""
        self._last_growth = self._clock()
        self._first_open = True
        self._start_offset = self._initial_offset()

    def _initial_offset(self) -> int:
        """Where to begin reading a log that already exists at submission (its end,
        unless from_start); a file that appears later is always read from byte 0."""
        if self.spec.log_raw is None or self.spec.from_start:
            return 0
        try:
            return os.stat(self.spec.log_raw).st_size
        except OSError:
            return 0

    # -- events -----------------------------------------------------------

    def _emit(self, kind: str, **fields: Any) -> None:
        record = {"ts": _now_iso(), "kind": kind, **fields}
        self._events.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._events.flush()

    def _emit_line(self, text: str, pattern: str) -> None:
        if self._line_events >= self.spec.max_events:
            if not self._truncated:
                self._truncated = True
                self._emit(
                    "note",
                    text=f"max_events={self.spec.max_events} reached; further line events "
                    "are truncated (still watching for the terminal condition)",
                )
            return
        self._line_events += 1
        self._emit("line", source="log", text=text, pattern=pattern)

    def emit_terminal(self, terminal: _Terminal) -> None:
        self._emit("terminal", state=terminal.state, reason=terminal.reason, **terminal.extra)

    def close(self) -> None:
        self._close_log()
        self._events.close()

    # -- log following ----------------------------------------------------

    def _close_log(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def _handle_line(self, raw: bytes) -> bool:
        """Emit a line event if *raw* matches; return True if it matched the until pattern."""
        text = raw.decode("utf-8", errors="replace").rstrip("\r")[:MAX_LINE_CHARS]
        hit = next((src for src, rx in self.spec.patterns if rx.search(text)), None)
        until_hit = self.spec.until_re is not None and self.spec.until_re.search(text) is not None
        if hit is None and until_hit:
            hit = self.spec.until[len("pattern:") :]
        if hit is not None:
            self._emit_line(text, hit)
        return until_hit

    def _open_log(self, real: str) -> None:
        resolved, err = check_log_path(real, self._config)
        if err is not None or resolved is None:
            raise _LogRejectedError(err or "log_path not allowed")
        try:
            fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return  # vanished between stat and open; retry next tick
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            raise _LogRejectedError(f"log_path is not a regular file: {real}")
        fh = os.fdopen(fd, "rb")
        self._pos = self._start_offset if self._first_open else 0
        fh.seek(self._pos)
        self._first_open = False
        self._fh = fh
        self._ident = (st.st_dev, st.st_ino)
        self._partial = b""
        self._last_growth = self._clock()

    def _drain_log(self) -> bool:
        """Read newly appended log bytes; True if a line matched the until pattern."""
        assert self.spec.log_raw is not None
        try:
            real = os.path.realpath(self.spec.log_raw)
            st = os.stat(real)
        except OSError:
            return False  # not there (yet, or momentarily during rotation)
        if self._fh is not None and (
            (st.st_dev, st.st_ino) != self._ident or st.st_size < self._pos
        ):
            self._close_log()
            self._emit("note", text="log rotated or truncated; reopening from the start")
        if self._fh is None:
            self._open_log(real)
        return self._read_new()

    def _read_new(self) -> bool:
        matched = False
        for _ in range(MAX_READS_PER_TICK):
            if self._fh is None:
                break
            data = self._fh.read(READ_CHUNK)
            if not data:
                break
            self._pos += len(data)
            self._last_growth = self._clock()
            *lines, self._partial = (self._partial + data).split(b"\n")
            if len(self._partial) > MAX_PARTIAL_BYTES:
                lines.append(self._partial)
                self._partial = b""
            for raw in lines:
                matched = self._handle_line(raw) or matched
            if matched:
                break
        return matched

    # -- unit polling -----------------------------------------------------

    async def _poll_unit(self, elapsed: float) -> _Terminal | None:
        assert self.spec.unit is not None
        cp = await asyncio.to_thread(_systemctl_show, self.spec.unit)
        if cp.returncode != 0:
            return self._systemctl_error(cp)
        self._systemctl_errors = 0
        props = _parse_show(cp.stdout or "")
        if props.get("LoadState") == "not-found":
            return self._unit_not_found(elapsed)
        active, sub = props.get("ActiveState", ""), props.get("SubState", "")
        if (active, sub) != self._last_unit_state:
            self._last_unit_state = (active, sub)
            self._emit(
                "unit", load_state=props.get("LoadState", ""), active_state=active, sub_state=sub
            )
        if active in _ACTIVE_STATES:
            self._seen_active = True
        elif active in ("inactive", "failed"):
            if self._seen_active:
                return _unit_exit_terminal(props)
            # Never seen running: it may simply not have been started yet, or carry a
            # stale Result from an earlier run. Give it the grace period to start.
            if elapsed >= self.spec.unit_grace_s:
                term = _unit_exit_terminal(props)
                term.reason = "unit_inactive_never_seen_active"
                return term
        return None

    def _systemctl_error(self, cp: _ShowResult) -> _Terminal | None:
        self._systemctl_errors += 1
        detail = (cp.stderr or "").strip()[:200]
        self._emit("note", text=f"systemctl show failed (rc={cp.returncode}): {detail}")
        if self._systemctl_errors >= MAX_SYSTEMCTL_ERRORS:
            return _Terminal("failed", "systemctl_error")
        return None

    def _unit_not_found(self, elapsed: float) -> _Terminal | None:
        if self._seen_active:
            # A transient unit (systemd-run) is garbage collected after it exits.
            # systemd keeps *failed* transient units unless --collect is used, so a
            # unit that was seen running and then vanished is reported as a success.
            return _Terminal("completed", "unit_collected_after_exit")
        if elapsed >= self.spec.unit_grace_s:
            return _Terminal("unit_missing", "unit_not_found")
        return None

    async def _drain_async(self) -> bool:
        """Run _drain_log off the event loop (file reads + regex matching can be large).

        Only this monitor's single task ever touches its state, and it awaits each
        drain, so there is no concurrent access. On cancellation the in-flight read
        is allowed to finish so the terminal event is still the last one written."""
        fut = asyncio.ensure_future(asyncio.to_thread(self._drain_log))
        try:
            return await asyncio.shield(fut)
        except asyncio.CancelledError:
            await asyncio.wait({fut})
            raise

    async def _conclude_unit(self, term: _Terminal) -> _Terminal:
        if self.spec.log_raw is not None and await self._drain_async():
            return _Terminal("completed", "pattern_matched")
        if self.spec.until_kind == "pattern" and term.state == "completed":
            # Silence must never mean success: the unit ended without the awaited pattern.
            return _Terminal("failed", "unit_exited_before_pattern", term.exit_code, term.extra)
        return term

    # -- main loop --------------------------------------------------------

    async def run(self) -> _Terminal:
        try:
            return await self._loop()
        except _LogRejectedError as exc:
            self._emit("note", text=str(exc))
            return _Terminal("failed", "log_path_not_allowed")

    async def _loop(self) -> _Terminal:
        spec = self.spec
        started = self._clock()
        deadline = started + spec.timeout_s
        next_unit = started
        interval = LOG_POLL_INTERVAL_S if spec.log_raw is not None else UNIT_POLL_INTERVAL_S
        while True:
            now = self._clock()
            if spec.log_raw is not None and await self._drain_async():
                return _Terminal("completed", "pattern_matched")
            if spec.unit is not None and now >= next_unit:
                next_unit = now + UNIT_POLL_INTERVAL_S
                unit_term = await self._poll_unit(now - started)
                if unit_term is not None:
                    return await self._conclude_unit(unit_term)
            now = self._clock()
            if (
                spec.until_kind == "log_idle"
                and self._fh is not None
                and now - self._last_growth >= spec.idle_s
            ):
                return _Terminal("completed", "log_idle")
            if now >= deadline:
                return _Terminal("timed_out", "timeout")
            await asyncio.sleep(max(0.0, min(interval, deadline - now)))


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


async def _finalize(
    db: Persistence,
    *,
    job_id: str,
    telemetry_id: str,
    mon: _Monitor,
    terminal: _Terminal,
    paths: tuple[Path, Path],
    started: float,
) -> None:
    """Write the terminal event, then flip the job row and reconcile shell_calls."""
    try:
        mon.emit_terminal(terminal)
    except Exception:
        logger.exception("Failed to write terminal event for monitor %s", job_id)
    finally:
        mon.close()
    try:
        await asyncio.to_thread(
            db.update_job_status,
            job_id,
            status=terminal.state,
            exit_code=terminal.exit_code,
            finished_at=_now_iso(),
            only_if_running=True,
        )
    except Exception:
        logger.exception("Failed to update job status for monitor %s", job_id)
    try:
        await asyncio.to_thread(
            db.finalize_call,
            telemetry_id,
            decision="executed",
            exit_code=terminal.exit_code,
            stdout_bytes=_file_size(paths[0]),
            stderr_bytes=_file_size(paths[1]),
            duration_ms=int((time.monotonic() - started) * 1000),
            decision_path=None,
            only_if_running=True,
        )
    except Exception:
        logger.exception("Failed to reconcile shell_calls row for monitor %s", job_id)


async def _supervise(
    db: Persistence,
    *,
    job_id: str,
    telemetry_id: str,
    mon: _Monitor,
    paths: tuple[Path, Path],
) -> None:
    started = time.monotonic()
    try:
        terminal = await mon.run()
    except asyncio.CancelledError:
        terminal = _Terminal("killed", "killed")
    except Exception as exc:
        logger.exception("Unexpected error in monitor %s", job_id)
        with paths[1].open("a", encoding="utf-8") as diag:
            diag.write(traceback.format_exc())
        terminal = _Terminal("failed", f"monitor_error: {type(exc).__name__}")
    await _finalize(
        db,
        job_id=job_id,
        telemetry_id=telemetry_id,
        mon=mon,
        terminal=terminal,
        paths=paths,
        started=started,
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def _canonical_cmd(spec: MonitorSpec) -> str:
    return f"shell_monitor unit={spec.unit or '-'} log={spec.log_raw or '-'} until={spec.until}"


async def start_monitor(req: ShellMonitorRequest, db: Persistence) -> ShellMonitorResponse:
    """Validate *req*, create the job and start the watcher task."""
    config = load_config()
    try:
        spec = _build_spec(req, config)
    except _SpecError as exc:
        return ShellMonitorResponse(status="error", error=str(exc), allowed_roots=exc.allowed)

    job_id = str(uuid.uuid4())
    telemetry_id = str(uuid.uuid4())
    job_dir = executor.job_dir_base() / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    events_path = job_dir / "events.jsonl"
    diag_path = job_dir / "diag.log"
    diag_path.touch()
    cmd = _canonical_cmd(spec)
    cwd = str(spec.log_path.parent) if spec.log_path is not None else str(Path.home())

    await asyncio.to_thread(
        db.record_call,
        call_id=telemetry_id,
        agent_id=req.agent_id,
        cwd=cwd,
        raw_cmd=cmd,
        normalized_template="shell_monitor",
        command_tier=1,
        final_tier=1,
        decision="running",  # reconciled to "executed" by _finalize, like background jobs
        matched_rule_pattern=None,
        matched_rule_category=None,
        exit_code=None,
        stdout_bytes=0,
        stderr_bytes=0,
        duration_ms=0,
        decision_path=["shell_monitor"],
        normalizer_warnings=[],
    )
    await asyncio.to_thread(
        db.create_job,
        job_id=job_id,
        telemetry_id=telemetry_id,
        raw_cmd=cmd,
        cwd=cwd,
        agent_id=req.agent_id,
        timeout_s=spec.timeout_s,
        stdout_path=str(events_path),
        stderr_path=str(diag_path),
        pid=None,
        kind="monitor",
    )

    mon = _Monitor(spec, config, events_path)
    task = asyncio.create_task(
        _supervise(
            db, job_id=job_id, telemetry_id=telemetry_id, mon=mon, paths=(events_path, diag_path)
        ),
        name=f"monitor-{job_id}",
    )
    _tasks[job_id] = task
    task.add_done_callback(lambda _t: _tasks.pop(job_id, None))
    logger.info("shell_monitor started job %s: %s (agent_id=%s)", job_id, cmd, req.agent_id)
    return ShellMonitorResponse(
        job_id=job_id,
        status="running",
        log_path_resolved=str(spec.log_path) if spec.log_path is not None else None,
        unit_resolved=spec.unit,
        until=spec.until,
        events_path=str(events_path),
    )


async def cancel_monitor(job_id: str, db: Persistence) -> bool:
    """Cancel a live monitor (shell_kill). False if no task is registered for *job_id*.

    The task itself writes the terminal "killed" event and status; the update
    below only covers a task cancelled before it ever got to run.
    """
    task = _tasks.get(job_id)
    if task is None:
        return False
    task.cancel()
    await asyncio.wait({task}, timeout=CANCEL_WAIT_S)
    await asyncio.to_thread(
        db.update_job_status,
        job_id,
        status="killed",
        finished_at=_now_iso(),
        only_if_running=True,
    )
    return True


def _append_restart_terminal(stdout_path: str | None) -> None:
    if not stdout_path or not Path(stdout_path).exists():
        return
    record = {"ts": _now_iso(), "kind": "terminal", "state": "killed", "reason": "server_restarted"}
    with Path(stdout_path).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def reconcile_orphans(db: Persistence) -> int:
    """Startup: close out monitor jobs left 'running' by a previous server process."""
    orphans = db.kill_orphaned_monitor_jobs()
    for job in orphans:
        try:
            _append_restart_terminal(job.get("stdout_path"))
        except OSError:
            logger.exception("Could not write restart terminal for monitor %s", job["job_id"])
    return len(orphans)


async def shutdown() -> None:
    """Cancel every live monitor (server shutdown) so each writes its terminal event."""
    tasks = list(_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=CANCEL_WAIT_S)


def read_events(path: str, limit: int = MAX_EVENTS_READ) -> list[dict]:
    """Parse events.jsonl, skipping malformed lines; return the *limit* most recent events."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    events: list[dict] = []
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events[-limit:]
