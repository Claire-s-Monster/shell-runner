"""Real-systemd smoke test for detached unit jobs (issue #81).

Runs real transient units through SystemdBackend, so it needs a reachable user
manager (XDG_RUNTIME_DIR / DBUS, lingering for headless hosts). Deselected by
default; run it with:  pixi run -e dev pytest tests/integration/test_unit_systemd_smoke.py -v -m systemd
"""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from shell_runner.executor import _unit_exit_code, _unit_terminal_status
from shell_runner.unit_runner import (
    SystemdBackend,
    UnitResult,
    _run,
    build_unit_argv,
    unit_name,
)

pytestmark = pytest.mark.systemd

ALLOWLIST = {"PATH": "/usr/bin:/bin", "LANG": "C", "SR_SPECIAL": "v$X%h$$%%"}
# Variables bash itself adds to a cleared environment.
BASH_ADDED = {"PWD", "OLDPWD", "SHLVL", "_"}
WAIT_S = 20.0


@pytest.fixture(scope="module")
def backend() -> SystemdBackend:
    if shutil.which("systemd-run") is None or shutil.which("systemctl") is None:
        pytest.skip("systemd-run/systemctl not available")
    probe = asyncio.run(_run("systemctl", "--user", "is-system-running"))
    if probe[0] not in (0, 1):  # 1 = degraded, still usable; others = no user manager
        pytest.skip(f"no reachable systemd user manager: {probe[2].decode().strip()}")
    return SystemdBackend()


@pytest.fixture
def start_unit(
    backend: SystemdBackend, tmp_path: Path, request: pytest.FixtureRequest
) -> Callable[..., tuple[str, Path]]:
    started: list[str] = []
    request.addfinalizer(lambda: [asyncio.run(backend.kill(jid)) for jid in started])

    def start(command: str, runtime_max_s: int = 60) -> tuple[str, Path]:
        job_id = str(uuid.uuid4())
        job_dir = tmp_path / job_id
        job_dir.mkdir()
        argv = build_unit_argv(
            job_id, command, str(tmp_path), ALLOWLIST, str(job_dir), "256M", runtime_max_s
        )
        started.append(job_id)
        asyncio.run(backend.start(argv))
        return job_id, job_dir

    return start


def _wait_result(backend: SystemdBackend, job_dir: Path) -> UnitResult:
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        result = backend.read_result(job_dir)
        if result is not None:
            return result
        time.sleep(0.2)
    pytest.fail(f"no result file in {job_dir} after {WAIT_S}s")


def _assert_collected(backend: SystemdBackend, job_id: str) -> None:
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        _, out, _ = asyncio.run(
            _run("systemctl", "--user", "show", "-p", "LoadState", "--value", unit_name(job_id))
        )
        if out.decode().strip() == "not-found" and job_id not in asyncio.run(
            backend.active_units()
        ):
            return
        time.sleep(0.2)
    pytest.fail(f"unit for {job_id} was not collected")


def test_special_chars_env_allowlist_and_exit_status(
    backend: SystemdBackend, start_unit: Callable[..., tuple[str, Path]]
) -> None:
    job_id, job_dir = start_unit(
        "echo \"special:$SR_SPECIAL\"; echo 'lit:$HOME %h 100%'; env; exit 3"
    )
    result = _wait_result(backend, job_dir)
    assert result.exit_status == "3"
    assert _unit_exit_code(result) == 3
    assert _unit_terminal_status(result) == "failed"

    stdout = (job_dir / "stdout.log").read_text()
    assert "special:v$X%h$$%%" in stdout
    assert "lit:$HOME %h 100%" in stdout

    env_keys = {
        line.split("=", 1)[0] for line in stdout.splitlines() if "=" in line and " " not in line
    }
    assert set(ALLOWLIST) <= env_keys
    assert env_keys - set(ALLOWLIST) <= BASH_ADDED
    assert "HOME" not in env_keys
    _assert_collected(backend, job_id)


def test_runtime_max_maps_to_timed_out(
    backend: SystemdBackend, start_unit: Callable[..., tuple[str, Path]]
) -> None:
    job_id, job_dir = start_unit("sleep 30", runtime_max_s=2)
    result = _wait_result(backend, job_dir)
    assert result.service_result == "timeout"
    assert _unit_terminal_status(result) == "timed_out"
    _assert_collected(backend, job_id)
