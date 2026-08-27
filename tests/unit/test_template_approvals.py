"""Unit tests for template_approvals persistence methods."""

from __future__ import annotations

from pathlib import Path

import pytest

from shell_runner.persistence import Persistence


def _make_db(tmp_path: Path) -> Persistence:
    return Persistence(db_path=tmp_path / "test.sqlite3")


def _insert_pending_prompt(
    db: Persistence, agent_id: str = "agent-a", command_tier: int = 3
) -> str:
    """Insert a pending prompt and return its id."""
    return db.create_pending_prompt(
        agent_id=agent_id,
        cwd="/tmp",
        raw_cmd="nonexistent_cmd_xyz --flag",
        normalized_template="nonexistent_cmd_xyz <flag>",
        command_tier=command_tier,
        matched_rule_category=None,
        decision_path=["T3_fallthrough"],
    )


# ---------------------------------------------------------------------------
# create_template_approval
# ---------------------------------------------------------------------------


def test_create_global_approval_returns_id(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    row_id = db.create_template_approval(
        template="nonexistent_cmd_xyz <flag>",
        agent_id=None,
        approved_tier=2,
    )
    assert isinstance(row_id, int)
    assert row_id >= 1


def test_create_agent_approval_returns_id(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    row_id = db.create_template_approval(
        template="nonexistent_cmd_xyz <flag>",
        agent_id="agent-a",
        approved_tier=2,
    )
    assert isinstance(row_id, int)
    assert row_id >= 1


# ---------------------------------------------------------------------------
# get_template_approved_tier
# ---------------------------------------------------------------------------


def test_get_approved_tier_returns_none_when_no_match(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    result = db.get_template_approved_tier("nonexistent_cmd_xyz <flag>", "agent-a")
    assert result is None


def test_get_approved_tier_returns_global_approval(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    db.create_template_approval(
        template="nonexistent_cmd_xyz <flag>",
        agent_id=None,
        approved_tier=2,
    )
    result = db.get_template_approved_tier("nonexistent_cmd_xyz <flag>", "agent-a")
    assert result == 2


def test_get_approved_tier_returns_agent_specific_approval(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    db.create_template_approval(
        template="nonexistent_cmd_xyz <flag>",
        agent_id="agent-a",
        approved_tier=2,
    )
    result = db.get_template_approved_tier("nonexistent_cmd_xyz <flag>", "agent-a")
    assert result == 2


def test_global_approval_takes_precedence_over_agent_specific(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    # Agent-specific at T2 (AUTO_CAPPED), global at T1 (AUTO_LOG)
    db.create_template_approval(
        template="nonexistent_cmd_xyz <flag>",
        agent_id="agent-a",
        approved_tier=2,
    )
    db.create_template_approval(
        template="nonexistent_cmd_xyz <flag>",
        agent_id=None,
        approved_tier=1,
    )
    result = db.get_template_approved_tier("nonexistent_cmd_xyz <flag>", "agent-a")
    # Global wins: returns 1
    assert result == 1


def test_replace_approval_for_same_scope_updates_tier(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    db.create_template_approval(
        template="nonexistent_cmd_xyz <flag>",
        agent_id="agent-a",
        approved_tier=2,
    )
    # Re-insert at T1 — should replace
    db.create_template_approval(
        template="nonexistent_cmd_xyz <flag>",
        agent_id="agent-a",
        approved_tier=1,
    )
    result = db.get_template_approved_tier("nonexistent_cmd_xyz <flag>", "agent-a")
    assert result == 1


def test_agent_approval_does_not_leak_to_other_agents(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    db.create_template_approval(
        template="nonexistent_cmd_xyz <flag>",
        agent_id="agent-a",
        approved_tier=2,
    )
    result = db.get_template_approved_tier("nonexistent_cmd_xyz <flag>", "agent-b")
    assert result is None


def test_global_approval_visible_to_all_agents(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    db.create_template_approval(
        template="nonexistent_cmd_xyz <flag>",
        agent_id=None,
        approved_tier=2,
    )
    assert db.get_template_approved_tier("nonexistent_cmd_xyz <flag>", "agent-a") == 2
    assert db.get_template_approved_tier("nonexistent_cmd_xyz <flag>", "agent-b") == 2
    assert db.get_template_approved_tier("nonexistent_cmd_xyz <flag>", "agent-c") == 2


# ---------------------------------------------------------------------------
# get_pending_prompt
# ---------------------------------------------------------------------------


def test_get_pending_prompt_returns_full_dict(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    prompt_id = _insert_pending_prompt(db, agent_id="agent-a", command_tier=3)
    result = db.get_pending_prompt(prompt_id)

    assert result is not None
    assert isinstance(result, dict)
    assert result["id"] == prompt_id
    assert result["agent_id"] == "agent-a"
    assert result["command_tier"] == 3
    assert result["normalized_template"] == "nonexistent_cmd_xyz <flag>"
    assert result["raw_cmd"] == "nonexistent_cmd_xyz --flag"
    assert result["cwd"] == "/tmp"
    # Optional fields present
    assert "expires_at" in result
    assert "approve_token" in result
    assert "approve_decision" in result
    assert "approved_at" in result
    assert "consumed_at" in result


def test_get_pending_prompt_returns_none_for_unknown_id(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    result = db.get_pending_prompt("00000000-0000-0000-0000-000000000000")
    assert result is None
