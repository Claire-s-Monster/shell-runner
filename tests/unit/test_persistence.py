"""Unit tests for persistence.py — SQLite CRUD operations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from shell_runner.persistence import Persistence


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Persistence:
    return Persistence(db_path=tmp_path / "test.sqlite3")


def _record_call(db: Persistence, **overrides: object) -> str:
    defaults: dict[str, object] = dict(
        agent_id="test-agent",
        cwd="/tmp",
        raw_cmd="echo hello",
        normalized_template="echo <word>",
        command_tier=1,
        final_tier=1,
        decision="executed",
        matched_rule_pattern=None,
        matched_rule_category=None,
        exit_code=0,
        stdout_bytes=10,
        stderr_bytes=0,
        duration_ms=5,
        decision_path=["T1 match"],
        normalizer_warnings=[],
    )
    defaults.update(overrides)
    return db.record_call(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_record_call_inserts_row_and_returns_id(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    tid = _record_call(db)
    assert isinstance(tid, str) and len(tid) == 36  # UUID4

    rows = db.telemetry_query()
    assert len(rows) == 1
    assert rows[0]["id"] == tid
    assert rows[0]["decision"] == "executed"


def test_upsert_template_inserts_new(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    db.upsert_template(template="echo <word>", agent_id="a1", current_tier=1, was_denied=False)
    with db._conn() as conn:
        row = conn.execute("SELECT * FROM templates WHERE template = 'echo <word>'").fetchone()
    assert row is not None
    assert row["observed_count"] == 1
    assert row["denial_count"] == 0


def test_upsert_template_increments_existing(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    db.upsert_template(template="echo <word>", agent_id="a1", current_tier=1, was_denied=False)
    db.upsert_template(template="echo <word>", agent_id="a2", current_tier=1, was_denied=True)
    with db._conn() as conn:
        row = conn.execute("SELECT * FROM templates WHERE template = 'echo <word>'").fetchone()
    assert row["observed_count"] == 2
    assert row["denial_count"] == 1


def test_upsert_template_tracks_distinct_agents(tmp_path: Path) -> None:
    import json

    db = _make_db(tmp_path)
    db.upsert_template(template="ls", agent_id="a1", current_tier=1, was_denied=False)
    db.upsert_template(template="ls", agent_id="a1", current_tier=1, was_denied=False)  # same again
    db.upsert_template(template="ls", agent_id="a2", current_tier=1, was_denied=False)
    with db._conn() as conn:
        row = conn.execute("SELECT observed_agents_json FROM templates WHERE template = 'ls'").fetchone()
    agents = json.loads(row["observed_agents_json"])
    assert sorted(agents) == ["a1", "a2"]


def test_create_pending_prompt_returns_id_and_sets_expiry(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    before = datetime.now(timezone.utc)
    pid = db.create_pending_prompt(
        agent_id="agent",
        cwd="/tmp",
        raw_cmd="pip install x",
        normalized_template="pip install <pkg>",
        command_tier=4,
        matched_rule_category="package",
        decision_path=["T4 match"],
    )
    assert isinstance(pid, str) and len(pid) == 36
    with db._conn() as conn:
        row = conn.execute("SELECT * FROM pending_prompts WHERE id = ?", (pid,)).fetchone()
    expires = datetime.fromisoformat(row["expires_at"])
    # expires_at should be ~5 minutes ahead
    diff = expires - before
    assert timedelta(seconds=290) <= diff <= timedelta(seconds=310)


def test_approve_prompt_approve_once_issues_token(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    pid = db.create_pending_prompt(
        agent_id="agent",
        cwd="/tmp",
        raw_cmd="pip install x",
        normalized_template="pip install <pkg>",
        command_tier=4,
        matched_rule_category=None,
        decision_path=[],
    )
    token = db.approve_prompt(prompt_id=pid, decision="approve_once")
    assert isinstance(token, str) and len(token) == 36


def test_approve_prompt_deny_returns_none(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    pid = db.create_pending_prompt(
        agent_id="agent",
        cwd="/tmp",
        raw_cmd="pip install x",
        normalized_template="pip install <pkg>",
        command_tier=4,
        matched_rule_category=None,
        decision_path=[],
    )
    token = db.approve_prompt(prompt_id=pid, decision="deny")
    assert token is None


def test_consume_approve_token_returns_details_and_marks_consumed(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    pid = db.create_pending_prompt(
        agent_id="agent",
        cwd="/tmp",
        raw_cmd="pip install x",
        normalized_template="pip install <pkg>",
        command_tier=4,
        matched_rule_category=None,
        decision_path=[],
    )
    token = db.approve_prompt(prompt_id=pid, decision="approve_once")
    assert token is not None

    details = db.consume_approve_token(token=token)
    assert details is not None
    assert details["raw_cmd"] == "pip install x"
    assert details["agent_id"] == "agent"

    # Second consume returns None (already consumed)
    second = db.consume_approve_token(token=token)
    assert second is None


def test_consume_approve_token_after_expiry_returns_none(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    pid = db.create_pending_prompt(
        agent_id="agent",
        cwd="/tmp",
        raw_cmd="pip install x",
        normalized_template="pip install <pkg>",
        command_tier=4,
        matched_rule_category=None,
        decision_path=[],
        ttl_seconds=0,  # immediately expired
    )
    # Patch: set expires_at to past
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    with db._conn() as conn:
        conn.execute("UPDATE pending_prompts SET expires_at = ? WHERE id = ?", (past, pid))

    token = db.approve_prompt(prompt_id=pid, decision="approve_once")
    assert token is not None

    result = db.consume_approve_token(token=token)
    assert result is None


def test_cleanup_expired_prompts_deletes_only_expired(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    # Active prompt
    active_id = db.create_pending_prompt(
        agent_id="a",
        cwd="/tmp",
        raw_cmd="cmd",
        normalized_template="cmd",
        command_tier=4,
        matched_rule_category=None,
        decision_path=[],
        ttl_seconds=300,
    )
    # Expired prompt
    expired_id = db.create_pending_prompt(
        agent_id="a",
        cwd="/tmp",
        raw_cmd="cmd2",
        normalized_template="cmd2",
        command_tier=4,
        matched_rule_category=None,
        decision_path=[],
        ttl_seconds=300,
    )
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    with db._conn() as conn:
        conn.execute(
            "UPDATE pending_prompts SET expires_at = ? WHERE id = ?", (past, expired_id)
        )

    deleted = db.cleanup_expired_prompts()
    assert deleted == 1

    with db._conn() as conn:
        remaining = conn.execute(
            "SELECT id FROM pending_prompts", ()
        ).fetchall()
    ids = [r["id"] for r in remaining]
    assert active_id in ids
    assert expired_id not in ids


def test_telemetry_query_filters(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    _record_call(db, agent_id="alice", decision="executed")
    _record_call(db, agent_id="bob", decision="denied")
    _record_call(db, agent_id="alice", decision="denied")

    alice = db.telemetry_query(agent_id="alice")
    assert len(alice) == 2

    denied = db.telemetry_query(decision="denied")
    assert len(denied) == 2

    alice_denied = db.telemetry_query(agent_id="alice", decision="denied")
    assert len(alice_denied) == 1


def test_health_stats_returns_counts_and_rates(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    _record_call(db, decision="executed", duration_ms=10)
    _record_call(db, decision="denied", duration_ms=5)
    _record_call(db, decision="prompt_required", duration_ms=3)

    stats = db.health_stats()
    assert stats["total_calls_24h"] == 3
    assert abs(stats["denied_rate_24h"] - 1 / 3) < 0.01
    assert abs(stats["prompt_rate_24h"] - 1 / 3) < 0.01
    assert stats["p50_latency_ms"] >= 0


def test_schema_initializes_idempotently(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    db._init_schema()  # call twice — must not raise
    db._init_schema()
    # Verify we can still operate
    _record_call(db)
    assert len(db.telemetry_query()) == 1
