"""Unit tests for denial_reason / is_reserved_primary_id (reserved primary agent_id wording)."""

from __future__ import annotations

import pytest

from shell_runner.classifier import Tier, classify, denial_reason, is_reserved_primary_id


@pytest.mark.parametrize(
    ("agent_id", "expected"),
    [
        ("primary", True),
        ("primary-session-x", True),
        ("primary:abc", True),
        ("", True),
        (None, True),
        ("focused-code-modifier", False),
        ("0b1c-uuid", False),
    ],
)
def test_is_reserved_primary_id(agent_id: str | None, expected: bool) -> None:
    assert is_reserved_primary_id(agent_id) is expected


@pytest.mark.parametrize("agent_id", ["primary", "primary-session-x"])
def test_reserved_primary_reason_despite_matched_rule(agent_id: str) -> None:
    cls = classify("ls", "/tmp", agent_id)
    assert cls.matched_rule is not None
    assert cls.command_tier != Tier.DENY
    reason = denial_reason(cls.command_tier, cls.matched_rule, agent_id)
    assert "reserved" in reason
    assert "session UUID" in reason
    assert "DENIED by" not in reason


def test_empty_agent_id_reason() -> None:
    reason = denial_reason(Tier.AUTO_LOG, None, "")
    assert "agent_id is empty" in reason
    assert "session UUID" in reason


def test_other_deny_capped_agent_reason() -> None:
    assert (
        denial_reason(Tier.AUTO_LOG, None, "some-agent")
        == "DENIED: agent 'some-agent' has DENY cap"
    )


def test_rule_level_denial_keeps_rule_text() -> None:
    cls = classify("rm -rf /", "/tmp", "focused-ghc-ci-analyzer")
    assert cls.command_tier == Tier.DENY
    assert denial_reason(cls.command_tier, cls.matched_rule, "focused-ghc-ci-analyzer").startswith(
        "DENIED by"
    )
