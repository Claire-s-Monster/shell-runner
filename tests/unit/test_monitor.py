"""Unit tests for shell_monitor (issue #68): the watcher in shell_runner.monitor.

systemctl is faked by replacing monitor._systemctl_show; logs live under tmp_path
and the poll intervals are shrunk so each test finishes in well under a second
(except the ones that exercise a real timeout/grace of 1s).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

import shell_runner.server as server_module
from shell_runner import executor, monitor
from shell_runner.models import ShellKillRequest, ShellMonitorRequest, ShellMonitorResponse
from shell_runner.persistence import Persistence


@pytest.fixture(autouse=True)
def _no_cwd_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start from empty cwd roots: other suites reload executor with a '/' root."""
    monkeypatch.setattr(executor, "_CWD_ROOTS", [])


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Persistence:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("SHELL_RUNNER_JOB_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("SHELL_RUNNER_MONITOR_LOG_DIRS", str(tmp_path / "logs"))
    monkeypatch.setattr(monitor, "UNIT_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(monitor, "LOG_POLL_INTERVAL_S", 0.01)
    (tmp_path / "logs").mkdir()
    fresh = Persistence(db_path=tmp_path / "test.sqlite3")
    monkeypatch.setattr("shell_runner.server.db", fresh)
    return fresh


def props(
    active: str = "active",
    sub: str = "running",
    *,
    result: str = "success",
    status: str = "0",
    load: str = "loaded",
) -> dict[str, str]:
    return {
        "LoadState": load,
        "ActiveState": active,
        "SubState": sub,
        "Result": result,
        "ExecMainStatus": status,
        "ExecMainCode": "1",
    }


NOT_FOUND = props("inactive", "dead", load="not-found")


class FakeSystemctl:
    """Scripted `systemctl show`: each call consumes one entry, the last one repeats."""

    def __init__(self, *script: dict[str, str] | tuple[int, str]) -> None:
        self.script = list(script)
        self.calls = 0

    def __call__(self, unit: str) -> subprocess.CompletedProcess[str]:
        self.calls += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, tuple):
            return subprocess.CompletedProcess([unit], item[0], "", item[1])
        out = "".join(f"{k}={v}\n" for k, v in item.items())
        return subprocess.CompletedProcess([unit], 0, out, "")


def fake_systemctl(monkeypatch: pytest.MonkeyPatch, *script: dict[str, str] | tuple[int, str]):
    fake = FakeSystemctl(*script)
    monkeypatch.setattr(monitor, "_systemctl_show", fake)
    return fake


async def wait_done(db: Persistence, job_id: str, timeout: float = 15.0) -> dict:
    async with asyncio.timeout(timeout):
        while True:
            job = db.get_job(job_id)
            assert job is not None
            if job["status"] != "running":
                return job
            await asyncio.sleep(0.01)


async def run_monitor(
    db: Persistence, **kwargs: Any
) -> tuple[ShellMonitorResponse, dict, list[dict]]:
    resp = await monitor.start_monitor(ShellMonitorRequest(agent_id="t", **kwargs), db)
    assert resp.status == "running", resp.error
    assert resp.job_id is not None
    assert resp.events_path is not None
    job = await wait_done(db, resp.job_id)
    return resp, job, monitor.read_events(resp.events_path)


def terminal(events: list[dict]) -> dict:
    assert events[-1]["kind"] == "terminal"
    assert sum(1 for e in events if e["kind"] == "terminal") == 1
    return events[-1]


def kinds(events: list[dict], kind: str) -> list[dict]:
    return [e for e in events if e["kind"] == kind]


# --------------------------------------------------------------------------
# unit watching
# --------------------------------------------------------------------------


async def test_unit_runs_then_exits_success(db: Persistence, monkeypatch) -> None:
    fake_systemctl(monkeypatch, props(), props(), props("inactive", "dead"))
    resp, job, events = await run_monitor(db, unit="svc")
    assert resp.unit_resolved == "svc.service"
    assert resp.until == "unit_exit"
    assert job["status"] == "completed"
    assert job["exit_code"] == 0
    assert job["pid"] is None
    assert [e["active_state"] for e in kinds(events, "unit")] == ["active", "inactive"]
    term = terminal(events)
    assert term["state"] == "completed"
    assert term["result"] == "success"
    with db._conn() as conn:
        decision = conn.execute(
            "SELECT decision FROM shell_calls WHERE id = ?", (job["telemetry_id"],)
        ).fetchone()[0]
    assert decision == "executed"


async def test_unit_result_exit_code_is_failed_with_fields(db: Persistence, monkeypatch) -> None:
    fake_systemctl(monkeypatch, props(), props("failed", "failed", result="exit-code", status="3"))
    _, job, events = await run_monitor(db, unit="svc.service")
    term = terminal(events)
    assert job["status"] == "failed"
    assert job["exit_code"] == 3
    assert term["state"] == "failed"
    assert term["reason"] == "unit_failed"
    assert term["result"] == "exit-code"
    assert term["exec_main_status"] == 3


async def test_unit_already_failed_on_first_poll(db: Persistence, monkeypatch) -> None:
    fake_systemctl(monkeypatch, props("failed", "failed", result="signal", status="0"))
    _, job, events = await run_monitor(db, unit="svc", unit_grace_s=0)
    assert job["status"] == "failed"
    assert terminal(events)["result"] == "signal"


async def test_inactive_then_started_then_exits_is_unit_exited(
    db: Persistence, monkeypatch
) -> None:
    idle = props("inactive", "dead")
    fake_systemctl(monkeypatch, idle, idle, props(), props("inactive", "dead"))
    _, job, events = await run_monitor(db, unit="svc", unit_grace_s=30)
    assert job["status"] == "completed"
    assert terminal(events)["reason"] == "unit_exited"


async def test_inactive_throughout_grace_reports_never_seen_active(
    db: Persistence, monkeypatch
) -> None:
    fake_systemctl(monkeypatch, props("inactive", "dead", result="exit-code", status="2"))
    _, job, events = await run_monitor(db, unit="svc", unit_grace_s=1)
    term = terminal(events)
    assert term["reason"] == "unit_inactive_never_seen_active"
    assert term["result"] == "exit-code"
    assert job["status"] == "failed"


async def test_inactive_then_not_found_never_active_is_unit_missing(
    db: Persistence, monkeypatch
) -> None:
    fake_systemctl(monkeypatch, props("inactive", "dead"), NOT_FOUND)
    _, job, events = await run_monitor(db, unit="svc", unit_grace_s=1)
    assert job["status"] == "unit_missing"
    assert terminal(events)["reason"] == "unit_not_found"


async def test_unit_never_appears_is_unit_missing_after_grace(db: Persistence, monkeypatch) -> None:
    fake = fake_systemctl(monkeypatch, NOT_FOUND)
    loop = asyncio.get_running_loop()
    start = loop.time()
    _, job, events = await run_monitor(db, unit="ghost", unit_grace_s=1)
    assert loop.time() - start >= 0.9
    assert fake.calls > 2
    assert job["status"] == "unit_missing"
    assert terminal(events)["state"] == "unit_missing"


async def test_unit_appearing_within_grace_is_followed(db: Persistence, monkeypatch) -> None:
    fake_systemctl(monkeypatch, NOT_FOUND, NOT_FOUND, props(), props("inactive", "dead"))
    _, job, _ = await run_monitor(db, unit="late", unit_grace_s=30)
    assert job["status"] == "completed"


async def test_unit_collected_after_exit_is_completed(db: Persistence, monkeypatch) -> None:
    fake_systemctl(monkeypatch, props(), NOT_FOUND)
    _, job, events = await run_monitor(db, unit="transient")
    assert job["status"] == "completed"
    assert terminal(events)["reason"] == "unit_collected_after_exit"


async def test_systemctl_errors_three_in_a_row_fail(db: Persistence, monkeypatch) -> None:
    fake_systemctl(monkeypatch, (127, "no systemctl"))
    _, job, events = await run_monitor(db, unit="svc")
    assert job["status"] == "failed"
    assert terminal(events)["reason"] == "systemctl_error"
    assert len(kinds(events, "note")) == 3


async def test_isolated_systemctl_error_is_survived(db: Persistence, monkeypatch) -> None:
    fake_systemctl(
        monkeypatch, (124, "timed out"), props(), (124, "timed out"), props("inactive", "dead")
    )
    _, job, events = await run_monitor(db, unit="svc")
    assert job["status"] == "completed"
    assert len(kinds(events, "note")) == 2


# --------------------------------------------------------------------------
# log following
# --------------------------------------------------------------------------


async def test_log_created_later_pattern_events_and_until_pattern(
    db: Persistence, tmp_path: Path
) -> None:
    log = tmp_path / "logs" / "later.log"

    async def writer() -> None:
        await asyncio.sleep(0.1)
        log.write_text("noise\nERROR boom\nnoise\nALL DONE\n")

    task = asyncio.create_task(writer())
    resp, job, events = await run_monitor(
        db, log_path=str(log), patterns=["ERROR"], until="pattern:ALL DONE"
    )
    await task
    assert resp.log_path_resolved == str(log)
    assert job["status"] == "completed"
    lines = kinds(events, "line")
    assert [e["text"] for e in lines] == ["ERROR boom", "ALL DONE"]
    assert lines[0]["pattern"] == "ERROR"
    assert lines[0]["source"] == "log"
    assert terminal(events)["reason"] == "pattern_matched"


async def test_existing_log_is_read_from_end_unless_from_start(
    db: Persistence, tmp_path: Path
) -> None:
    log = tmp_path / "logs" / "old.log"
    log.write_text("ERROR old\n")

    async def writer() -> None:
        await asyncio.sleep(0.1)
        with log.open("a") as fh:
            fh.write("ERROR new\nEND\n")

    task = asyncio.create_task(writer())
    _, _, events = await run_monitor(db, log_path=str(log), patterns=["ERROR"], until="pattern:END")
    await task
    assert [e["text"] for e in kinds(events, "line")] == ["ERROR new", "END"]

    log.write_text("ERROR old\nEND\n")
    _, _, events = await run_monitor(
        db, log_path=str(log), patterns=["ERROR"], until="pattern:END", from_start=True
    )
    assert [e["text"] for e in kinds(events, "line")] == ["ERROR old", "END"]


async def test_unit_exit_before_pattern_is_failed_even_on_success(
    db: Persistence, monkeypatch, tmp_path: Path
) -> None:
    fake_systemctl(monkeypatch, props(), props("inactive", "dead"))
    log = tmp_path / "logs" / "quiet.log"
    log.write_text("")
    _, job, events = await run_monitor(db, unit="svc", log_path=str(log), until="pattern:NEVER")
    assert job["status"] == "failed"
    term = terminal(events)
    assert term["reason"] == "unit_exited_before_pattern"
    assert term["result"] == "success"


async def test_pattern_in_final_drain_wins_over_unit_exit(
    db: Persistence, monkeypatch, tmp_path: Path
) -> None:
    log = tmp_path / "logs" / "final.log"
    log.write_text("")
    fake_systemctl(monkeypatch, props("inactive", "dead"))
    log.write_text("DONE\n")
    _, job, events = await run_monitor(
        db, unit="svc", log_path=str(log), until="pattern:DONE", from_start=True
    )
    assert job["status"] == "completed"
    assert terminal(events)["reason"] == "pattern_matched"


async def test_log_idle_completes(db: Persistence, tmp_path: Path) -> None:
    log = tmp_path / "logs" / "idle.log"
    log.write_text("start\n")
    _, job, events = await run_monitor(db, log_path=str(log), until="log_idle:0.2")
    assert job["status"] == "completed"
    assert terminal(events)["reason"] == "log_idle"
    assert kinds(events, "line") == []  # no patterns -> no line events


async def test_timeout_writes_terminal(db: Persistence, monkeypatch) -> None:
    fake_systemctl(monkeypatch, props())
    _, job, events = await run_monitor(db, unit="forever", timeout_s=1)
    assert job["status"] == "timed_out"
    assert terminal(events)["reason"] == "timeout"


async def test_max_events_truncates_but_keeps_watching(db: Persistence, tmp_path: Path) -> None:
    log = tmp_path / "logs" / "many.log"
    log.write_text("")

    async def writer() -> None:
        await asyncio.sleep(0.1)
        with log.open("a") as fh:
            fh.write("hit\n" * 6 + "END\n")

    task = asyncio.create_task(writer())
    _, job, events = await run_monitor(
        db, log_path=str(log), patterns=["hit"], until="pattern:END", max_events=2
    )
    await task
    assert job["status"] == "completed"
    assert len(kinds(events, "line")) == 2
    notes = kinds(events, "note")
    assert len(notes) == 1
    assert "truncated" in notes[0]["text"]
    assert terminal(events)["reason"] == "pattern_matched"


async def test_rotation_and_truncation_reopen_from_start(db: Persistence, tmp_path: Path) -> None:
    log = tmp_path / "logs" / "rot.log"
    log.write_text("")

    async def writer() -> None:
        await asyncio.sleep(0.1)
        with log.open("a") as fh:
            fh.write("one\n")
        await asyncio.sleep(0.15)
        log.rename(tmp_path / "logs" / "rot.log.1")  # rotation: new inode
        log.write_text("two\n")
        await asyncio.sleep(0.15)
        log.write_text("3\n")  # truncation: smaller than what was read
        await asyncio.sleep(0.15)
        with log.open("a") as fh:
            fh.write("END\n")

    task = asyncio.create_task(writer())
    _, _, events = await run_monitor(
        db, log_path=str(log), patterns=["one", "two", "3"], until="pattern:END"
    )
    await task
    assert [e["text"] for e in kinds(events, "line")] == ["one", "two", "3", "END"]
    assert len(kinds(events, "note")) >= 2


async def test_long_line_text_truncated(db: Persistence, tmp_path: Path) -> None:
    log = tmp_path / "logs" / "long.log"
    log.write_text("x" * 5000 + "\n")
    _, _, events = await run_monitor(
        db, log_path=str(log), patterns=["x"], until="pattern:x", from_start=True
    )
    assert len(kinds(events, "line")[0]["text"]) == monitor.MAX_LINE_CHARS


async def test_invalid_utf8_is_replaced(db: Persistence, tmp_path: Path) -> None:
    log = tmp_path / "logs" / "bin.log"
    log.write_bytes(b"bad \xff\xfe line\n")
    _, _, events = await run_monitor(
        db, log_path=str(log), patterns=["bad"], until="pattern:bad", from_start=True
    )
    assert "�" in kinds(events, "line")[0]["text"]


async def test_symlink_swapped_in_after_start_is_rejected(db: Persistence, tmp_path: Path) -> None:
    log = tmp_path / "logs" / "swap.log"

    async def writer() -> None:
        await asyncio.sleep(0.1)
        os.symlink("/etc/passwd", log)

    task = asyncio.create_task(writer())
    _, job, events = await run_monitor(db, log_path=str(log), patterns=["."], until="log_idle:5")
    await task
    assert job["status"] == "failed"
    assert terminal(events)["reason"] == "log_path_not_allowed"
    assert kinds(events, "line") == []


async def test_unexpected_exception_is_failed_with_traceback(
    db: Persistence, monkeypatch, tmp_path: Path
) -> None:
    def boom(self: Any) -> bool:
        raise ValueError("kaboom")

    monkeypatch.setattr(monitor._Monitor, "_drain_log", boom)
    log = tmp_path / "logs" / "x.log"
    log.write_text("")
    _, job, events = await run_monitor(db, log_path=str(log), until="log_idle:5")
    assert job["status"] == "failed"
    assert terminal(events)["reason"] == "monitor_error: ValueError"
    assert "kaboom" in Path(job["stderr_path"]).read_text()


# --------------------------------------------------------------------------
# request validation, presets
# --------------------------------------------------------------------------


async def start(db: Persistence, **kwargs: Any) -> ShellMonitorResponse:
    return await monitor.start_monitor(ShellMonitorRequest(agent_id="t", **kwargs), db)


async def test_neither_unit_nor_log_is_error(db: Persistence) -> None:
    resp = await start(db, until="log_idle:1")
    assert resp.status == "error"
    assert "unit or log_path" in (resp.error or "")
    assert resp.job_id is None


async def test_unit_exit_without_unit_is_error(db: Persistence, tmp_path: Path) -> None:
    resp = await start(db, log_path=str(tmp_path / "logs" / "a.log"), until="unit_exit")
    assert resp.status == "error"
    assert "unit_exit" in (resp.error or "")


async def test_until_required_without_unit(db: Persistence, tmp_path: Path) -> None:
    resp = await start(db, log_path=str(tmp_path / "logs" / "a.log"))
    assert resp.status == "error"
    assert "until" in (resp.error or "")


@pytest.mark.parametrize("until", ["bogus", "log_idle:abc", "log_idle:0", "pattern:", "pattern:("])
async def test_bad_until_is_error(db: Persistence, tmp_path: Path, until: str) -> None:
    resp = await start(db, log_path=str(tmp_path / "logs" / "a.log"), until=until)
    assert resp.status == "error"


@pytest.mark.parametrize("unit", ["bad unit", "a/b", "$(x)", "x" * 300, ""])
async def test_bad_unit_is_error(db: Persistence, unit: str) -> None:
    resp = await start(db, unit=unit)
    assert resp.status == "error"


async def test_unit_suffix_normalisation(db: Persistence, monkeypatch) -> None:
    fake_systemctl(monkeypatch, props("inactive", "dead"))
    resp, _, _ = await run_monitor(db, unit="my.timer", unit_grace_s=0)
    assert resp.unit_resolved == "my.timer"
    resp, _, _ = await run_monitor(db, unit="my@inst", unit_grace_s=0)
    assert resp.unit_resolved == "my@inst.service"


async def test_bad_regex_is_error(db: Persistence, tmp_path: Path) -> None:
    resp = await start(db, log_path=str(tmp_path / "logs" / "a.log"), patterns=["("], until="x")
    assert resp.status == "error"
    assert "regex" in (resp.error or "")


async def test_pattern_limits(db: Persistence, tmp_path: Path) -> None:
    log = str(tmp_path / "logs" / "a.log")
    too_many = await start(db, log_path=log, patterns=["a"] * 21, until="log_idle:1")
    too_long = await start(db, log_path=log, patterns=["a" * 501], until="log_idle:1")
    assert too_many.status == "error"
    assert too_long.status == "error"


async def test_builtin_preset_matches(db: Persistence, tmp_path: Path) -> None:
    log = tmp_path / "logs" / "p.log"
    log.write_text("fine\nFATAL: it broke\n=== 3 passed in 1s ===\n")
    _, _, events = await run_monitor(
        db, log_path=str(log), preset="errors", until="log_idle:0.1", from_start=True
    )
    assert [e["text"] for e in kinds(events, "line")] == ["FATAL: it broke"]


async def test_pytest_preset(db: Persistence, tmp_path: Path) -> None:
    log = tmp_path / "logs" / "py.log"
    log.write_text("x\n=== 3 passed in 1s ===\n")
    _, _, events = await run_monitor(
        db, log_path=str(log), preset="pytest", until="log_idle:0.1", from_start=True
    )
    assert len(kinds(events, "line")) == 1


def _write_toml(tmp_path: Path, body: str) -> None:
    cfg = tmp_path / "cfg" / "shell-runner"
    cfg.mkdir(parents=True)
    (cfg / "monitor.toml").write_text(body)


async def test_toml_preset_overrides_builtin_and_adds_new(db: Persistence, tmp_path: Path) -> None:
    _write_toml(
        tmp_path,
        '[presets.errors]\npatterns = ["BOOM"]\n[presets.hb]\npatterns = ["gated green"]\n',
    )
    log = tmp_path / "logs" / "o.log"
    log.write_text("fatal error\nBOOM here\ngated green\n")
    _, _, events = await run_monitor(
        db, log_path=str(log), preset="errors", until="log_idle:0.1", from_start=True
    )
    assert [e["text"] for e in kinds(events, "line")] == ["BOOM here"]
    _, _, events = await run_monitor(
        db, log_path=str(log), preset="hb", until="log_idle:0.1", from_start=True
    )
    assert [e["text"] for e in kinds(events, "line")] == ["gated green"]


async def test_unknown_preset_lists_available(db: Persistence, tmp_path: Path) -> None:
    resp = await start(db, log_path=str(tmp_path / "logs" / "a.log"), preset="nope", until="x")
    assert resp.status == "error"
    assert "errors" in (resp.error or "")
    assert "pytest" in (resp.error or "")


async def test_malformed_toml_is_ignored(db: Persistence, tmp_path: Path) -> None:
    _write_toml(tmp_path, "this is [not toml")
    log = tmp_path / "logs" / "a.log"
    log.write_text("")
    resp = await start(db, log_path=str(log), until="log_idle:0.1")
    assert resp.status == "running"
    await wait_done(db, resp.job_id or "")


# --------------------------------------------------------------------------
# log_path allowlist
# --------------------------------------------------------------------------


def test_outside_roots_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SHELL_RUNNER_MONITOR_LOG_DIRS", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(executor, "_CWD_ROOTS", [tmp_path / "cwd"])
    path, err = monitor.check_log_path("/var/tmp/x/app.log", {})
    assert path is None
    assert err is not None
    assert "log_path not allowed" in err
    assert str(tmp_path / "cwd") in err
    assert "/tmp" in err


def test_tmp_and_cwd_roots_allowed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SHELL_RUNNER_MONITOR_LOG_DIRS", raising=False)
    monkeypatch.setattr(executor, "_CWD_ROOTS", [tmp_path / "cwd"])
    assert monitor.check_log_path("/tmp/x/app.log", {})[1] is None
    assert monitor.check_log_path(str(tmp_path / "cwd" / "sub" / "a.log"), {})[1] is None


def test_env_and_toml_dirs_allowed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(executor, "_CWD_ROOTS", [])
    monkeypatch.setenv("SHELL_RUNNER_MONITOR_LOG_DIRS", f"{tmp_path / 'e1'}{os.pathsep}/var/tmp/e2")
    assert monitor.check_log_path(str(tmp_path / "e1" / "a.log"), {})[1] is None
    assert monitor.check_log_path("/var/tmp/e2/a.log", {})[1] is None
    assert monitor.check_log_path("/var/tmp/e3/a.log", {})[1] is not None
    assert monitor.check_log_path("/var/tmp/e3/a.log", {"log_dirs": ["/var/tmp/e3"]})[1] is None


@pytest.mark.parametrize(
    "rel",
    [".ssh/id_rsa", "a/.gnupg/x.log", ".aws/credentials", ".config/gh/hosts.yml"],
)
def test_deny_list_wins_inside_allowed_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rel: str
) -> None:
    monkeypatch.setenv("SHELL_RUNNER_MONITOR_LOG_DIRS", str(tmp_path))
    path, err = monitor.check_log_path(str(tmp_path / rel), {})
    assert path is None
    assert err is not None
    assert "not allowed" in err


@pytest.mark.parametrize("path", ["/etc/passwd", "/proc/1/environ", "/root/x.log", "/sys/x"])
def test_system_dirs_denied_even_if_allowed(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    monkeypatch.setenv("SHELL_RUNNER_MONITOR_LOG_DIRS", "/")
    assert monitor.check_log_path(path, {})[0] is None


def test_symlink_escape_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHELL_RUNNER_MONITOR_LOG_DIRS", str(tmp_path))
    (tmp_path / "ok").mkdir()
    (tmp_path / "ok" / "to_etc").symlink_to("/etc/passwd")
    (tmp_path / "ok" / "to_out").symlink_to("/var/tmp/shell-monitor-escape.log")
    assert monitor.check_log_path(str(tmp_path / "ok" / "to_etc"), {})[0] is None
    assert monitor.check_log_path(str(tmp_path / "ok" / "to_out"), {})[0] is None


async def test_start_rejects_disallowed_path_without_creating_job(
    db: Persistence, tmp_path: Path
) -> None:
    resp = await start(db, log_path="/etc/passwd", patterns=["root"], until="log_idle:1")
    assert resp.status == "error"
    assert "log_path not allowed" in (resp.error or "")
    assert resp.allowed_roots
    assert resp.job_id is None
    assert db.list_running_jobs() == []


# --------------------------------------------------------------------------
# kill
# --------------------------------------------------------------------------


async def test_kill_cancels_monitor_and_writes_killed_terminal(
    db: Persistence, monkeypatch
) -> None:
    fake_systemctl(monkeypatch, props())
    resp = await start(db, unit="forever")
    assert resp.job_id is not None
    await asyncio.sleep(0.05)

    def no_kill(*_a: Any) -> None:
        raise AssertionError("os.kill must not be called for a monitor job")

    monkeypatch.setattr(server_module.os, "kill", no_kill)
    result = await server_module.shell_kill_route(ShellKillRequest(job_id=resp.job_id))
    assert result.killed is True
    job = db.get_job(resp.job_id)
    assert job is not None
    assert job["status"] == "killed"
    assert terminal(monitor.read_events(resp.events_path or ""))["state"] == "killed"


async def test_kill_of_unregistered_pid_none_job_just_marks_killed(
    db: Persistence, monkeypatch, tmp_path: Path
) -> None:
    fake_systemctl(monkeypatch, props())
    resp = await start(db, unit="forever")
    assert resp.job_id is not None
    # simulate a server restart: the task is gone from the registry
    task = monitor._tasks.pop(resp.job_id)
    task.cancel()
    await asyncio.sleep(0.05)
    with db._write_conn() as conn:
        conn.execute("UPDATE jobs SET status = 'running' WHERE job_id = ?", (resp.job_id,))

    def no_kill(*_a: Any) -> None:
        raise AssertionError("os.kill must not be called when pid is None")

    monkeypatch.setattr(server_module.os, "kill", no_kill)
    result = await server_module.shell_kill_route(ShellKillRequest(job_id=resp.job_id))
    assert result.killed is True
    job = db.get_job(resp.job_id)
    assert job is not None
    assert job["status"] == "killed"


async def test_reconcile_orphans_kills_pidless_running_jobs(
    db: Persistence, monkeypatch, tmp_path: Path
) -> None:
    fake_systemctl(monkeypatch, props())
    resp = await start(db, unit="forever")
    assert resp.job_id is not None
    assert resp.events_path is not None
    # simulate a crash: the task vanishes without finalizing, the row stays running
    task = monitor._tasks.pop(resp.job_id)
    task.cancel()
    await asyncio.sleep(0.05)
    with db._write_conn() as conn:
        conn.execute("UPDATE jobs SET status = 'running' WHERE job_id = ?", (resp.job_id,))
    # a normal job (has a pid) must be left alone
    with db._write_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, telemetry_id, raw_cmd, cwd, agent_id, started_at, "
            "status, pid, timeout_s) "
            "SELECT 'real', telemetry_id, raw_cmd, cwd, agent_id, started_at, 'running', 4242, 5 "
            "FROM jobs WHERE job_id = ?",
            (resp.job_id,),
        )
    before = len(monitor.read_events(resp.events_path))

    assert monitor.reconcile_orphans(db) == 1

    job = db.get_job(resp.job_id)
    assert job is not None
    assert job["status"] == "killed"
    assert job["finished_at"]
    events = monitor.read_events(resp.events_path)
    assert len(events) == before + 1
    assert events[-1]["state"] == "killed"
    assert events[-1]["reason"] == "server_restarted"
    real = db.get_job("real")
    assert real is not None
    assert real["status"] == "running"
    assert monitor.reconcile_orphans(db) == 0


def test_read_events_caps_to_most_recent(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("".join(f'{{"kind": "line", "n": {i}}}\n' for i in range(10)) + "garbage\n")
    events = monitor.read_events(str(path), limit=3)
    assert [e["n"] for e in events] == [7, 8, 9]
    assert monitor.read_events(str(tmp_path / "missing"), limit=3) == []
