"""Unit tests for approver columns migration, retention config and prompt purge."""

from __future__ import annotations

import math
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from shell_runner.persistence import Persistence, parse_time_bound
from shell_runner.server import resolve_shell_calls_retention_s

# Tables as they were before the approver columns existed.
_OLD_SCHEMA = """
CREATE TABLE pending_prompts (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    agent_id TEXT NOT NULL, cwd TEXT NOT NULL, raw_cmd TEXT NOT NULL,
    normalized_template TEXT NOT NULL, command_tier INTEGER NOT NULL,
    matched_rule_category TEXT, decision_path_json TEXT, approve_token TEXT,
    approve_decision TEXT, approved_at TEXT, consumed_at TEXT
);
CREATE TABLE template_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, template TEXT NOT NULL, agent_id TEXT,
    approved_tier INTEGER NOT NULL, approved_at TEXT NOT NULL, approved_via_prompt_id TEXT
);
CREATE TABLE verb_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, verb TEXT NOT NULL, cwd_prefix TEXT NOT NULL,
    agent_id TEXT, approved_tier INTEGER NOT NULL, approved_at TEXT NOT NULL,
    approved_via_prompt_id TEXT
);
"""


def _columns(db_path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def test_old_schema_is_migrated_and_keeps_rows(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO template_approvals (template, agent_id, approved_tier, approved_at)"
        " VALUES ('ls', NULL, 2, 'x')"
    )
    conn.commit()
    conn.close()

    db = Persistence(db_path=path)

    assert "approver_agent_id" in _columns(path, "pending_prompts")
    assert "approved_by" in _columns(path, "template_approvals")
    assert "approved_by" in _columns(path, "verb_approvals")
    assert db.get_template_approved_tier("ls", "any") == 2
    pid = db.create_pending_prompt(
        agent_id="a",
        cwd="/tmp",
        raw_cmd="x",
        normalized_template="x",
        command_tier=4,
        matched_rule_category=None,
        decision_path=[],
    )
    assert db.approve_prompt(prompt_id=pid, decision="deny", approver_agent_id="primary") is None


def test_approver_lookup_uses_agent_cmd_index(tmp_path: Path) -> None:
    path = tmp_path / "t.sqlite3"
    Persistence(db_path=path)
    conn = sqlite3.connect(path)
    try:
        plan = " ".join(
            str(r[3])
            for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM pending_prompts "
                "WHERE agent_id = ? AND raw_cmd = ? AND cwd = ? AND approver_agent_id IS NOT NULL",
                ("a", "x", "/tmp"),
            )
        )
    finally:
        conn.close()
    assert "idx_pending_agent_cmd" in plan


def test_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "t.sqlite3"
    Persistence(db_path=path)
    Persistence(db_path=path)  # second init must not raise "duplicate column"
    assert "approver_agent_id" in _columns(path, "pending_prompts")


# --- retention env parsing ---------------------------------------------------


def test_retention_default_is_90_days() -> None:
    assert resolve_shell_calls_retention_s({}) == 90 * 86400


def test_retention_custom_days() -> None:
    assert resolve_shell_calls_retention_s({"SHELL_RUNNER_RETENTION_DAYS": "30"}) == 30 * 86400


@pytest.mark.parametrize("value", ["0", "-1"])
def test_retention_zero_or_negative_keeps_forever(value: str) -> None:
    assert math.isinf(resolve_shell_calls_retention_s({"SHELL_RUNNER_RETENTION_DAYS": value}))


def test_retention_invalid_raises() -> None:
    with pytest.raises(RuntimeError, match="SHELL_RUNNER_RETENTION_DAYS"):
        resolve_shell_calls_retention_s({"SHELL_RUNNER_RETENTION_DAYS": "soon"})


def test_retention_legacy_seconds_still_honoured_and_new_var_wins() -> None:
    assert resolve_shell_calls_retention_s({"SHELL_CALLS_RETENTION_S": "1000000"}) == 1000000
    both = {"SHELL_CALLS_RETENTION_S": "1000000", "SHELL_RUNNER_RETENTION_DAYS": "10"}
    assert resolve_shell_calls_retention_s(both) == 10 * 86400


# --- prompt purge keeps decided prompts ---------------------------------------


def _expired_prompt(db: Persistence, *, decided: bool, age_days: int) -> str:
    pid = db.create_pending_prompt(
        agent_id="a",
        cwd="/tmp",
        raw_cmd="x",
        normalized_template="x",
        command_tier=4,
        matched_rule_category=None,
        decision_path=[],
    )
    if decided:
        db.approve_prompt(prompt_id=pid, decision="deny", approver_agent_id="primary")
    created = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    expired = (datetime.now(UTC) - timedelta(days=age_days) + timedelta(minutes=5)).isoformat()
    with db._write_conn() as conn:
        conn.execute(
            "UPDATE pending_prompts SET created_at = ?, expires_at = ? WHERE id = ?",
            (created, expired, pid),
        )
    return pid


def _exists(db: Persistence, pid: str) -> bool:
    with db._conn() as conn:
        return (
            conn.execute("SELECT 1 FROM pending_prompts WHERE id = ?", (pid,)).fetchone()
            is not None
        )


def test_decided_prompts_survive_expiry_until_retention(tmp_path: Path) -> None:
    db = Persistence(db_path=tmp_path / "t.sqlite3")
    undecided = _expired_prompt(db, decided=False, age_days=1)
    decided_recent = _expired_prompt(db, decided=True, age_days=1)
    decided_old = _expired_prompt(db, decided=True, age_days=100)

    db.cleanup_expired_prompts(approver_retention_seconds=90 * 86400)

    assert not _exists(db, undecided)
    assert _exists(db, decided_recent)
    assert not _exists(db, decided_old)


def test_decided_prompts_kept_forever_when_retention_is_none(tmp_path: Path) -> None:
    db = Persistence(db_path=tmp_path / "t.sqlite3")
    decided_old = _expired_prompt(db, decided=True, age_days=1000)
    undecided = _expired_prompt(db, decided=False, age_days=1000)

    db.cleanup_expired_prompts(None)

    assert _exists(db, decided_old)
    assert not _exists(db, undecided)


# --- time bound parsing -------------------------------------------------------


def test_parse_time_bound_relative_and_iso() -> None:
    now = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
    assert parse_time_bound("24h", now) == "2026-01-09T12:00:00+00:00"
    assert parse_time_bound("7d", now) == "2026-01-03T12:00:00+00:00"
    assert parse_time_bound("2026-01-01T00:00:00Z", now) == "2026-01-01T00:00:00+00:00"
    assert parse_time_bound("2026-01-01", now) == "2026-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="invalid time"):
        parse_time_bound("yesterday", now)
