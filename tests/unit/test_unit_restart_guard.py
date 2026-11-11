"""mcp_restart's running-jobs guard ignores detached units (issue #81, step 8).

Units are transient systemd services that survive a shell-runner restart by
design, so they must not make the restart refuse. In-process jobs still do.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

from shell_runner import restart
from shell_runner import server as srv
from shell_runner.persistence import Persistence
from shell_runner.restart import RestartEntry

AGENT = "focused-code-modifier"


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Persistence:
    fresh = Persistence(db_path=tmp_path / "test.sqlite3")
    monkeypatch.setattr(srv, "db", fresh)
    return fresh


@pytest.fixture
def scheduled(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Registry with a self-restarting shell-runner; restart._run is recorded, never executed."""
    reg = {
        "shell-runner": RestartEntry(
            name="shell-runner",
            unit="shell-runner.service",
            repo="/tmp/sr",
            port=4111,
            self_restart=True,
        )
    }
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(restart, "load_registry", lambda path=None: reg)
    monkeypatch.setattr(restart, "_run", fake_run)
    monkeypatch.setattr(restart.time, "sleep", lambda _s: None)
    return calls


def _running(db: Persistence, kind: str, pid: int | None) -> str:
    job_id = str(uuid.uuid4())
    telemetry_id = db.record_call(
        agent_id=AGENT,
        cwd="/tmp",
        raw_cmd="long job",
        normalized_template="long job",
        command_tier=1,
        final_tier=1,
        decision="running",
        matched_rule_pattern=None,
        matched_rule_category=None,
        exit_code=None,
        stdout_bytes=0,
        stderr_bytes=0,
        duration_ms=0,
        decision_path=["x"],
        normalizer_warnings=[],
    )
    db.create_job(
        job_id=job_id,
        telemetry_id=telemetry_id,
        raw_cmd="long job",
        cwd="/tmp",
        agent_id=AGENT,
        timeout_s=10,
        stdout_path="/tmp/o",
        stderr_path="/tmp/e",
        pid=pid,
        kind=kind,
    )
    return job_id


def test_summary_excludes_units_keeps_process_and_monitor(db: Persistence) -> None:
    _running(db, "unit", None)
    proc = _running(db, "process", 1)
    mon = _running(db, "monitor", None)
    assert {j["job_id"] for j in srv._running_jobs_summary()} == {proc, mon}


async def test_only_unit_jobs_running_does_not_refuse_restart(
    db: Persistence, scheduled: list[list[str]]
) -> None:
    _running(db, "unit", None)
    _running(db, "unit", None)
    res = await srv._mcp_restart_tool({"server": "shell-runner", "skip_preflight": True})
    assert res["status"] != "refused"
    assert scheduled  # the restart was actually scheduled


async def test_running_process_job_still_refuses_restart(
    db: Persistence, scheduled: list[list[str]]
) -> None:
    _running(db, "unit", None)
    proc = _running(db, "process", 1)
    res = await srv._mcp_restart_tool({"server": "shell-runner", "skip_preflight": True})
    assert res["status"] == "refused"
    assert [j["job_id"] for j in res["running_jobs"]] == [proc]
    assert scheduled == []
