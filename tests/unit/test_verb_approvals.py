"""Unit tests for verb_approvals persistence methods (issue #26 P2 Part A)."""

from __future__ import annotations

from pathlib import Path

from shell_runner.persistence import Persistence


def _make_db(tmp_path: Path) -> Persistence:
    return Persistence(db_path=tmp_path / "test.sqlite3")


# ---------------------------------------------------------------------------
# create_verb_approval
# ---------------------------------------------------------------------------


def test_create_verb_approval_returns_id(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    row_id = db.create_verb_approval(
        verb="git",
        cwd_prefix=str(tmp_path),
        agent_id="agent-a",
        approved_tier=2,
    )
    assert isinstance(row_id, int)
    assert row_id >= 1


def test_create_verb_approval_replaces_same_scope(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    db.create_verb_approval(
        verb="git", cwd_prefix=str(tmp_path), agent_id="agent-a", approved_tier=2
    )
    db.create_verb_approval(
        verb="git", cwd_prefix=str(tmp_path), agent_id="agent-a", approved_tier=1
    )
    result = db.get_verb_approved_tier("git", str(tmp_path), "agent-a")
    assert result == 1


# ---------------------------------------------------------------------------
# get_verb_approved_tier — cwd containment
# ---------------------------------------------------------------------------


def test_get_verb_approved_tier_none_when_no_match(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    result = db.get_verb_approved_tier("git", str(tmp_path), "agent-a")
    assert result is None


def test_get_verb_approved_tier_matches_subdirectory(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    db.create_verb_approval(
        verb="git", cwd_prefix=str(tmp_path), agent_id="agent-a", approved_tier=2
    )
    result = db.get_verb_approved_tier("git", str(sub), "agent-a")
    assert result == 2


def test_get_verb_approved_tier_none_outside_prefix(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    inside = tmp_path / "inside"
    inside.mkdir()
    outside = tmp_path.parent / f"sibling-{tmp_path.name}"
    db.create_verb_approval(verb="git", cwd_prefix=str(inside), agent_id="agent-a", approved_tier=2)
    result = db.get_verb_approved_tier("git", str(outside), "agent-a")
    assert result is None


def test_get_verb_approved_tier_global_agent_none_applies_to_any_agent(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    db.create_verb_approval(verb="git", cwd_prefix=str(tmp_path), agent_id=None, approved_tier=2)
    assert db.get_verb_approved_tier("git", str(tmp_path), "agent-a") == 2
    assert db.get_verb_approved_tier("git", str(tmp_path), "agent-b") == 2


def test_get_verb_approved_tier_returns_max_among_matches(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    # Broad prefix at tier 1 (AUTO_LOG), narrower prefix at tier 2 (AUTO_CAPPED).
    db.create_verb_approval(
        verb="git", cwd_prefix=str(tmp_path), agent_id="agent-a", approved_tier=1
    )
    db.create_verb_approval(verb="git", cwd_prefix=str(sub), agent_id="agent-a", approved_tier=2)
    # Both rows match cwd=sub; MAX(approved_tier) = 2 (the more conservative one) wins.
    result = db.get_verb_approved_tier("git", str(sub), "agent-a")
    assert result == 2


# ---------------------------------------------------------------------------
# consume_approval_by_command
# ---------------------------------------------------------------------------


def test_consume_approval_by_command_none_when_not_approved(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    db.create_pending_prompt(
        agent_id="agent-a",
        cwd="/tmp",
        raw_cmd="nonexistent_cmd_xyz --flag",
        normalized_template="nonexistent_cmd_xyz <flag>",
        command_tier=3,
        matched_rule_category=None,
        decision_path=["T3_fallthrough"],
    )
    result = db.consume_approval_by_command(
        raw_cmd="nonexistent_cmd_xyz --flag", cwd="/tmp", agent_id="agent-a"
    )
    assert result is None


def test_consume_approval_by_command_single_use(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    prompt_id = db.create_pending_prompt(
        agent_id="agent-a",
        cwd="/tmp",
        raw_cmd="nonexistent_cmd_xyz --flag",
        normalized_template="nonexistent_cmd_xyz <flag>",
        command_tier=3,
        matched_rule_category=None,
        decision_path=["T3_fallthrough"],
    )
    db.approve_prompt(prompt_id=prompt_id, decision="approve_once")

    first = db.consume_approval_by_command(
        raw_cmd="nonexistent_cmd_xyz --flag", cwd="/tmp", agent_id="agent-a"
    )
    assert first is not None
    assert first["raw_cmd"] == "nonexistent_cmd_xyz --flag"

    second = db.consume_approval_by_command(
        raw_cmd="nonexistent_cmd_xyz --flag", cwd="/tmp", agent_id="agent-a"
    )
    assert second is None
