"""In-process write serialisation for Persistence (issue #53)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from shell_runner import persistence
from shell_runner.persistence import Persistence

# Writes are serialised and each opens a fresh WAL connection (slow on some
# disks), so keep the total modest to stay inside the per-test timeout.
THREADS = 20
CALLS_PER_THREAD = 6


def _record(p: Persistence, i: int) -> str:
    return p.record_call(
        agent_id="agent",
        cwd="/tmp",
        raw_cmd=f"ls /tmp/{i}",
        normalized_template="ls <path>",
        command_tier=1,
        final_tier=1,
        decision="allow",
        matched_rule_pattern=None,
        matched_rule_category=None,
        exit_code=0,
        stdout_bytes=0,
        stderr_bytes=0,
        duration_ms=1,
        decision_path=["t"],
        normalizer_warnings=[],
    )


def test_concurrent_record_call_never_hits_busy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(persistence, "BUSY_TIMEOUT_MS", 50, raising=False)
    p = Persistence(tmp_path / "db.sqlite")
    errors: list[BaseException] = []
    barrier = threading.Barrier(THREADS)

    def worker() -> None:
        barrier.wait()
        for i in range(CALLS_PER_THREAD):
            try:
                _record(p, i)
            except Exception as exc:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    with p._conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM shell_calls").fetchone()[0]
    assert count == THREADS * CALLS_PER_THREAD


def test_instances_on_same_db_share_lock(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    a, b = Persistence(db), Persistence(str(db))
    assert a._write_lock is b._write_lock


def test_lock_released_after_exception(tmp_path: Path) -> None:
    p = Persistence(tmp_path / "db.sqlite")
    try:
        with p._write_conn():
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    done: list[str] = []
    t = threading.Thread(target=lambda: done.append(_record(p, 0)))
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()
    assert len(done) == 1


def test_write_conn_reuses_same_connection(tmp_path: Path) -> None:
    p = Persistence(tmp_path / "db.sqlite")
    with p._write_conn() as first:
        pass
    _record(p, 0)
    with p._write_conn() as second:
        pass
    assert first is second


def test_write_conn_pragmas(tmp_path: Path) -> None:
    p = Persistence(tmp_path / "db.sqlite")
    with p._write_conn() as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_exception_in_transaction_rolls_back_shared_conn(tmp_path: Path) -> None:
    p = Persistence(tmp_path / "db.sqlite")
    try:
        with p._write_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert conn.in_transaction is False
    assert _record(p, 1)


def test_close_then_write_reopens(tmp_path: Path) -> None:
    p = Persistence(tmp_path / "db.sqlite")
    with p._write_conn() as old:
        pass
    p.close()
    p.close()  # idempotent
    assert _record(p, 2)
    with p._write_conn() as new:
        pass
    assert new is not old


def test_nested_inner_exception_does_not_roll_back_outer(tmp_path: Path) -> None:
    p = Persistence(tmp_path / "db.sqlite")
    with p._write_conn() as outer:
        outer.execute("CREATE TABLE IF NOT EXISTS t53 (x)")
        outer.execute("BEGIN IMMEDIATE")
        outer.execute("INSERT INTO t53 VALUES (1)")
        try:
            with p._write_conn() as inner:
                assert inner is outer
                raise RuntimeError("inner")
        except RuntimeError:
            pass
        assert outer.in_transaction is True
        outer.execute("COMMIT")
    with p._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM t53").fetchone()[0] == 1


def test_nested_exception_propagating_to_outer_rolls_back(tmp_path: Path) -> None:
    p = Persistence(tmp_path / "db.sqlite")
    try:
        with p._write_conn() as outer:
            outer.execute("BEGIN IMMEDIATE")
            with p._write_conn():
                raise RuntimeError("inner")
    except RuntimeError:
        pass
    assert outer.in_transaction is False


def _fresh_count(p: Persistence, table: str) -> int:
    fresh = sqlite3.connect(p.db_path)
    try:
        return fresh.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        fresh.close()


def test_consume_early_return_leaves_no_open_transaction(tmp_path: Path) -> None:
    p = Persistence(tmp_path / "db.sqlite")
    assert p.consume_approve_token(token="unknown") is None
    assert p.consume_approval_by_command(raw_cmd="x", cwd="/", agent_id="a") is None
    with p._write_conn() as conn:
        assert conn.in_transaction is False
    _record(p, 0)
    assert _fresh_count(p, "shell_calls") == 1


def test_normal_exit_with_open_transaction_is_rolled_back(tmp_path: Path, caplog) -> None:
    p = Persistence(tmp_path / "db.sqlite")
    with p._write_conn() as conn:
        conn.execute("CREATE TABLE t53b (x)")
    with caplog.at_level("WARNING"), p._write_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO t53b VALUES (1)")
    assert conn.in_transaction is False
    assert _fresh_count(p, "t53b") == 0
    assert "open transaction" in caplog.text


def test_nested_normal_exit_does_not_roll_back_outer(tmp_path: Path) -> None:
    p = Persistence(tmp_path / "db.sqlite")
    with p._write_conn() as outer:
        outer.execute("CREATE TABLE t53c (x)")
        outer.execute("BEGIN IMMEDIATE")
        outer.execute("INSERT INTO t53c VALUES (1)")
        with p._write_conn():
            pass
        assert outer.in_transaction is True
        outer.execute("COMMIT")
    assert _fresh_count(p, "t53c") == 1


def test_nested_write_conn_is_reentrant(tmp_path: Path) -> None:
    p = Persistence(tmp_path / "db.sqlite")
    finished = threading.Event()

    def nested() -> None:
        with p._write_conn(), p._write_conn():
            pass
        finished.set()

    t = threading.Thread(target=nested, daemon=True)
    t.start()
    t.join(timeout=5)
    assert finished.is_set()
