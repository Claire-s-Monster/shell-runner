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
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

DEFAULT_MEMORY_MAX = "8G"
DEFAULT_RUNTIME_MAX_S = 14400
DEFAULT_MAX_DETACHED = 4

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


class UnitStartError(RuntimeError):
    """systemd-run failed to start the unit."""


@dataclass(frozen=True)
class UnitResult:
    """Contents of the ExecStopPost result file (systemd's $EXIT_STATUS, $SERVICE_RESULT)."""

    exit_status: str  # numeric exit code, or a signal name such as "TERM"
    service_result: str  # "success", "exit-code", "signal", "timeout", ...


def resolve_memory_max(environ: Mapping[str, str] | None = None) -> str:
    """MemoryMax for unit jobs: env override if finite and valid, else the default."""
    raw = (environ if environ is not None else os.environ).get(MEMORY_MAX_ENV)
    if raw is None:
        return DEFAULT_MEMORY_MAX
    if _MEMORY_RE.fullmatch(raw):
        return raw
    logger.warning("ignoring invalid %s=%r; using %s", MEMORY_MAX_ENV, raw, DEFAULT_MEMORY_MAX)
    return DEFAULT_MEMORY_MAX


def resolve_runtime_max_s(environ: Mapping[str, str] | None = None) -> int:
    """RuntimeMaxSec for unit jobs: env override if a positive integer, else the default."""
    raw = (environ if environ is not None else os.environ).get(RUNTIME_MAX_ENV)
    if raw is None:
        return DEFAULT_RUNTIME_MAX_S
    if _RUNTIME_RE.fullmatch(raw):
        return int(raw)
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


def _sd_escape(text: str) -> str:
    # NOTE: not yet verified on a real host. systemd treats $ (variable
    # substitution) and % (specifier) specially in command lines and several path
    # properties, so both are doubled. Confirm with a real `systemd-run --user`
    # that `$$` and `%%` arrive literally before relying on this for production.
    return text.replace("$", "$$").replace("%", "%%")


def _sd_quote(text: str) -> str:
    """Quote one word of a systemd command line (double quotes, C-style escapes)."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + _sd_escape(escaped) + '"'


def _check_path(field: str, value: str) -> None:
    if "\n" in value or "\r" in value or "\0" in value:
        raise ValueError(f"{field} must not contain newlines or NUL")
    if not value.startswith("/"):
        raise ValueError(f"{field} must be an absolute path")


def build_unit_argv(
    job_id: str,
    command: str,
    cwd: str,
    env: Mapping[str, str],
    job_dir: str,
    memory_max: str,
    runtime_max_s: int,
) -> list[str]:
    """Build the systemd-run argv for a detached job. Pure: no I/O, no os.environ.

    The child's environment is exactly ``env`` (via ``env -i``); neither
    ``--setenv`` nor the caller's environment is ever used.
    """
    name = unit_name(job_id).removesuffix(".service")
    _check_path("cwd", cwd)
    _check_path("job_dir", job_dir)
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
        f"WorkingDirectory={_sd_escape(cwd)}",
        f"StandardOutput=append:{_sd_escape(job_dir)}/stdout.log",
        f"StandardError=append:{_sd_escape(job_dir)}/stderr.log",
        f"ExecStopPost=/bin/sh -c {_sd_quote(stop_script)}",
    ]
    argv = ["systemd-run", "--user", f"--unit={name}", "--collect", "--quiet"]
    for prop in props:
        argv += ["-p", prop]
    argv += ["--", "env", "-i"]
    argv += [f"{k}={_sd_escape(v)}" for k, v in env.items()]
    argv += ["/bin/bash", "-c", _sd_escape(command)]
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
        await _run("systemctl", "--user", "stop", unit_name(job_id))

    async def active_units(self) -> set[str]:
        rc, out, _ = await _run(
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
            return set()
        found: set[str] = set()
        for line in out.decode(errors="replace").splitlines():
            parts = line.split()
            match = _UNIT_NAME_RE.fullmatch(parts[0]) if parts else None
            if match:
                found.add(match.group(1))
        return found

    def read_result(self, job_dir: Path) -> UnitResult | None:
        try:
            text = (Path(job_dir) / _RESULT_FILE).read_text()
        except OSError:
            return None
        parts = text.split()
        if len(parts) != 2:
            return None
        return UnitResult(exit_status=parts[0], service_result=parts[1])
