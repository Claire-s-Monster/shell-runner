"""Unit tests for the mcp_restart logic (issue #56). All subprocess/HTTP is mocked."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from shell_runner import restart
from shell_runner.restart import RegistryError, RestartEntry


def _entry(**kw: Any) -> RestartEntry:
    base: dict[str, Any] = {
        "name": "git",
        "unit": "mcp-git-http.service",
        "repo": "/tmp/repo",
        "port": 4100,
    }
    base.update(kw)
    return RestartEntry(**base)


def _cp(rc: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], rc, out, err)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(restart.time, "sleep", lambda _s: None)


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, RestartEntry]:
    reg = {
        "git": _entry(),
        "shell-runner": _entry(
            name="shell-runner",
            unit="shell-runner.service",
            port=4111,
            self_restart=True,
            repo="/tmp/sr",
        ),
    }
    monkeypatch.setattr(restart, "load_registry", lambda path=None: reg)
    return reg


# -- registry ---------------------------------------------------------------


def test_default_registry_loads() -> None:
    reg = restart.load_registry()
    assert set(reg) == {
        "git",
        "pixi-task",
        "quality-assurance",
        "session-intelligence",
        "shell-runner",
    }
    assert reg["git"].unit == "mcp-git-http.service"
    assert reg["shell-runner"].self_restart is True
    assert reg["shell-runner"].smoke is not None
    assert "SHELL_RUNNER_DB" in reg["shell-runner"].smoke_env


def test_env_var_overrides_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "r.toml"
    f.write_text('[servers.x]\nunit = "x.service"\nport = 1234\nrepo = "/r"\n')
    monkeypatch.setenv(restart.ENV_REGISTRY, str(f))
    reg = restart.load_registry()
    assert set(reg) == {"x"}
    assert reg["x"].port == 1234


@pytest.mark.parametrize(
    "body",
    [
        '[servers.x]\nunit = "bad unit.service"\nport = 1\nrepo = "/r"\n',
        '[servers.x]\nunit = "x.service"\nport = "1"\nrepo = "/r"\n',
        '[servers.x]\nunit = "x.service"\nport = 1\n',
        '[servers.x]\nunit = "x.service"\nport = 1\nrepo = "/r"\npreflight = ["pytest"]\n',
        "not = [valid",
        "",
    ],
)
def test_invalid_registry_rejected(tmp_path: Path, body: str) -> None:
    f = tmp_path / "r.toml"
    f.write_text(body)
    with pytest.raises(RegistryError):
        restart.load_registry(f)


@pytest.mark.parametrize("unit", ["a.service", "a@b.c-d_e.service"])
def test_unit_name_valid(unit: str) -> None:
    assert restart.valid_unit(unit)


@pytest.mark.parametrize(
    "unit", ["a", "a.timer", "a b.service", "a;rm.service", "../a.service", ""]
)
def test_unit_name_invalid(unit: str) -> None:
    assert not restart.valid_unit(unit)


def test_systemd_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    env = restart.systemd_env()
    assert env["XDG_RUNTIME_DIR"].startswith("/run/user/")
    assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={env['XDG_RUNTIME_DIR']}/bus"


def test_systemd_env_from_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/x")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/y")
    env = restart.systemd_env()
    assert env["XDG_RUNTIME_DIR"] == "/x"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/y"


def _pixi_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, create: bool = True) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PIXI_HOME", raising=False)
    pixi_bin = tmp_path / ".pixi" / "bin"
    if create:
        pixi_bin.mkdir(parents=True)
    return pixi_bin


def test_clean_env_prepends_pixi_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pixi_bin = _pixi_home(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert restart.clean_env()["PATH"] == f"{pixi_bin}:/usr/bin:/bin"


def test_clean_env_no_duplicate_pixi_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pixi_bin = _pixi_home(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", f"/usr/bin:{pixi_bin}:/bin")
    assert restart.clean_env()["PATH"] == f"/usr/bin:{pixi_bin}:/bin"


def test_clean_env_skips_missing_pixi_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pixi_home(tmp_path, monkeypatch, create=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert restart.clean_env()["PATH"] == "/usr/bin:/bin"


def test_clean_env_honors_pixi_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pixi_home(tmp_path, monkeypatch, create=False)
    custom = tmp_path / "custom"
    (custom / "bin").mkdir(parents=True)
    monkeypatch.setenv("PIXI_HOME", str(custom))
    monkeypatch.setenv("PATH", "/usr/bin")
    env = restart.clean_env()
    assert env["PATH"] == f"{custom / 'bin'}:/usr/bin"
    assert "PIXI_HOME" not in env


def test_clean_env_missing_path_uses_defpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pixi_bin = _pixi_home(tmp_path, monkeypatch)
    monkeypatch.delenv("PATH", raising=False)
    assert restart.clean_env()["PATH"] == str(pixi_bin) + os.pathsep + os.defpath


def test_preflight_finds_pixi_outside_service_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for #58: a systemd-style PATH lacking ~/.pixi/bin must still find pixi."""
    pixi_bin = _pixi_home(tmp_path, monkeypatch, create=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    entry = _entry(repo=str(tmp_path), preflight=(("pixi", "run", "x"),))
    failed = restart.run_checks(entry, skip_preflight=False)
    assert failed is not None
    assert failed["status"] == "preflight_failed"

    pixi_bin.mkdir(parents=True)
    stub = pixi_bin / "pixi"
    stub.write_text('#!/bin/sh\necho stub-pixi "$@"\n')
    os.chmod(stub, 0o755)  # noqa: S103
    assert restart.run_checks(entry, skip_preflight=False) is None


def test_clean_env_drops_activated_env_leakage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pixi_home(tmp_path, monkeypatch, create=False)
    for k, v in {
        "PIXI_PROJECT_ROOT": "/p",
        "CONDA_PREFIX": "/c",
        "VIRTUAL_ENV": "/v",
        "PYTHONPATH": "/pp",
        "PYTHONHOME": "/ph",
        "KEEP_ME": "1",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PATH", "/a/.pixi/envs/ci/bin:/usr/bin:/b/.pixi/envs/dev/bin:/bin")
    env = restart.clean_env()
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["KEEP_ME"] == "1"
    for k in ("PIXI_PROJECT_ROOT", "CONDA_PREFIX", "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"):
        assert k not in env


def test_preflight_uses_clean_env(
    registry: dict[str, RestartEntry], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PIXI_ENVIRONMENT_NAME", "ci")
    seen: list[dict[str, str]] = []

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kw["env"])
        return _cp()

    monkeypatch.setattr(restart, "_run", fake_run)
    assert restart.run_checks(_entry(preflight=(("pytest",),)), False) is None
    assert "PIXI_ENVIRONMENT_NAME" not in seen[0]


# -- mcp_restart ------------------------------------------------------------


async def test_unknown_server_rejected(registry: dict[str, RestartEntry]) -> None:
    res = await restart.mcp_restart("nope", running_jobs=list)
    assert res["status"] == "error"
    assert "git" in res["valid"]


async def test_preflight_failure_leaves_unit_untouched(
    registry: dict[str, RestartEntry], monkeypatch: pytest.MonkeyPatch
) -> None:
    registry["git"] = _entry(preflight=(("pytest", "-q"),))
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _cp(1, "boom\n" * 100)

    monkeypatch.setattr(restart, "_run", fake_run)
    res = await restart.mcp_restart("git", running_jobs=list)
    assert res["status"] == "preflight_failed"
    assert res["step"] == "preflight: pytest -q"
    assert len(res["output_tail"]) < 5000
    assert not any(a[0] == "systemctl" for a in calls)


async def test_skip_preflight_skips_steps(
    registry: dict[str, RestartEntry], monkeypatch: pytest.MonkeyPatch
) -> None:
    registry["git"] = _entry(preflight=(("pytest",),))
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _cp()

    monkeypatch.setattr(restart, "_run", fake_run)
    monkeypatch.setattr(restart, "wait_healthy", lambda e, timeout_s: True)
    res = await restart.mcp_restart("git", skip_preflight=True, running_jobs=list)
    assert res["status"] == "healthy"
    assert [c[0] for c in calls] == ["systemctl"]


async def test_smoke_failure_blocks_restart(
    registry: dict[str, RestartEntry], monkeypatch: pytest.MonkeyPatch
) -> None:
    registry["git"] = _entry(smoke=("srv", "--port", "{port}"))
    calls: list[list[str]] = []
    monkeypatch.setattr(restart, "_run", lambda argv, **kw: calls.append(argv) or _cp())
    monkeypatch.setattr(restart, "run_smoke", lambda e: "smoke died")
    res = await restart.mcp_restart("git", running_jobs=list)
    assert res["status"] == "preflight_failed"
    assert res["step"] == "smoke"
    assert res["output_tail"] == "smoke died"
    assert calls == []


async def test_non_self_success(
    registry: dict[str, RestartEntry], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kw))
        return _cp()

    monkeypatch.setattr(restart, "_run", fake_run)
    monkeypatch.setattr(restart, "wait_healthy", lambda e, timeout_s: True)
    res = await restart.mcp_restart("git", running_jobs=list)
    assert res["status"] == "healthy"
    argv, kw = calls[0]
    assert argv == ["systemctl", "--user", "restart", "mcp-git-http.service"]
    assert "XDG_RUNTIME_DIR" in kw["env"]
    assert "DBUS_SESSION_BUS_ADDRESS" in kw["env"]


async def test_non_self_unhealthy_returns_journal(
    registry: dict[str, RestartEntry], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[0] == "journalctl":
            return _cp(0, "\n".join(f"line{i}" for i in range(100)))
        return _cp()

    monkeypatch.setattr(restart, "_run", fake_run)
    monkeypatch.setattr(restart, "wait_healthy", lambda e, timeout_s: False)
    res = await restart.mcp_restart("git", running_jobs=list)
    assert res["status"] == "unhealthy"
    assert res["journal_tail"].splitlines()[-1] == "line99"
    assert len(res["journal_tail"].splitlines()) == 40
    assert ["journalctl", "--user", "-u", "mcp-git-http.service", "-n", "40", "--no-pager"] in calls


async def test_restart_command_failure(
    registry: dict[str, RestartEntry], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(restart, "_run", lambda argv, **kw: _cp(5, "", "no such unit"))
    res = await restart.mcp_restart("git", running_jobs=list)
    assert res["status"] == "error"
    assert "no such unit" in res["output_tail"]


async def test_self_refuses_with_running_jobs(
    registry: dict[str, RestartEntry], monkeypatch: pytest.MonkeyPatch
) -> None:
    called = []
    monkeypatch.setattr(restart, "_run", lambda argv, **kw: called.append(argv) or _cp())
    jobs = [{"job_id": "j1", "command": "sleep 100"}]
    res = await restart.mcp_restart("shell-runner", running_jobs=lambda: jobs)
    assert res["status"] == "refused"
    assert res["running_jobs"][0]["job_id"] == "j1"
    assert called == []


async def test_self_force_invokes_systemd_run(
    registry: dict[str, RestartEntry], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kw))
        return _cp()

    monkeypatch.setattr(restart, "_run", fake_run)
    res = await restart.mcp_restart(
        "shell-runner", force=True, running_jobs=lambda: [{"job_id": "j1"}]
    )
    assert res["status"] == "restarting"
    assert res["state_file"].endswith("shell-runner/last-restart.json")
    argv, kw = calls[-1]
    assert argv[:4] == ["systemd-run", "--user", "--on-active=2", argv[3]]
    assert argv[3].startswith("--unit=shell-runner-restart-")
    assert "--collect" in argv
    assert argv[-1] == "/tmp/sr/scripts/shell-runner-self-restart.sh"
    assert kw["env"]["XDG_RUNTIME_DIR"]
    assert kw["env"]["DBUS_SESSION_BUS_ADDRESS"]
    assert not any(c[0][0] == "systemctl" for c in calls)


async def test_self_no_jobs_schedules_without_force(
    registry: dict[str, RestartEntry], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(restart, "_run", lambda argv, **kw: _cp())
    res = await restart.mcp_restart("shell-runner", running_jobs=list)
    assert res["status"] == "restarting"


async def test_self_systemd_run_failure(
    registry: dict[str, RestartEntry], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(restart, "_run", lambda argv, **kw: _cp(1, "", "denied"))
    res = await restart.mcp_restart("shell-runner", running_jobs=list)
    assert res["status"] == "error"


# -- health -----------------------------------------------------------------


def test_check_health_uses_initialize_post(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class Resp:
        status = 200

        def __enter__(self) -> Resp:
            return self

        def __exit__(self, *a: object) -> None:
            return None

    def fake_urlopen(req: Any, timeout: float) -> Resp:
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["accept"] = req.get_header("Accept")
        return Resp()

    monkeypatch.setattr(restart.urllib.request, "urlopen", fake_urlopen)
    assert restart.check_health(_entry())
    assert seen["url"] == "http://127.0.0.1:4100/mcp"
    assert seen["method"] == "POST"
    assert seen["accept"] == "application/json, text/event-stream"


def test_check_health_false_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(req: Any, timeout: float) -> None:
        raise OSError("refused")

    monkeypatch.setattr(restart.urllib.request, "urlopen", boom)
    assert not restart.check_health(_entry())


@pytest.mark.parametrize(
    "health",
    [
        "file:///etc/passwd",
        "http://example.com/mcp",
        "ftp://127.0.0.1/x",
        "http://192.168.1.1:4100",
    ],
)
def test_health_url_validation_rejects_invalid(tmp_path: Path, health: str) -> None:
    f = tmp_path / "r.toml"
    f.write_text(f'[servers.x]\nunit = "x.service"\nport = 1\nrepo = "/r"\nhealth = "{health}"\n')
    with pytest.raises(RegistryError, match="loopback"):
        restart.load_registry(f)


@pytest.mark.parametrize(
    "health",
    [
        "http://127.0.0.1:4100/health",
        "http://localhost:4100/mcp",
        "http://[::1]:4100/mcp",
        "https://127.0.0.1:4100/health",
        "https://localhost:4100/mcp",
        "https://[::1]:4100/mcp",
    ],
)
def test_health_url_validation_accepts_loopback(tmp_path: Path, health: str) -> None:
    f = tmp_path / "r.toml"
    f.write_text(f'[servers.x]\nunit = "x.service"\nport = 1\nrepo = "/r"\nhealth = "{health}"\n')
    reg = restart.load_registry(f)
    assert reg["x"].health == health
