"""create_or_reattach_prompt: identical open prompts are reused, not duplicated (issue #119)."""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from shell_runner.persistence import Persistence
from shell_runner.script_pin import UNPINNABLE

BASE: dict[str, Any] = {
    "agent_id": "agent-a",
    "cwd": "/tmp",
    "raw_cmd": "mytool --flag",
    "normalized_template": "mytool --flag",
    "command_tier": 3,
    "matched_rule_category": None,
    "decision_path": ["fallthrough"],
}
SHA_A = "a" * 64
SHA_B = "b" * 64


@pytest.fixture
def db(tmp_path: Path) -> Persistence:
    return Persistence(db_path=tmp_path / "test.sqlite3")


def _open(db: Persistence, **overrides: Any) -> tuple[dict, bool]:
    return db.create_or_reattach_prompt(**{**BASE, **overrides})


def _count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM pending_prompts").fetchone()[0]


def test_first_call_inserts_a_prompt(db: Persistence) -> None:
    row, reattached = _open(db)
    assert reattached is False
    assert row["raw_cmd"] == BASE["raw_cmd"]
    assert datetime.fromisoformat(row["expires_at"]) > datetime.now(UTC)
    assert _count(db.db_path) == 1


def test_identical_call_reattaches_and_keeps_expiry(db: Persistence) -> None:
    first, _ = _open(db)
    second, reattached = _open(db)
    assert reattached is True
    assert second["id"] == first["id"]
    assert second["expires_at"] == first["expires_at"]  # re-attach never extends expiry
    assert _count(db.db_path) == 1


@pytest.mark.parametrize(
    ("script_sha256", "script_paths"),
    [
        (None, None),
        (UNPINNABLE, None),
        (SHA_A, '{"/tmp/x.sh": "' + SHA_A + '"}'),
    ],
)
def test_identical_pin_reattaches(
    db: Persistence, script_sha256: str | None, script_paths: str | None
) -> None:
    pin = {"script_sha256": script_sha256, "script_paths": script_paths}
    first, _ = _open(db, **pin)
    second, reattached = _open(db, **pin)
    assert reattached is True
    assert second["id"] == first["id"]
    assert _count(db.db_path) == 1


@pytest.mark.parametrize(
    ("field", "first", "second"),
    [
        ("agent_id", "agent-a", "agent-b"),
        ("raw_cmd", "mytool --flag", "mytool --other"),
        ("cwd", "/tmp", "/var/tmp"),
        ("detach", False, True),
        ("script_sha256", None, SHA_A),
        ("script_sha256", SHA_A, SHA_B),
        ("script_sha256", UNPINNABLE, SHA_A),
        ("script_sha256", UNPINNABLE, None),
        ("script_paths", None, '{"/tmp/x.sh": "' + SHA_A + '"}'),
        ("normalized_template", "mytool --flag", "mytool <arg>"),
        ("command_tier", 3, 4),
        ("matched_rule_category", None, "package"),
        ("matched_rule_category", "package", "network"),
        # the #98 manifest marker (server.RUNNER_MANIFEST_NOT_APPROVED) ...
        ("decision_path", ["fallthrough"], ["fallthrough", "runner_manifest_not_approved"]),
        # ... and the #107 non-durable loader marker (classifier.py ~508)
        (
            "decision_path",
            ["fallthrough"],
            ["fallthrough", "non_durable: LD_PRELOAD; approve_once only"],
        ),
    ],
)
def test_any_mismatch_creates_a_new_prompt(
    db: Persistence, field: str, first: Any, second: Any
) -> None:
    a, _ = _open(db, **{field: first})
    b, reattached = _open(db, **{field: second})
    assert reattached is False
    assert b["id"] != a["id"]
    assert _count(db.db_path) == 2


def test_expired_prompt_is_not_reattached(db: Persistence) -> None:
    old = db.create_pending_prompt(**BASE, ttl_seconds=-1)
    row, reattached = _open(db)
    assert reattached is False
    assert row["id"] != old
    assert _count(db.db_path) == 2


@pytest.mark.parametrize("decision", ["approve_once", "approve_template", "deny"])
def test_decided_prompt_is_never_reattached(db: Persistence, decision: str) -> None:
    first, _ = _open(db)
    db.approve_prompt(prompt_id=first["id"], decision=decision)
    row, reattached = _open(db)
    assert reattached is False
    assert row["id"] != first["id"]


def test_consumed_prompt_is_never_reattached(db: Persistence) -> None:
    first, _ = _open(db)
    db.approve_prompt(prompt_id=first["id"], decision="approve_once")
    consumed = db.consume_approval_by_command(
        raw_cmd=BASE["raw_cmd"], cwd=BASE["cwd"], agent_id=BASE["agent_id"]
    )
    assert consumed is not None
    row, reattached = _open(db)
    assert reattached is False
    assert row["id"] != first["id"]


def test_create_pending_prompt_still_always_inserts(db: Persistence) -> None:
    a = db.create_pending_prompt(**BASE)
    b = db.create_pending_prompt(**BASE)
    assert a != b
    assert _count(db.db_path) == 2


def test_concurrent_threads_create_one_row(tmp_path: Path) -> None:
    """In-process only: same-path instances share one RLock, so this guards the lock,
    not BEGIN IMMEDIATE (see the cross-process test below)."""
    path = tmp_path / "test.sqlite3"
    Persistence(db_path=path)  # schema first, so the threads only race on the prompt
    barrier = threading.Barrier(8)
    results: list[tuple[str, bool]] = []
    lock = threading.Lock()

    def call() -> None:
        own = Persistence(db_path=path)
        barrier.wait()
        row, reattached = own.create_or_reattach_prompt(**BASE)
        with lock:
            results.append((row["id"], reattached))

    threads = [threading.Thread(target=call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 8
    assert len({pid for pid, _ in results}) == 1
    assert sorted(r for _, r in results) == [False] + [True] * 7
    assert _count(path) == 1


def _reattach_in_child(db_path: str, ready: Any, go: Any, entered: Any, results: Any) -> None:
    """Spawned child (top-level so spawn can pickle it). Its Persistence is built before
    the parent takes the write lock, so schema init never contends with it."""
    db = Persistence(db_path=db_path)
    ready.set()
    go.wait(timeout=10)
    entered.set()  # about to call create_or_reattach_prompt
    row, reattached = db.create_or_reattach_prompt(**BASE)
    results.put((row["id"], reattached))


def test_lookup_waits_for_a_concurrent_writer_across_processes(tmp_path: Path) -> None:
    """Deterministic: the child must block on BEGIN IMMEDIATE while the parent holds the
    write lock, then re-attach to the row the parent committed. A lookup outside the
    transaction would not block (WAL readers never wait), miss the row and insert a second."""
    path = tmp_path / "test.sqlite3"
    Persistence(db_path=path)  # schema
    ctx = multiprocessing.get_context("spawn")
    ready, go, entered = ctx.Event(), ctx.Event(), ctx.Event()
    results = ctx.Queue()
    child = ctx.Process(target=_reattach_in_child, args=(str(path), ready, go, entered, results))
    child.start()
    try:
        assert ready.wait(timeout=30), "child never initialised"
        writer = sqlite3.connect(path, isolation_level=None, timeout=5)
        try:
            writer.execute("BEGIN IMMEDIATE")  # the parent now holds the write lock
            go.set()
            assert entered.wait(timeout=10), "child never reached create_or_reattach_prompt"
            time.sleep(1.0)
            assert child.is_alive()
            assert results.empty(), "child finished while the parent held the write lock"
            parent_id = str(uuid.uuid4())
            now = datetime.now(UTC)
            # same column set as create_pending_prompt's INSERT
            writer.execute(
                """
                INSERT INTO pending_prompts (
                    id, created_at, expires_at, agent_id, cwd, raw_cmd,
                    normalized_template, command_tier, matched_rule_category,
                    decision_path_json, detach, script_sha256, script_paths
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    parent_id,
                    now.isoformat(),
                    (now + timedelta(seconds=300)).isoformat(),
                    BASE["agent_id"],
                    BASE["cwd"],
                    BASE["raw_cmd"],
                    BASE["normalized_template"],
                    BASE["command_tier"],
                    BASE["matched_rule_category"],
                    json.dumps(BASE["decision_path"]),
                    0,
                    None,
                    None,
                ),
            )
            writer.execute("COMMIT")
        finally:
            writer.close()
        child_id, reattached = results.get(timeout=10)
    finally:
        child.join(timeout=10)
        if child.is_alive():
            child.terminate()

    assert (child_id, reattached) == (parent_id, True)
    assert _count(path) == 1
