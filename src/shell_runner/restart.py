"""MCP server restart logic for the ``mcp_restart`` tool (issue #56).

Targets come from a fixed registry (TOML); no caller input is ever interpolated
into a shell -- every command is an argv list and unit names are validated.
Blocking subprocess/HTTP work runs off the event loop via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import signal
import socket
import subprocess
import tempfile
import time
import tomllib
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ENV_REGISTRY = "SHELL_RUNNER_RESTART_REGISTRY"
DEFAULT_REGISTRY = Path(__file__).parent / "data" / "restart_registry.toml"
HELPER_RELPATH = "scripts/shell-runner-self-restart.sh"

_UNIT_RE = re.compile(r"^[A-Za-z0-9@._-]+\.service$")
_TAIL_CHARS = 2000
_JOURNAL_LINES = 40
_JOURNAL_FETCH_LINES = 400
# uvicorn access-log lines, e.g. `INFO:     127.0.0.1:48962 - "POST /observe HTTP/1.1" 200 OK`
_ACCESS_LOG_RE = re.compile(r'INFO:\s+\S+:\d+ - "[A-Z]+ \S+ HTTP/[\d.]+" \d{3}')
PREFLIGHT_TIMEOUT_S = 900
SMOKE_TIMEOUT_S = 30
HEALTH_TIMEOUT_S = 60
SYSTEMCTL_TIMEOUT_S = 60


class RegistryError(ValueError):
    """Raised when the restart registry is missing or malformed."""


@dataclass(frozen=True)
class RestartEntry:
    name: str
    unit: str
    repo: str
    port: int
    health: str | None = None
    preflight: tuple[tuple[str, ...], ...] = ()
    smoke: tuple[str, ...] | None = None
    smoke_env: dict[str, str] = field(default_factory=dict)
    self_restart: bool = False


def valid_unit(unit: str) -> bool:
    return bool(_UNIT_RE.match(unit))


def _str_list(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value) or not value:
        raise RegistryError(f"{where}: expected a non-empty list of strings")
    return tuple(value)


def _parse_entry(name: str, raw: Any) -> RestartEntry:
    if not isinstance(raw, dict):
        raise RegistryError(f"servers.{name}: expected a table")
    unit, repo, port = raw.get("unit"), raw.get("repo"), raw.get("port")
    if not isinstance(unit, str) or not valid_unit(unit):
        raise RegistryError(f"servers.{name}: invalid unit {unit!r}")
    if not isinstance(repo, str) or not repo:
        raise RegistryError(f"servers.{name}: repo is required")
    if isinstance(port, bool) or not isinstance(port, int):
        raise RegistryError(f"servers.{name}: port must be an integer")
    pre = tuple(_str_list(p, f"servers.{name}.preflight") for p in raw.get("preflight", []))
    smoke = raw.get("smoke")
    smoke_env = raw.get("smoke_env", {})
    if not isinstance(smoke_env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in smoke_env.items()
    ):
        raise RegistryError(f"servers.{name}.smoke_env: expected a string table")
    health = raw.get("health")
    if health is not None and not isinstance(health, str):
        raise RegistryError(f"servers.{name}.health: expected a string")
    if health is not None:
        parsed = urllib.parse.urlsplit(health)
        if parsed.scheme not in ("http", "https"):
            raise RegistryError(
                f"servers.{name}.health: must be an http(s) URL on a loopback host, got {health!r}"
            )
        hostname = (parsed.hostname or "").lower()
        if hostname not in ("127.0.0.1", "localhost", "::1"):
            raise RegistryError(
                f"servers.{name}.health: must be an http(s) URL on a loopback host, got {health!r}"
            )
    return RestartEntry(
        name=name,
        unit=unit,
        repo=repo,
        port=port,
        health=health,
        preflight=pre,
        smoke=_str_list(smoke, f"servers.{name}.smoke") if smoke is not None else None,
        smoke_env=dict(smoke_env),
        self_restart=bool(raw.get("self", False)),
    )


def load_registry(path: Path | str | None = None) -> dict[str, RestartEntry]:
    """Load the registry from *path*, else $SHELL_RUNNER_RESTART_REGISTRY, else the default."""
    src = Path(path or os.environ.get(ENV_REGISTRY) or DEFAULT_REGISTRY)
    try:
        data = tomllib.loads(src.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RegistryError(f"cannot load restart registry {src}: {exc}") from exc
    servers = data.get("servers")
    if not isinstance(servers, dict) or not servers:
        raise RegistryError(f"{src}: no [servers.*] entries")
    return {name: _parse_entry(name, raw) for name, raw in servers.items()}


def systemd_env() -> dict[str, str]:
    """Environment for systemctl/systemd-run/journalctl against the user manager."""
    env = dict(os.environ)
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    env["XDG_RUNTIME_DIR"] = runtime
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
    return env


def clean_env() -> dict[str, str]:
    """os.environ minus activated pixi/conda/venv state, for preflight and smoke children."""
    pixi_home = os.environ.get("PIXI_HOME")  # read before PIXI_* is dropped
    pixi_bin = (Path(pixi_home) / "bin") if pixi_home else (Path.home() / ".pixi" / "bin")
    drop = {"VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"}
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in drop and not k.startswith(("PIXI_", "CONDA_"))
    }
    entries = [p for p in env.get("PATH", os.defpath).split(os.pathsep) if "/.pixi/envs/" not in p]
    if pixi_bin.is_dir() and str(pixi_bin) not in entries:
        # systemd user units lack ~/.pixi/bin, so `pixi` would not resolve (#58)
        entries.insert(0, str(pixi_bin))
    if "PATH" in env or pixi_bin.is_dir():
        env["PATH"] = os.pathsep.join(entries)
    return env


def _tail(text: str, chars: int = _TAIL_CHARS) -> str:
    return text[-chars:]


def _run(
    argv: list[str], *, cwd: str | None, env: dict[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run an argv list (never a shell) and capture combined text output."""
    try:
        return subprocess.run(  # noqa: S603
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else ""
        return subprocess.CompletedProcess(argv, 124, out, f"timed out after {timeout}s")
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def _combined(cp: subprocess.CompletedProcess[str]) -> str:
    return _tail((cp.stdout or "") + (cp.stderr or ""))


# -- health -----------------------------------------------------------------

_INIT_BODY = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "mcp_restart", "version": "1"},
        },
    }
).encode()


def check_health(entry: RestartEntry) -> bool:
    """True when the server answers an MCP initialize POST with HTTP 200."""
    # URL is loopback-only: built from a validated int port, or a registry `health` restricted to
    # http(s)://loopback in _parse_entry.
    url = entry.health or f"http://127.0.0.1:{entry.port}/mcp"
    req = urllib.request.Request(  # noqa: S310
        url,
        data=_INIT_BODY,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    try:
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            return bool(resp.status == 200)
    except (OSError, ValueError):
        return False


def wait_healthy(entry: RestartEntry, timeout_s: float, interval_s: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if check_health(entry):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval_s)


# -- preflight / smoke ------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _stop_group(proc: subprocess.Popen[bytes]) -> None:
    for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def run_smoke(entry: RestartEntry) -> str | None:
    """Start a throwaway instance on a free port. Returns None on success, else an output tail."""
    if entry.smoke is None:
        return None
    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="mcp-restart-smoke-") as tmp:
        subs = {"{port}": str(port), "{tmpdir}": tmp, "{repo}": entry.repo}

        def sub(text: str) -> str:
            for key, val in subs.items():
                text = text.replace(key, val)
            return text

        env = clean_env()
        env.update({k: sub(v) for k, v in entry.smoke_env.items()})
        log = Path(tmp) / "smoke.log"
        with log.open("wb") as fh:
            proc = subprocess.Popen(  # noqa: S603
                [sub(a) for a in entry.smoke],
                cwd=entry.repo,
                env=env,
                stdout=fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                probe = dataclasses.replace(entry, port=port, health=None)
                deadline = time.monotonic() + SMOKE_TIMEOUT_S
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        break
                    if check_health(probe):
                        return None
                    time.sleep(0.5)
            finally:
                _stop_group(proc)
        return _tail(log.read_text(errors="replace")) or "smoke instance never became healthy"


def run_checks(entry: RestartEntry, skip_preflight: bool) -> dict[str, Any] | None:
    """Preflight commands then smoke. Returns a preflight_failed result or None."""
    env = clean_env()
    if not skip_preflight:
        for argv in entry.preflight:
            cp = _run(list(argv), cwd=entry.repo, env=env, timeout=PREFLIGHT_TIMEOUT_S)
            if cp.returncode != 0:
                return {
                    "status": "preflight_failed",
                    "step": "preflight: " + " ".join(argv),
                    "output_tail": _combined(cp),
                }
    if not skip_preflight and entry.smoke is not None:
        tail = run_smoke(entry)
        if tail is not None:
            return {"status": "preflight_failed", "step": "smoke", "output_tail": tail}
    return None


# -- restart ----------------------------------------------------------------


def _journal_tail(entry: RestartEntry, env: dict[str, str]) -> str:
    cp = _run(
        ["journalctl", "--user", "-u", entry.unit, "-n", str(_JOURNAL_FETCH_LINES), "--no-pager"],
        cwd=None,
        env=env,
        timeout=30,
    )
    lines = [
        ln for ln in (cp.stdout or cp.stderr or "").splitlines() if not _ACCESS_LOG_RE.search(ln)
    ]
    return "\n".join(lines[-_JOURNAL_LINES:])


def _restart_other(entry: RestartEntry) -> dict[str, Any]:
    env = systemd_env()
    cp = _run(
        ["systemctl", "--user", "restart", entry.unit],
        cwd=None,
        env=env,
        timeout=SYSTEMCTL_TIMEOUT_S,
    )
    if cp.returncode != 0:
        return {"status": "error", "step": "systemctl restart", "output_tail": _combined(cp)}
    if wait_healthy(entry, timeout_s=HEALTH_TIMEOUT_S):
        return {"status": "healthy", "server": entry.name, "unit": entry.unit, "port": entry.port}
    return {
        "status": "unhealthy",
        "server": entry.name,
        "unit": entry.unit,
        "journal_tail": _journal_tail(entry, env),
    }


def state_file() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "shell-runner" / "last-restart.json"


def _schedule_self(entry: RestartEntry) -> dict[str, Any]:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    helper = str(Path(entry.repo) / HELPER_RELPATH)
    cp = _run(
        [
            "systemd-run",
            "--user",
            "--on-active=2",
            f"--unit=shell-runner-restart-{stamp}",
            "--collect",
            "bash",  # no dependency on the helper's exec bit
            helper,
        ],
        cwd=None,
        env=systemd_env(),
        timeout=SYSTEMCTL_TIMEOUT_S,
    )
    if cp.returncode != 0:
        return {"status": "error", "step": "systemd-run", "output_tail": _combined(cp)}
    return {
        "status": "restarting",
        "server": entry.name,
        "state_file": str(state_file()),
        "note": "restart fires in ~2s; read state_file after reconnecting",
    }


def _restart_sync(
    entry: RestartEntry,
    force: bool,
    skip_preflight: bool,
    running_jobs: Callable[[], list[dict[str, Any]]],
) -> dict[str, Any]:
    if entry.self_restart and not force:
        jobs = running_jobs()
        if jobs:
            return {
                "status": "refused",
                "reason": "background jobs are running; restart would orphan them (use force)",
                "running_jobs": jobs,
            }
    failed = run_checks(entry, skip_preflight)
    if failed is not None:
        return failed
    return _schedule_self(entry) if entry.self_restart else _restart_other(entry)


async def mcp_restart(
    server: str,
    force: bool = False,
    skip_preflight: bool = False,
    *,
    running_jobs: Callable[[], list[dict[str, Any]]],
) -> dict[str, Any]:
    """Restart a registered MCP server after preflight and smoke checks."""
    try:
        registry = await asyncio.to_thread(load_registry)
    except RegistryError as exc:
        return {"status": "error", "error": str(exc)}
    entry = registry.get(server)
    if entry is None:
        return {
            "status": "error",
            "error": f"unknown server: {server!r}",
            "valid": sorted(registry),
        }
    return await asyncio.to_thread(_restart_sync, entry, force, skip_preflight, running_jobs)
