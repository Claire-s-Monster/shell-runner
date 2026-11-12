"""unit_runner: pure argv builder, limits, unit-name validation, SystemdBackend guards.

Nothing here spawns systemd-run or systemctl: subprocess creation is faked.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from shell_runner import unit_runner
from shell_runner.unit_runner import (
    DEFAULT_MEMORY_MAX,
    DEFAULT_RUNTIME_MAX_S,
    UNPARSEABLE_RESULT,
    SystemdBackend,
    UnitResult,
    build_unit_argv,
    resolve_memory_max,
    resolve_runtime_max_s,
    unit_name,
)

JOB_ID = "123e4567-e89b-42d3-a456-426614174000"


def _argv(**over: object) -> list[str]:
    kwargs: dict[str, object] = {
        "job_id": JOB_ID,
        "command": "echo hi",
        "cwd": "/work/dir",
        "env": {"PATH": "/usr/bin", "HOME": "/home/u"},
        "job_dir": "/jobs/" + JOB_ID,
        "memory_max": "8G",
        "runtime_max_s": 14400,
        "environ": {"XDG_RUNTIME_DIR": "/run/user/1234"},
    }
    kwargs.update(over)
    return build_unit_argv(**kwargs)  # type: ignore[arg-type]


def test_defaults_are_finite() -> None:
    assert DEFAULT_MEMORY_MAX == "8G"
    assert DEFAULT_RUNTIME_MAX_S == 14400


def test_golden_argv() -> None:
    jd = "/jobs/" + JOB_ID
    stop_script = (
        f"echo $$EXIT_STATUS $$SERVICE_RESULT > {jd}/result.tmp && mv {jd}/result.tmp {jd}/result"
    )
    assert _argv() == [
        "systemd-run",
        "--user",
        f"--unit=sr-job-{JOB_ID}",
        "--collect",
        "--quiet",
        "-p",
        "MemoryMax=8G",
        "-p",
        "MemorySwapMax=0",
        "-p",
        "RuntimeMaxSec=14400",
        "-p",
        "Type=exec",
        "-p",
        "KillMode=control-group",
        "-p",
        "WorkingDirectory=/work/dir",
        "-p",
        f"StandardOutput=append:{jd}/stdout.log",
        "-p",
        f"StandardError=append:{jd}/stderr.log",
        "-p",
        f'ExecStopPost=/bin/sh -c "{stop_script}"',
        "-p",
        "ProtectSystem=strict",
        "-p",
        "PrivateUsers=yes",
        "-p",
        "ReadWritePaths=-/tmp -/var/tmp -/run/user/1234",
        "--",
        "env",
        "-i",
        "PATH=/usr/bin",
        "HOME=/home/u",
        "/bin/bash",
        "-c",
        "echo hi",
    ]


def test_sandbox_props_omitted_when_disabled() -> None:
    argv = _argv(environ={"SHELL_RUNNER_SANDBOX": "0"})
    assert not any(a.startswith(("ProtectSystem=", "PrivateUsers=", "ReadWritePaths=")) for a in argv)


def test_sandbox_extra_rw_paths_reach_argv() -> None:
    argv = _argv(environ={"XDG_RUNTIME_DIR": "/r", "SHELL_RUNNER_SANDBOX_RW_PATHS": "/srv/x"})
    assert "ReadWritePaths=-/tmp -/var/tmp -/r -/srv/x" in argv


def test_dollar_doubled_percent_literal_in_command_words() -> None:
    argv = _argv(command="echo $HOME 50%", env={"A": "x$y%z"})
    tail = argv[argv.index("--") + 1 :]
    assert tail == ["env", "-i", "A=x$$y%z", "/bin/bash", "-c", "echo $$HOME 50%"]


def test_odd_path_chars_quoted_in_exec_stop_post() -> None:
    argv = _argv(job_dir="/jobs/it's a dir")
    stop = next(a for a in argv if a.startswith("ExecStopPost="))
    assert "'\"'\"'" in stop.replace("\\", "")  # the embedded single quote


@pytest.mark.parametrize("field", ["cwd", "job_dir"])
@pytest.mark.parametrize("bad", ["%", "$", "\n", "\\"])
def test_property_paths_reject_special_chars(field: str, bad: str) -> None:
    with pytest.raises(ValueError, match=field):
        _argv(**{field: f"/tmp/a{bad}b"})


def test_env_is_only_the_passed_dict_no_setenv() -> None:
    argv = _argv(env={})
    assert "--setenv" not in " ".join(argv)
    assert not any(a.startswith("-E") for a in argv)
    assert argv[argv.index("--") + 1 : argv.index("--") + 3] == ["env", "-i"]
    assert argv[-3:] == ["/bin/bash", "-c", "echo hi"]


@pytest.mark.parametrize("key", ["1A", "A-B", "A=B", "", "A B", "A\n", "é"])
def test_bad_env_keys_rejected(key: str) -> None:
    with pytest.raises(ValueError, match="env"):
        _argv(env={key: "v"})


@pytest.mark.parametrize("field", ["cwd", "job_dir"])
def test_newline_in_paths_rejected(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        _argv(**{field: "/tmp/a\nb"})


@pytest.mark.parametrize("field", ["cwd", "job_dir"])
def test_relative_paths_rejected(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        _argv(**{field: "rel/path"})


def test_build_rejects_infinite_limits() -> None:
    with pytest.raises(ValueError, match="memory_max"):
        _argv(memory_max="infinity")
    with pytest.raises(ValueError, match="runtime_max_s"):
        _argv(runtime_max_s=0)


def test_build_rejects_bad_job_id() -> None:
    with pytest.raises(ValueError, match="job_id"):
        _argv(job_id="../../etc")


# --- unit name -------------------------------------------------------------


def test_unit_name_valid() -> None:
    assert unit_name(JOB_ID) == f"sr-job-{JOB_ID}.service"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "abc",
        JOB_ID.upper(),
        JOB_ID + "\n",
        JOB_ID + ".service",
        "sr-job-" + JOB_ID,
        "-" + JOB_ID,
        "../" + JOB_ID,
        JOB_ID[:-1],
        JOB_ID + "x",
        "123e4567-e89b-42d3-a456-42661417400g",
    ],
)
def test_unit_name_rejects(bad: str) -> None:
    with pytest.raises(ValueError, match="job_id"):
        unit_name(bad)


# --- finite limits -----------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["infinity", "Infinity", "0", "-1", "-8G", "abc", "", "8GB", "0G", "50%", "inf", "1e3", " 4G"],
)
def test_memory_max_bad_values_fall_back(value: str) -> None:
    env = {"SHELL_RUNNER_UNIT_MEMORY_MAX": value}
    assert resolve_memory_max(env) == DEFAULT_MEMORY_MAX


@pytest.mark.parametrize("value", ["512M", "2G", "1048576", "1T", "4K"])
def test_memory_max_valid_override(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    _fake_ram(monkeypatch, 2 * 2**40)
    assert resolve_memory_max({"SHELL_RUNNER_UNIT_MEMORY_MAX": value}) == value


def test_memory_max_unset_is_default() -> None:
    assert resolve_memory_max({}) == DEFAULT_MEMORY_MAX


@pytest.mark.parametrize("value", ["infinity", "0", "-5", "abc", "", "1.5", "1e3", "inf", " 60"])
def test_runtime_bad_values_fall_back(value: str) -> None:
    env = {"SHELL_RUNNER_UNIT_RUNTIME_MAX_S": value}
    assert resolve_runtime_max_s(env) == DEFAULT_RUNTIME_MAX_S


def test_runtime_valid_override_and_unset() -> None:
    assert resolve_runtime_max_s({"SHELL_RUNNER_UNIT_RUNTIME_MAX_S": "600"}) == 600
    assert resolve_runtime_max_s({}) == DEFAULT_RUNTIME_MAX_S


# --- SystemdBackend (subprocess creation faked) --------------------------------


class _FakeProc:
    def __init__(self, rc: int = 0, out: bytes = b"", err: bytes = b"") -> None:
        self.returncode = rc
        self._out, self._err = out, err

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._out, self._err


def _patch_exec(
    monkeypatch: pytest.MonkeyPatch, proc: _FakeProc | None = None
) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    async def fake(*argv: str, **kwargs: object) -> _FakeProc:
        assert "shell" not in kwargs
        calls.append(argv)
        return proc or _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    return calls


async def test_kill_refuses_non_uuid_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_exec(monkeypatch)
    with pytest.raises(ValueError, match="job_id"):
        await SystemdBackend().kill("--all")
    with pytest.raises(ValueError, match="job_id"):
        await SystemdBackend().kill("sr-job-x*")
    assert calls == []


async def test_kill_uses_unit_name_only(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_exec(monkeypatch)
    await SystemdBackend().kill(JOB_ID)
    assert calls == [("systemctl", "--user", "stop", f"sr-job-{JOB_ID}.service")]


async def test_start_runs_argv_without_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_exec(monkeypatch)
    await SystemdBackend().start(["systemd-run", "--user", "x"])
    assert calls == [("systemd-run", "--user", "x")]


async def test_start_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_exec(monkeypatch, _FakeProc(rc=1, err=b"Failed to connect to bus"))
    with pytest.raises(unit_runner.UnitStartError, match="Failed to connect"):
        await SystemdBackend().start(["systemd-run", "--user", "x"])


async def test_active_units_parses_and_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    out = (
        f"sr-job-{JOB_ID}.service loaded active running x\n"
        "sr-job-not-a-uuid.service loaded active running y\n"
        "other.service loaded active running z\n"
    ).encode()
    _patch_exec(monkeypatch, _FakeProc(out=out))
    assert await SystemdBackend().active_units() == {JOB_ID}


async def test_active_units_raises_when_systemctl_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed query is not 'no units': reconcile must be able to tell them apart."""
    _patch_exec(monkeypatch, _FakeProc(rc=1, err=b"Failed to connect to bus"))
    with pytest.raises(unit_runner.UnitBackendError, match="Failed to connect"):
        await SystemdBackend().active_units()


async def test_active_units_empty_listing_is_an_empty_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_exec(monkeypatch, _FakeProc(rc=0, out=b""))
    assert await SystemdBackend().active_units() == set()


# --- read_result -----------------------------------------------------------------


def test_read_result_missing(tmp_path: Path) -> None:
    assert SystemdBackend().read_result(tmp_path) is None


def test_read_result_parses(tmp_path: Path) -> None:
    (tmp_path / "result").write_text("3 exit-code\n")
    assert SystemdBackend().read_result(tmp_path) == UnitResult(
        exit_status="3", service_result="exit-code"
    )


@pytest.mark.parametrize(
    "text", ["", "² success\n", "1234 exit-code\n", "0 success\nextra\n", "0 success" + " x" * 200]
)
def test_read_result_garbage_is_unparseable(tmp_path: Path, text: str) -> None:
    (tmp_path / "result").write_text(text)
    assert SystemdBackend().read_result(tmp_path) == UNPARSEABLE_RESULT


def test_read_result_oversized_is_unparseable(tmp_path: Path) -> None:
    (tmp_path / "result").write_bytes(b"0 success\n" + b"\0" * 10_000)
    assert SystemdBackend().read_result(tmp_path) == UNPARSEABLE_RESULT


def test_read_result_oversized_wellformed_is_unparseable(tmp_path: Path) -> None:
    (tmp_path / "result").write_bytes(b"0 " + b"a" * 10_000 + b"\n")
    assert SystemdBackend().read_result(tmp_path) == UNPARSEABLE_RESULT


@pytest.mark.timeout(5)
def test_read_result_fifo_is_unparseable(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "result")
    assert SystemdBackend().read_result(tmp_path) == UNPARSEABLE_RESULT


def test_read_result_symlink_is_unparseable(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("0 success\n")
    (tmp_path / "result").symlink_to(target)
    assert SystemdBackend().read_result(tmp_path) == UNPARSEABLE_RESULT


def test_read_result_directory_is_unparseable(tmp_path: Path) -> None:
    (tmp_path / "result").mkdir()
    assert SystemdBackend().read_result(tmp_path) == UNPARSEABLE_RESULT


def test_read_result_signal_name_parses(tmp_path: Path) -> None:
    (tmp_path / "result").write_text("TERM signal\n")
    assert SystemdBackend().read_result(tmp_path) == UnitResult("TERM", "signal")


# --- kill return code (finding 3) --------------------------------------------------


async def test_kill_nonzero_rc_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_exec(monkeypatch, _FakeProc(rc=1, err=b"Failed to stop: Access denied"))
    with pytest.raises(unit_runner.UnitBackendError, match="Access denied"):
        await SystemdBackend().kill(JOB_ID)


@pytest.mark.parametrize(
    "err", [b"Unit sr-job-x.service not loaded.", b"Unit sr-job-x.service does not exist."]
)
async def test_kill_unit_not_loaded_is_success(monkeypatch: pytest.MonkeyPatch, err: bytes) -> None:
    _patch_exec(monkeypatch, _FakeProc(rc=5, err=err))
    await SystemdBackend().kill(JOB_ID)


# --- env upper bounds (finding 4) ---------------------------------------------------


def test_runtime_clamped_to_seven_days() -> None:
    assert resolve_runtime_max_s({"SHELL_RUNNER_UNIT_RUNTIME_MAX_S": "99999999999"}) == 604800
    assert resolve_runtime_max_s({"SHELL_RUNNER_UNIT_RUNTIME_MAX_S": "604800"}) == 604800


def _fake_ram(monkeypatch: pytest.MonkeyPatch, ram_bytes: int | None) -> None:
    def sysconf(name: str) -> int:
        if ram_bytes is None:
            raise ValueError(name)
        return {"SC_PHYS_PAGES": ram_bytes // 4096, "SC_PAGE_SIZE": 4096}[name]

    monkeypatch.setattr(unit_runner.os, "sysconf", sysconf)


@pytest.mark.parametrize("value", ["16G", "17G", "1T", "16384M", "17179869184"])
def test_memory_max_at_or_above_ram_falls_back(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    _fake_ram(monkeypatch, 16 * 2**30)
    assert resolve_memory_max({"SHELL_RUNNER_UNIT_MEMORY_MAX": value}) == DEFAULT_MEMORY_MAX


@pytest.mark.parametrize("value", ["15G", "512M", "4K"])
def test_memory_max_below_ram_accepted(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    _fake_ram(monkeypatch, 16 * 2**30)
    assert resolve_memory_max({"SHELL_RUNNER_UNIT_MEMORY_MAX": value}) == value


def test_memory_max_sysconf_failure_caps_at_one_tib(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_ram(monkeypatch, None)
    assert resolve_memory_max({"SHELL_RUNNER_UNIT_MEMORY_MAX": "1T"}) == "1T"
    assert resolve_memory_max({"SHELL_RUNNER_UNIT_MEMORY_MAX": "2T"}) == DEFAULT_MEMORY_MAX
