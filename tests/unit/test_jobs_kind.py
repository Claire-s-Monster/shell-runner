"""jobs.kind column (process|monitor|unit): migration, backfill, validation, orphan sweep."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from shell_runner.persistence import Persistence

_OLD_JOBS = """
CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY, telemetry_id TEXT NOT NULL, raw_cmd TEXT NOT NULL,
    cwd TEXT NOT NULL, agent_id TEXT NOT NULL, started_at TEXT NOT NULL,
    finished_at TEXT, status TEXT NOT NULL, exit_code INTEGER, pid INTEGER,
    stdout_path TEXT, stderr_path TEXT, timeout_s INTEGER
);
"""


def _insert_old(conn: sqlite3.Connection, job_id: str, pid: int | None) -> None:
    conn.execute(
        "INSERT INTO jobs (job_id, telemetry_id, raw_cmd, cwd, agent_id, started_at,"
        " status, pid) VALUES (?, 't', 'x', '/tmp', 'a', 'now', 'running', ?)",
        (job_id, pid),
    )


def _kind(path: Path, job_id: str) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT kind FROM jobs WHERE job_id = ?", (job_id,)).fetchone()[0]
    finally:
        conn.close()


def _tid(db: Persistence) -> str:
    return db.record_call(
        agent_id="a",
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
        decision_path=["background"],
        normalizer_warnings=[],
    )


def _job(db: Persistence, job_id: str, pid: int | None, **kw: object) -> None:
    db.create_job(
        job_id=job_id,
        telemetry_id=_tid(db),
        raw_cmd="x",
        cwd="/tmp",
        agent_id="a",
        timeout_s=30,
        stdout_path="/tmp/o",
        stderr_path="/tmp/e",
        pid=pid,
        **kw,  # type: ignore[arg-type]
    )


def test_old_schema_gets_kind_and_backfills_monitors(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_JOBS)
    _insert_old(conn, "mon", None)
    _insert_old(conn, "proc", 42)
    conn.commit()
    conn.close()

    Persistence(db_path=path)

    assert _kind(path, "mon") == "monitor"
    assert _kind(path, "proc") == "process"


def test_backfill_does_not_run_on_second_migration(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_JOBS)
    conn.commit()
    conn.close()
    Persistence(db_path=path)

    # A unit job legitimately has pid NULL; a re-migration must not relabel it.
    conn = sqlite3.connect(path)
    _insert_old(conn, "unit1", None)
    conn.execute("UPDATE jobs SET kind = 'unit' WHERE job_id = 'unit1'")
    conn.commit()
    conn.close()

    Persistence(db_path=path)

    assert _kind(path, "unit1") == "unit"


def test_create_job_default_and_explicit_kind(tmp_path: Path) -> None:
    path = tmp_path / "t.sqlite3"
    db = Persistence(db_path=path)
    _job(db, "p", 5)
    _job(db, "m", None, kind="monitor")
    _job(db, "u", None, kind="unit")
    assert [_kind(path, j) for j in ("p", "m", "u")] == ["process", "monitor", "unit"]


def test_create_job_rejects_invalid_kind(tmp_path: Path) -> None:
    db = Persistence(db_path=tmp_path / "t.sqlite3")
    with pytest.raises(ValueError, match="kind"):
        _job(db, "bad", None, kind="bogus")


def test_orphan_sweep_uses_kind_not_pid(tmp_path: Path) -> None:
    db = Persistence(db_path=tmp_path / "t.sqlite3")
    _job(db, "m", None, kind="monitor")
    _job(db, "u", None, kind="unit")  # pid NULL but must survive a restart
    _job(db, "p", 7, kind="process")

    swept = db.kill_orphaned_monitor_jobs()

    assert [r["job_id"] for r in swept] == ["m"]
    assert db.get_job("m")["status"] == "killed"  # type: ignore[index]
    assert db.get_job("u")["status"] == "running"  # type: ignore[index]
    assert db.get_job("p")["status"] == "running"  # type: ignore[index]
