"""Detached jobs as transient ``systemd-run --user`` units (issue #81).

A unit job runs under the user's systemd manager, so it survives a shell-runner
restart. This module is mechanism only: a pure argv builder and a small backend
that talks to systemd. Tests fake the backend; nothing here runs at import time.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .sandbox_config import sandbox_properties

logger = logging.getLogger(__name__)

DEFAULT_MEMORY_MAX = "8G"
DEFAULT_RUNTIME_MAX_S = 14400
DEFAULT_MAX_DETACHED = 4
MAX_RUNTIME_MAX_S = 7 * 24 * 3600  # env overrides are clamped to 7 days
_MEMORY_SUFFIXES = {"K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40}
_MEMORY_FALLBACK_CEILING = 2**40  # used when physical RAM cannot be read
_MAX_RESULT_BYTES = 256

MAX_DETACHED_ENV = "SHELL_RUNNER_MAX_DETACHED"
MEMORY_MAX_ENV = "SHELL_RUNNER_UNIT_MEMORY_MAX"
RUNTIME_MAX_ENV = "SHELL_RUNNER_UNIT_RUNTIME_MAX_S"

_JOB_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_UNIT_NAME_RE = re.compile(
    r"sr-job-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.service"
)
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Finite only: a positive integer with an optional binary-size suffix.
_MEMORY_RE = re.compile(r"[1-9][0-9]*[KMGT]?")
_RUNTIME_RE = re.compile(r"[1-9][0-9]*")

_RESULT_FILE = "result"
# `systemctl stop` on a unit that is not loaded fails, but there is nothing left to stop.
_UNIT_ABSENT_RE = re.compile(r"not loaded|does not exist|not found", re.IGNORECASE)
# ASCII only: str.isdigit() accepts characters such as U+00B2 that int() rejects.
_RESULT_RE = re.compile(rb"([0-9]{1,3}|[A-Z]+) ([a-z-]+)\n?")


class UnitStartError(RuntimeError):
    """systemd-run failed to start the unit."""


class UnitBackendError(RuntimeError):
    """systemd could not be queried (so the state of running units is unknown)."""


@dataclass(frozen=True)
class UnitResult:
    """Contents of the ExecStopPost result file (systemd's $EXIT_STATUS, $SERVICE_RESULT)."""

    exit_status: str  # numeric exit code, or a signal name such as "TERM"
    service_result: str  # "success", "exit-code", "signal", "timeout", ...


# Returned by read_result for a present-but-invalid result file (never produced by
# parsing: "?" cannot match the exit-status pattern, "_" the service-result one).
UNPARSEABLE_RESULT = UnitResult(exit_status="?", service_result="result_unparseable")


def resolve_memory_max(environ: Mapping[str, str] | None = None) -> str:
    """MemoryMax for unit jobs: env override if finite and valid, else the default."""
    raw = (environ if environ is not None else os.environ).get(MEMORY_MAX_ENV)
    if raw is None:
        return DEFAULT_MEMORY_MAX
    if _MEMORY_RE.fullmatch(raw) and _memory_bytes(raw) < _memory_ceiling_bytes():
        return raw
    logger.warning("ignoring invalid %s=%r; using %s", MEMORY_MAX_ENV, raw, DEFAULT_MEMORY_MAX)
    return DEFAULT_MEMORY_MAX


def _memory_bytes(value: str) -> int:
    """Bytes in a validated _MEMORY_RE value (systemd suffixes are powers of 1024)."""
    if value[-1] in _MEMORY_SUFFIXES:
        return int(value[:-1]) * _MEMORY_SUFFIXES[value[-1]]
    return int(value)


def _memory_ceiling_bytes() -> int:
    """Exclusive upper bound for MemoryMax: physical RAM, or 1T when that is unknown."""
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return _MEMORY_FALLBACK_CEILING + 1  # sysconf failed: reject only values > 1T


def resolve_runtime_max_s(environ: Mapping[str, str] | None = None) -> int:
    """RuntimeMaxSec for unit jobs: env override if a positive integer, else the default."""
    raw = (environ if environ is not None else os.environ).get(RUNTIME_MAX_ENV)
    if raw is None:
        return DEFAULT_RUNTIME_MAX_S
    if _RUNTIME_RE.fullmatch(raw):
        return min(int(raw), MAX_RUNTIME_MAX_S)
    logger.warning("ignoring invalid %s=%r; using %d", RUNTIME_MAX_ENV, raw, DEFAULT_RUNTIME_MAX_S)
    return DEFAULT_RUNTIME_MAX_S


def resolve_max_detached(environ: Mapping[str, str] | None = None) -> int:
    """Cap on concurrently running unit jobs: env override if a positive integer, else 4."""
    raw = (environ if environ is not None else os.environ).get(MAX_DETACHED_ENV)
    if raw is None:
        return DEFAULT_MAX_DETACHED
    if _RUNTIME_RE.fullmatch(raw):  # same shape: positive integer
        return int(raw)
    logger.warning("ignoring invalid %s=%r; using %d", MAX_DETACHED_ENV, raw, DEFAULT_MAX_DETACHED)
    return DEFAULT_MAX_DETACHED


def _check_job_id(job_id: str) -> None:
    if not _JOB_ID_RE.fullmatch(job_id):
        raise ValueError(f"invalid job_id: {job_id!r}")


def unit_name(job_id: str) -> str:
    """The only unit name this module ever addresses: sr-job-<uuid>.service."""
    _check_job_id(job_id)
    name = f"sr-job-{job_id}.service"
    if not _UNIT_NAME_RE.fullmatch(name):  # defence in depth
        raise ValueError(f"invalid job_id: {job_id!r}")
    return name


def _sd_dollar(text: str) -> str:
    # Verified on a real host (systemd-run --user): `$` is expanded in the command
    # argv words after `--`, so it is doubled and arrives literally. `%` is NOT
    # expanded there (specifiers are not processed), so it must not be doubled.
    return text.replace("$", "$$")


def _sd_quote(text: str) -> str:
    """Quote one ExecStopPost word (double quotes, C-style escapes).

    Doubles both `$` and `%`; verified on a real host (exit status and the
    RuntimeMaxSec 'timeout' result were captured correctly).
    """
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped.replace("$", "$$").replace("%", "%%") + '"'


def _check_path(field: str, value: str) -> None:
    if "\n" in value or "\r" in value or "\0" in value:
        raise ValueError(f"{field} must not contain newlines or NUL")
    if not value.startswith("/"):
        raise ValueError(f"{field} must be an absolute path")


def _check_property_path(field: str, value: str) -> None:
    # systemd expands % specifiers in -p path values and escaping there is not
    # verified on a real host, so fail closed instead of escaping.
    _check_path(field, value)
    bad = [c for c in "%$\\" if c in value]
    if bad:
        raise ValueError(f"{field} must not contain {' or '.join(repr(c) for c in bad)}: {value!r}")


def build_unit_argv(
    job_id: str,
    command: str,
    cwd: str,
    env: Mapping[str, str],
    job_dir: str,
    memory_max: str,
    runtime_max_s: int,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Build the systemd-run argv for a detached job. No I/O.

    The child's environment is exactly ``env`` (via ``env -i``); neither
    ``--setenv`` nor the caller's environment is ever used. ``environ`` (default
    os.environ) only configures the sandbox properties (issue #82).
    """
    name = unit_name(job_id).removesuffix(".service")
    _check_property_path("cwd", cwd)
    _check_property_path("job_dir", job_dir)
    if not _MEMORY_RE.fullmatch(memory_max):
        raise ValueError(f"memory_max must be finite: {memory_max!r}")
    if runtime_max_s <= 0:
        raise ValueError(f"runtime_max_s must be positive: {runtime_max_s!r}")
    for key in env:
        if not _ENV_KEY_RE.fullmatch(key):
            raise ValueError(f"invalid env key: {key!r}")

    jd = shlex.quote(job_dir)
    stop_script = (
        f"echo $EXIT_STATUS $SERVICE_RESULT > {jd}/{_RESULT_FILE}.tmp"
        f" && mv {jd}/{_RESULT_FILE}.tmp {jd}/{_RESULT_FILE}"
    )
    props = [
        f"MemoryMax={memory_max}",
        "MemorySwapMax=0",
        f"RuntimeMaxSec={runtime_max_s}",
        "Type=exec",
        "KillMode=control-group",
        f"WorkingDirectory={cwd}",
        f"StandardOutput=append:{job_dir}/stdout.log",
        f"StandardError=append:{job_dir}/stderr.log",
        f"ExecStopPost=/bin/sh -c {_sd_quote(stop_script)}",
        # Paths are validated in sandbox_config, not by _check_property_path.
        *sandbox_properties(environ),
    ]
    argv = ["systemd-run", "--user", f"--unit={name}", "--collect", "--quiet"]
    for prop in props:
        argv += ["-p", prop]
    argv += ["--", "env", "-i"]
    argv += [f"{k}={_sd_dollar(v)}" for k, v in env.items()]
    argv += ["/bin/bash", "-c", _sd_dollar(command)]
    return argv


class UnitBackend(Protocol):
    """What the executor needs from the unit mechanism (faked in tests)."""

    async def start(self, argv: list[str]) -> None: ...

    async def kill(self, job_id: str) -> None: ...

    async def active_units(self) -> set[str]: ...

    def read_result(self, job_dir: Path) -> UnitResult | None: ...


async def _run(*argv: str) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode if proc.returncode is not None else -1, out, err


class SystemdBackend:
    """UnitBackend backed by the user's systemd manager (list argv, never a shell)."""

    async def start(self, argv: list[str]) -> None:
        rc, _, err = await _run(*argv)
        if rc != 0:
            raise UnitStartError(f"systemd-run exited {rc}: {err.decode(errors='replace').strip()}")

    async def kill(self, job_id: str) -> None:
        # unit_name() validates job_id, so nothing but sr-job-<uuid>.service is addressed.
        name = unit_name(job_id)
        rc, _, err = await _run("systemctl", "--user", "stop", name)
        text = err.decode(errors="replace").strip()
        if rc != 0 and not _UNIT_ABSENT_RE.search(text):
            raise UnitBackendError(f"systemctl stop {name} exited {rc}: {text}")

    async def active_units(self) -> set[str]:
        rc, out, err = await _run(
            "systemctl",
            "--user",
            "list-units",
            "--type=service",
            "--plain",
            "--no-legend",
            "--no-pager",
            "sr-job-*",
        )
        if rc != 0:
            # Not "no units": callers must not conclude jobs are gone from a failed query.
            raise UnitBackendError(
                f"systemctl list-units exited {rc}: {err.decode(errors='replace').strip()}"
            )
        found: set[str] = set()
        for line in out.decode(errors="replace").splitlines():
            parts = line.split()
            match = _UNIT_NAME_RE.fullmatch(parts[0]) if parts else None
            if match:
                found.add(match.group(1))
        return found

    def read_result(self, job_dir: Path) -> UnitResult | None:
        """None when there is no result file; UNPARSEABLE_RESULT when it is invalid."""
        path = Path(job_dir) / _RESULT_FILE
        flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return None
        except OSError:
            # ELOOP (symlink) or anything else unopenable: not a trustworthy result.
            return UNPARSEABLE_RESULT
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return UNPARSEABLE_RESULT
            data = os.read(fd, _MAX_RESULT_BYTES + 1)
        except OSError:
            return UNPARSEABLE_RESULT
        finally:
            os.close(fd)
        match = _RESULT_RE.fullmatch(data) if len(data) <= _MAX_RESULT_BYTES else None
        if match is None:
            return UNPARSEABLE_RESULT
        return UnitResult(
            exit_status=match.group(1).decode(), service_result=match.group(2).decode()
        )
