"""Unit tests for jobs persistence methods."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from shell_runner.persistence import Persistence


def _make_db(tmp_path: Path) -> Persistence:
    return Persistence(db_path=tmp_path / "test.sqlite3")


def _insert_shell_call(db: Persistence) -> str:
    """Insert a minimal shell_calls row and return its telemetry_id."""
    return db.record_call(
        agent_id="test-agent",
        cwd="/tmp",
        raw_cmd="echo hi",
        normalized_template="echo <word>",
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


def _create_job(db: Persistence, telemetry_id: str, **overrides: object) -> str:
    job_id = "test-job-" + str(id(overrides))
    kwargs: dict[str, object] = dict(
        job_id=job_id,
        telemetry_id=telemetry_id,
        raw_cmd="echo hi",
        cwd="/tmp",
        agent_id="test-agent",
        timeout_s=30,
        stdout_path="/tmp/stdout.log",
        stderr_path="/tmp/stderr.log",
        pid=12345,
    )
    kwargs.update(overrides)
    db.create_job(**kwargs)  # type: ignore[arg-type]
    return str(kwargs["job_id"])


# ---------------------------------------------------------------------------
# create_job
# ---------------------------------------------------------------------------


def test_create_job_inserts_running_row(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    tid = _insert_shell_call(db)
    job_id = _create_job(db, tid)

    job = db.get_job(job_id)
    assert job is not None
    assert job["job_id"] == job_id
    assert job["status"] == "running"
    assert job["telemetry_id"] == tid
    assert job["pid"] == 12345
    assert job["finished_at"] is None
    assert job["exit_code"] is None


# ---------------------------------------------------------------------------
# update_job_status
# ---------------------------------------------------------------------------


def test_update_job_status_transitions_to_completed(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    tid = _insert_shell_call(db)
    job_id = _create_job(db, tid)

    db.update_job_status(
        job_id, status="completed", exit_code=0, finished_at="2026-01-01T00:00:00+00:00"
    )

    job = db.get_job(job_id)
    assert job is not None
    assert job["status"] == "completed"
    assert job["exit_code"] == 0
    assert job["finished_at"] == "2026-01-01T00:00:00+00:00"


def test_update_job_status_transitions_to_timed_out(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    tid = _insert_shell_call(db)
    job_id = _create_job(db, tid)

    db.update_job_status(
        job_id, status="timed_out", exit_code=-1, finished_at="2026-01-01T00:01:00+00:00"
    )

    job = db.get_job(job_id)
    assert job is not None
    assert job["status"] == "timed_out"
    assert job["exit_code"] == -1


# ---------------------------------------------------------------------------
# get_job
# ---------------------------------------------------------------------------


def test_get_job_returns_none_for_unknown_id(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    assert db.get_job("nonexistent-id") is None


def test_get_job_returns_dict(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    tid = _insert_shell_call(db)
    job_id = _create_job(db, tid)

    result = db.get_job(job_id)
    assert isinstance(result, dict)
    assert result["job_id"] == job_id


# ---------------------------------------------------------------------------
# list_running_jobs
# ---------------------------------------------------------------------------


def test_list_running_jobs_filters_correctly(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    tid1 = _insert_shell_call(db)
    tid2 = _insert_shell_call(db)
    tid3 = _insert_shell_call(db)

    running_id = _create_job(db, tid1, job_id="job-running")
    completed_id = _create_job(db, tid2, job_id="job-completed")
    killed_id = _create_job(db, tid3, job_id="job-killed")

    db.update_job_status(
        completed_id, status="completed", exit_code=0, finished_at="2026-01-01T00:00:00+00:00"
    )
    db.update_job_status(killed_id, status="killed", finished_at="2026-01-01T00:00:00+00:00")

    running = db.list_running_jobs()
    running_ids = [j["job_id"] for j in running]

    assert running_id in running_ids
    assert completed_id not in running_ids
    assert killed_id not in running_ids


def test_list_running_jobs_empty_when_none_running(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    tid = _insert_shell_call(db)
    job_id = _create_job(db, tid, job_id="job-done")
    db.update_job_status(
        job_id, status="completed", exit_code=0, finished_at="2026-01-01T00:00:01+00:00"
    )

    assert db.list_running_jobs() == []


# ---------------------------------------------------------------------------
# cleanup_old_jobs
# ---------------------------------------------------------------------------


def test_cleanup_old_jobs_removes_old_rows_and_returns_them(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    tid1 = _insert_shell_call(db)
    tid2 = _insert_shell_call(db)

    old_id = _create_job(db, tid1, job_id="job-old")
    new_id = _create_job(db, tid2, job_id="job-new")

    # Force the old job's started_at into the distant past
    ancient = "2020-01-01T00:00:00+00:00"
    with db._conn() as conn:
        conn.execute("UPDATE jobs SET started_at = ? WHERE job_id = ?", (ancient, old_id))

    removed = db.cleanup_old_jobs(retention_seconds=60)

    removed_ids = [r["job_id"] for r in removed]
    assert old_id in removed_ids
    assert new_id not in removed_ids

    # Verify row is gone
    assert db.get_job(old_id) is None
    # New job still present
    assert db.get_job(new_id) is not None


def test_cleanup_old_jobs_returns_empty_when_nothing_stale(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    tid = _insert_shell_call(db)
    _create_job(db, tid, job_id="job-fresh")

    removed = db.cleanup_old_jobs(retention_seconds=3600)
    assert removed == []


def test_cleanup_old_jobs_includes_file_paths_in_returned_rows(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    tid = _insert_shell_call(db)
    stdout_p = str(tmp_path / "stdout.log")
    stderr_p = str(tmp_path / "stderr.log")
    job_id = _create_job(db, tid, job_id="job-stale", stdout_path=stdout_p, stderr_path=stderr_p)

    ancient = "2020-01-01T00:00:00+00:00"
    with db._conn() as conn:
        conn.execute("UPDATE jobs SET started_at = ? WHERE job_id = ?", (ancient, job_id))

    removed = db.cleanup_old_jobs(retention_seconds=60)
    assert len(removed) == 1
    assert removed[0]["stdout_path"] == stdout_p
    assert removed[0]["stderr_path"] == stderr_p
