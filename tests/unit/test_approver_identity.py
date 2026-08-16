"""Unit tests for the approver-identity guard and prompt-visibility additions
(branch fix/approve-identity-guard-and-prompt-visibility).

Covers:
  - classifier.is_primary_identity
  - classifier.lookup_agent_cap (privilege-escalation regression, issue #36)
  - normalizer.describe_template_scope (issue #37)
  - persistence.peek_approve_token / list_pending_prompts
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from shell_runner.catalog import Tier
from shell_runner.classifier import (
    DEFAULT_AGENT_CAP,
    KNOWN_AGENT_CAPS,
    is_primary_identity,
    lookup_agent_cap,
)
from shell_runner.normalizer import PATH_PLACEHOLDERS, describe_template_scope
from shell_runner.persistence import Persistence

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Persistence:
    return Persistence(db_path=tmp_path / "test.sqlite3")


def _make_prompt(db: Persistence, **overrides: object) -> str:
    defaults: dict[str, object] = dict(
        agent_id="agent",
        cwd="/tmp",
        raw_cmd="pip install x",
        normalized_template="pip install <pkg>",
        command_tier=4,
        matched_rule_category=None,
        decision_path=[],
    )
    defaults.update(overrides)
    return db.create_pending_prompt(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# A) is_primary_identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "agent_id",
    [
        "primary",
        "PRIMARY",
        "  primary  ",
        "primary-session-482f1f98",
        "primary:abc",
    ],
)
def test_is_primary_identity_true_cases(agent_id: str) -> None:
    assert is_primary_identity(agent_id) is True


@pytest.mark.parametrize(
    "agent_id",
    [
        None,
        "",
        "   ",
        "focused-code-modifier",
        "primarysomething",  # no separator — must NOT match (important negative case)
        "notprimary-x",
        "x-primary",
    ],
)
def test_is_primary_identity_false_cases(agent_id: str | None) -> None:
    assert is_primary_identity(agent_id) is False


# ---------------------------------------------------------------------------
# B) lookup_agent_cap — privilege-escalation regression (issue #36)
# ---------------------------------------------------------------------------


def test_lookup_agent_cap_bare_primary_is_deny() -> None:
    assert lookup_agent_cap("primary") is Tier.DENY


def test_lookup_agent_cap_suffixed_primary_is_deny_regression() -> None:
    """REGRESSION (issue #36): before the fix, a suffixed primary id like
    "primary-session-abc" fell through to DEFAULT_AGENT_CAP (AUTO_CAPPED,
    tier 2) instead of DENY, granting auto-execute rights that the bare
    "primary" identity is denied — a privilege escalation. Do not
    "simplify" this test away: it is the whole point of the fix."""
    assert lookup_agent_cap("primary-session-abc") is Tier.DENY


def test_lookup_agent_cap_colon_suffixed_primary_is_deny() -> None:
    assert lookup_agent_cap("primary:abc") is Tier.DENY


@pytest.mark.parametrize("agent_id", [None, ""])
def test_lookup_agent_cap_missing_agent_id_is_deny(agent_id: str | None) -> None:
    assert lookup_agent_cap(agent_id) is Tier.DENY


def test_lookup_agent_cap_known_agent_returns_mapped_tier() -> None:
    assert KNOWN_AGENT_CAPS["focused-code-modifier"] is Tier.AUTO_CAPPED
    assert lookup_agent_cap("focused-code-modifier") is Tier.AUTO_CAPPED


def test_lookup_agent_cap_unknown_agent_falls_back_to_default() -> None:
    assert lookup_agent_cap("totally-unrecognized-agent-xyz") is DEFAULT_AGENT_CAP


# ---------------------------------------------------------------------------
# C) describe_template_scope (issue #37)
# ---------------------------------------------------------------------------


def test_describe_template_scope_no_placeholders_returns_none() -> None:
    assert describe_template_scope("rm -f .git/index.lock") is None


@pytest.mark.parametrize("placeholder", PATH_PLACEHOLDERS)
def test_describe_template_scope_warns_for_each_path_placeholder(placeholder: str) -> None:
    template = f"rm -f {placeholder}"
    message = describe_template_scope(template)
    assert message is not None
    assert placeholder in message


def test_describe_template_scope_names_both_placeholders_present() -> None:
    first, second = PATH_PLACEHOLDERS[0], PATH_PLACEHOLDERS[1]
    template = f"cp {first} {second}"
    message = describe_template_scope(template)
    assert message is not None
    assert first in message
    assert second in message


def test_describe_template_scope_is_deterministic() -> None:
    template = f"rm -f {PATH_PLACEHOLDERS[0]}"
    first_call = describe_template_scope(template)
    second_call = describe_template_scope(template)
    assert first_call == second_call


# ---------------------------------------------------------------------------
# D) persistence.peek_approve_token / list_pending_prompts
# ---------------------------------------------------------------------------


def test_peek_approve_token_does_not_consume(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    pid = _make_prompt(db)
    token = db.approve_prompt(prompt_id=pid, decision="approve_once")
    assert token is not None

    first_peek = db.peek_approve_token(token=token)
    second_peek = db.peek_approve_token(token=token)
    assert first_peek is not None
    assert second_peek is not None
    assert first_peek["consumed_at"] is None
    assert second_peek["consumed_at"] is None

    # Peeking must not have burned the token: consume must still succeed.
    consumed = db.consume_approve_token(token=token)
    assert consumed is not None
    assert consumed["raw_cmd"] == "pip install x"


def test_peek_approve_token_unknown_token_returns_none(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    assert db.peek_approve_token(token="does-not-exist") is None


def test_list_pending_prompts_excludes_approved_and_consumed(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    untouched_id = _make_prompt(db, raw_cmd="echo untouched")
    resolved_id = _make_prompt(db, raw_cmd="echo resolved")
    token = db.approve_prompt(prompt_id=resolved_id, decision="approve_once")
    assert token is not None
    consumed = db.consume_approve_token(token=token)
    assert consumed is not None

    pending = db.list_pending_prompts()
    ids = [row["id"] for row in pending]
    assert untouched_id in ids
    assert resolved_id not in ids


def test_list_pending_prompts_include_resolved_includes_resolved(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    untouched_id = _make_prompt(db, raw_cmd="echo untouched")
    resolved_id = _make_prompt(db, raw_cmd="echo resolved")
    token = db.approve_prompt(prompt_id=resolved_id, decision="approve_once")
    assert token is not None
    consumed = db.consume_approve_token(token=token)
    assert consumed is not None

    pending = db.list_pending_prompts(include_resolved=True)
    ids = [row["id"] for row in pending]
    assert untouched_id in ids
    assert resolved_id in ids


def test_list_pending_prompts_ordered_by_created_at_desc(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    oldest_id = _make_prompt(db, raw_cmd="echo oldest")
    middle_id = _make_prompt(db, raw_cmd="echo middle")
    newest_id = _make_prompt(db, raw_cmd="echo newest")

    base = datetime.now(timezone.utc)
    with db._conn() as conn:
        conn.execute(
            "UPDATE pending_prompts SET created_at = ? WHERE id = ?",
            ((base - timedelta(seconds=20)).isoformat(), oldest_id),
        )
        conn.execute(
            "UPDATE pending_prompts SET created_at = ? WHERE id = ?",
            ((base - timedelta(seconds=10)).isoformat(), middle_id),
        )
        conn.execute(
            "UPDATE pending_prompts SET created_at = ? WHERE id = ?",
            (base.isoformat(), newest_id),
        )

    pending = db.list_pending_prompts(include_resolved=True)
    ids = [row["id"] for row in pending]
    assert ids == [newest_id, middle_id, oldest_id]
