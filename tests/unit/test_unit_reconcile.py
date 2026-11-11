"""reconcile_units + retention for kind='unit' jobs (issue #81, step 7).

A FakeUnitBackend stands in for systemd; nothing here spawns a unit.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

import pytest

from shell_runner import executor
from shell_runner import server as srv
from shell_runner.persistence import Persistence
from shell_runner.unit_runner import UnitResult

AGENT = "focused-code-modifier"
OLD = "2020-01-01T00:00:00+00:00"


class FakeUnitBackend:
    def __init__(self) -> None:
        self.active: set[str] = set()
        self.results: dict[str, UnitResult] = {}
        self.fail_active: Exception | None = None
        self.killed: list[str] = []

    async def start(self, argv: list[str]) -> None:
        return None

    async def kill(self, job_id: str) -> None:
        self.killed.append(job_id)

    async def active_units(self) -> set[str]:
        if self.fail_active is not None:
            raise self.fail_active
        return set(self.active)

    def read_result(self, job_dir: Path) -> UnitResult | None:
        return self.results.get(Path(job_dir).name)


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Persistence:
    fresh = Persistence(db_path=tmp_path / "test.sqlite3")
    monkeypatch.setattr(srv, "db", fresh)
    return fresh


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> FakeUnitBackend:
    fake = FakeUnitBackend()
    monkeypatch.setattr(executor, "_unit_backend", fake)
    return fake


def _job(
    db: Persistence,
    tmp_path: Path,
    kind: str = "unit",
    *,
    started_at: str | None = None,
    status: str = "running",
) -> str:
    job_id = str(uuid.uuid4())
    telemetry_id = db.record_call(
        agent_id=AGENT,
        cwd="/tmp",
        raw_cmd="x",
        normalized_template="x",
        command_tier=1,
        final_tier=1,
        decision="running",
        matched_rule_pattern=None,
        matched_rule_category=None,
        exit_code=None,
        stdout_bytes=0,
        stderr_bytes=0,
        duration_ms=0,
        decision_path=["detached"],
        normalizer_warnings=[],
    )
    job_dir = tmp_path / "jobs" / job_id
    job_dir.mkdir(parents=True)
    db.create_job(
        job_id=job_id,
        telemetry_id=telemetry_id,
        raw_cmd="x",
        cwd="/tmp",
        agent_id=AGENT,
        timeout_s=10,
        stdout_path=str(job_dir / "stdout.log"),
        stderr_path=str(job_dir / "stderr.log"),
        pid=None if kind != "process" else 1,
        kind=kind,
    )
    if started_at is not None:
        with db._write_conn() as conn:
            conn.execute("UPDATE jobs SET started_at = ? WHERE job_id = ?", (started_at, job_id))
    if status != "running":
        db.update_job_status(job_id, status=status, exit_code=0, finished_at=OLD)
    return job_id


def _row(db: Persistence, job_id: str) -> dict:
    job = db.get_job(job_id)
    assert job is not None
    return job


def _decision(db: Persistence, job_id: str) -> str:
    with db._conn() as conn:
        row = conn.execute(
            "SELECT decision FROM shell_calls WHERE id = ?", (_row(db, job_id)["telemetry_id"],)
        ).fetchone()
    return row[0]


# --- the three branches ---------------------------------------------------------


async def test_active_unit_stays_running(
    db: Persistence, backend: FakeUnitBackend, tmp_path: Path
) -> None:
    job_id = _job(db, tmp_path)
    backend.active = {job_id}
    assert await executor.reconcile_units(db, backend) == 0
    assert _row(db, job_id)["status"] == "running"


@pytest.mark.parametrize(
    ("result", "status", "exit_code"),
    [
        (UnitResult("0", "success"), "completed", 0),
        (UnitResult("2", "exit-code"), "failed", 2),
        (UnitResult("TERM", "timeout"), "timed_out", None),
    ],
)
async def test_gone_unit_with_result_becomes_terminal(
    db: Persistence,
    backend: FakeUnitBackend,
    tmp_path: Path,
    result: UnitResult,
    status: str,
    exit_code: int | None,
) -> None:
    job_id = _job(db, tmp_path)
    backend.results[job_id] = result
    assert await executor.reconcile_units(db, backend) == 1
    row = _row(db, job_id)
    assert (row["status"], row["exit_code"]) == (status, exit_code)
    assert row["finished_at"] is not None
    assert _decision(db, job_id) == "executed"


async def test_gone_unit_without_result_is_unit_missing(
    db: Persistence, backend: FakeUnitBackend, tmp_path: Path
) -> None:
    job_id = _job(db, tmp_path)
    assert await executor.reconcile_units(db, backend) == 1
    row = _row(db, job_id)
    assert row["status"] == "unit_missing"
    assert row["finished_at"] is not None
    assert _decision(db, job_id) == "executed"


async def test_only_running_unit_rows_are_considered(
    db: Persistence, backend: FakeUnitBackend, tmp_path: Path
) -> None:
    proc = _job(db, tmp_path, "process")
    mon = _job(db, tmp_path, "monitor")
    done = _job(db, tmp_path, status="completed")
    assert await executor.reconcile_units(db, backend) == 0
    assert _row(db, proc)["status"] == "running"
    assert _row(db, mon)["status"] == "running"
    assert _row(db, done)["status"] == "completed"


async def test_reconcile_does_not_overwrite_killed(
    db: Persistence, backend: FakeUnitBackend, tmp_path: Path
) -> None:
    job_id = _job(db, tmp_path, status="killed")
    backend.results[job_id] = UnitResult("0", "success")
    assert await executor.reconcile_units(db, backend) == 0
    assert _row(db, job_id)["status"] == "killed"


# --- backend errors change nothing ----------------------------------------------


@pytest.mark.parametrize("exc", [RuntimeError("bus down"), FileNotFoundError("systemctl")])
async def test_backend_error_leaves_rows_untouched(
    db: Persistence,
    backend: FakeUnitBackend,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    exc: Exception,
) -> None:
    gone = _job(db, tmp_path)  # would be 'unit_missing' if the backend were trusted
    finished = _job(db, tmp_path)
    backend.results[finished] = UnitResult("0", "success")
    backend.fail_active = exc
    with caplog.at_level(logging.WARNING):
        assert await executor.reconcile_units(db, backend) == 0
    assert _row(db, gone)["status"] == "running"
    assert _row(db, finished)["status"] == "running"
    assert any(r.levelno == logging.WARNING for r in caplog.records)

    backend.fail_active = None  # next tick succeeds
    assert await executor.reconcile_units(db, backend) == 2
    assert _row(db, gone)["status"] == "unit_missing"
    assert _row(db, finished)["status"] == "completed"


# --- wiring: periodic tick and lifespan -----------------------------------------


async def test_periodic_cleanup_tick_reconciles_units(
    db: Persistence,
    backend: FakeUnitBackend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = _job(db, tmp_path)
    ticks = 0

    async def one_tick(_: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(srv.asyncio, "sleep", one_tick)
    with pytest.raises(asyncio.CancelledError):
        await srv._periodic_cleanup(1)
    assert _row(db, job_id)["status"] == "unit_missing"


async def test_lifespan_reconciles_units(
    db: Persistence,
    backend: FakeUnitBackend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHELL_RUNNER_LOAD_SEED_APPROVALS", "0")
    job_id = _job(db, tmp_path)
    backend.results[job_id] = UnitResult("0", "success")
    async with srv._lifespan(srv.app):
        assert _row(db, job_id)["status"] == "completed"


# --- retention ------------------------------------------------------------------


def test_retention_skips_running_units_and_deletes_old_finished(
    db: Persistence, tmp_path: Path
) -> None:
    running_unit = _job(db, tmp_path, started_at=OLD)
    old_done_unit = _job(db, tmp_path, started_at=OLD, status="completed")
    old_process = _job(db, tmp_path, "process", started_at=OLD, status="completed")
    fresh_done = _job(db, tmp_path, status="completed")

    removed = {j["job_id"] for j in db.cleanup_old_jobs(60)}

    assert removed == {old_done_unit, old_process}
    assert db.get_job(running_unit) is not None
    assert db.get_job(fresh_done) is not None
    assert (tmp_path / "jobs" / running_unit).is_dir()
