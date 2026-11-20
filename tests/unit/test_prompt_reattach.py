"""create_or_reattach_prompt: identical open prompts are reused, not duplicated (issue #119)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
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


def test_non_script_calls_match_null_sha(db: Persistence) -> None:
    first, _ = _open(db)
    assert first["script_sha256"] is None
    second, reattached = _open(db)
    assert (second["id"], reattached) == (first["id"], True)


def test_unpinnable_matches_only_itself(db: Persistence) -> None:
    first, _ = _open(db, script_sha256=UNPINNABLE)
    second, reattached = _open(db, script_sha256=UNPINNABLE)
    assert (second["id"], reattached) == (first["id"], True)


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
