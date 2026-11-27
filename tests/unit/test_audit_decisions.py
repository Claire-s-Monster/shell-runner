"""shell_audit mode='decisions' exposes the prompt_decisions history (issue #106)."""

# ruff: noqa: F811  (fixtures imported from test_deny_revokes_rules are used as arguments)

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from shell_runner.models import AuditRequest
from shell_runner.persistence import Persistence
from tests.unit.test_deny_revokes_rules import calls, client, db  # noqa: F401  (fixtures)
from tests.unit.test_rule_revocations import _link_rules, _new_prompt

COLUMNS = {
    "id",
    "prompt_id",
    "decided_at",
    "approver_agent_id",
    "reason",
    "requested_decision",
    "effective_decision",
    "previous_decision",
    "prompt_state",
    "prompt_updated",
    "revoked_rules",
}


def _age(db: Persistence, where: str, value: str, ago: timedelta) -> None:
    ts = (datetime.now(UTC) - ago).isoformat()
    with db._write_conn() as conn:
        conn.execute(f"UPDATE prompt_decisions SET decided_at = ? WHERE {where} = ?", (ts, value))


def _seed(db: Persistence) -> dict[str, str]:
    """p1: approve_once (alice) then deny (bob). p2: approve_template (carol)."""
    p1 = _new_prompt(db, "worker")
    db.decide_prompt(prompt_id=p1, decision="approve_once", approver_agent_id="alice")
    db.decide_prompt(prompt_id=p1, decision="deny", approver_agent_id="bob", reason="nope")
    p2 = _new_prompt(db, "other")
    db.decide_prompt(prompt_id=p2, decision="approve_template", approver_agent_id="carol")
    return {"p1": p1, "p2": p2}


def _decisions(db: Persistence, **kw: Any) -> dict[str, Any]:
    return db.audit_query(mode="decisions", **kw)


def test_prompt_decisions_has_decided_at_id_index(db: Persistence) -> None:
    with db._write_conn() as conn:
        indexes = [row[1] for row in conn.execute("PRAGMA index_list(prompt_decisions)")]
        cols_by_index = {
            name: [row[2] for row in conn.execute(f"PRAGMA index_info({name})")] for name in indexes
        }

    assert ["decided_at", "id"] in cols_by_index.values()


# --- shape, order, limit ---------------------------------------------------------


def test_decisions_row_shape_and_order(db: Persistence) -> None:
    ps = _seed(db)

    result = _decisions(db)

    assert result["mode"] == "decisions"
    assert result["count"] == 3
    assert result["truncated"] is False
    assert result["notes"] == []
    rows = result["rows"]
    assert [r["effective_decision"] for r in rows] == ["approve_template", "deny", "approve_once"]
    assert rows[0]["prompt_id"] == ps["p2"]
    assert set(rows[0]) == COLUMNS
    assert rows[1]["previous_decision"] == "approve_once"
    assert rows[1]["reason"] == "nope"
    assert rows[1]["approver_agent_id"] == "bob"
    assert isinstance(rows[0]["revoked_rules"], int)


def test_decisions_order_is_decided_at_desc_then_id_desc(db: Persistence) -> None:
    _seed(db)
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    same = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    with db._write_conn() as conn:
        conn.execute("UPDATE prompt_decisions SET decided_at = ? WHERE id = 3", (old,))
        conn.execute("UPDATE prompt_decisions SET decided_at = ? WHERE id IN (1, 2)", (same,))

    ids = [r["id"] for r in _decisions(db)["rows"]]

    assert ids == [2, 1, 3]


def test_decisions_limit_truncates(db: Persistence) -> None:
    _seed(db)

    result = _decisions(db, limit=2)

    assert result["count"] == 2
    assert result["truncated"] is True
    assert _decisions(db, limit=3)["truncated"] is False


def test_decisions_empty(db: Persistence) -> None:
    result = _decisions(db)
    assert (result["rows"], result["count"], result["truncated"]) == ([], 0, False)


# --- filters ---------------------------------------------------------------------


def test_decisions_filters(db: Persistence) -> None:
    ps = _seed(db)

    by_prompt = _decisions(db, prompt_id=ps["p1"])
    assert [r["effective_decision"] for r in by_prompt["rows"]] == ["deny", "approve_once"]
    assert {r["approver_agent_id"] for r in _decisions(db, approver_agent_id="bob")["rows"]} == {
        "bob"
    }
    assert _decisions(db, approver_agent_id="nobody")["count"] == 0
    by_eff = _decisions(db, effective_decision="approve_once")
    assert [r["approver_agent_id"] for r in by_eff["rows"]] == ["alice"]


def test_decisions_filters_combine(db: Persistence) -> None:
    ps = _seed(db)

    hit = _decisions(db, prompt_id=ps["p1"], approver_agent_id="bob", effective_decision="deny")
    assert [r["reason"] for r in hit["rows"]] == ["nope"]
    assert _decisions(db, prompt_id=ps["p2"], approver_agent_id="bob")["count"] == 0
    assert _decisions(db, prompt_id=ps["p1"], effective_decision="approve_template")["count"] == 0


def test_decisions_since_until(db: Persistence) -> None:
    ps = _seed(db)
    _age(db, "prompt_id", ps["p1"], timedelta(days=3))
    cut = (datetime.now(UTC) - timedelta(days=1)).isoformat()

    assert {r["prompt_id"] for r in _decisions(db, since=cut)["rows"]} == {ps["p2"]}
    assert {r["prompt_id"] for r in _decisions(db, until=cut)["rows"]} == {ps["p1"]}
    assert _decisions(db, since=cut, prompt_id=ps["p1"])["count"] == 0


def test_effective_decision_is_validated_by_the_request_model() -> None:
    for value in (
        "deny",
        "approve_once",
        "approve_template",
        "approve_template_global",
        "approve_verb",
    ):
        assert AuditRequest(mode="decisions", effective_decision=value).effective_decision == value
    with pytest.raises(ValidationError):
        AuditRequest(mode="decisions", effective_decision="bogus")


def test_request_model_accepts_decisions_mode_and_filters() -> None:
    req = AuditRequest(mode="decisions", approver_agent_id="a", prompt_id="p")
    assert req.mode == "decisions"
    assert req.approver_agent_id == "a"


# --- what is recorded --------------------------------------------------------------


def test_closed_prompt_deny_is_visible_and_prompt_untouched(
    db: Persistence, tmp_path: Path
) -> None:
    pid = _new_prompt(db)
    db.decide_prompt(prompt_id=pid, decision="approve_once", approver_agent_id="primary")
    _link_rules(db, pid, tmp_path)
    with db._write_conn() as conn:
        conn.execute(
            "UPDATE pending_prompts SET consumed_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), pid),
        )
    before = db.get_pending_prompt(pid)

    db.decide_prompt(prompt_id=pid, decision="deny", approver_agent_id="other", reason="late")

    assert db.get_pending_prompt(pid) == before
    top = _decisions(db, prompt_id=pid)["rows"][0]
    assert top["effective_decision"] == "deny"
    assert top["prompt_state"] == "consumed"
    assert top["prompt_updated"] in (0, False)
    assert top["revoked_rules"] == 3
    assert top["previous_decision"] == "approve_once"
    assert top["approver_agent_id"] == "other"


def test_expired_prompt_deny_is_visible(db: Persistence) -> None:
    pid = _new_prompt(db)
    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    with db._write_conn() as conn:
        conn.execute("UPDATE pending_prompts SET expires_at = ? WHERE id = ?", (past, pid))

    db.decide_prompt(prompt_id=pid, decision="deny", approver_agent_id="primary")

    (row,) = _decisions(db, prompt_id=pid)["rows"]
    assert row["prompt_state"] == "expired"


def test_downgrade_requested_vs_effective_visible(db: Persistence) -> None:
    pid = _new_prompt(db)
    db.decide_prompt(
        prompt_id=pid,
        decision="approve_once",
        approver_agent_id="primary",
        requested_decision="approve_template",
    )

    (row,) = _decisions(db, prompt_id=pid)["rows"]
    assert row["requested_decision"] == "approve_template"
    assert row["effective_decision"] == "approve_once"
    assert _decisions(db, effective_decision="approve_once")["count"] == 1
    assert _decisions(db, effective_decision="approve_template")["count"] == 0


def test_orphan_deny_with_rules_is_recorded_as_missing(db: Persistence, tmp_path: Path) -> None:
    pid = _new_prompt(db)
    _link_rules(db, pid, tmp_path)
    with db._write_conn() as conn:
        conn.execute("DELETE FROM pending_prompts WHERE id = ?", (pid,))

    db.decide_prompt(prompt_id=pid, decision="deny", approver_agent_id="primary")

    (row,) = _decisions(db, prompt_id=pid)["rows"]
    assert row["prompt_state"] == "missing"
    assert row["revoked_rules"] == 3


def test_refused_409_redecision_is_not_recorded(db: Persistence) -> None:
    from shell_runner.persistence import PromptClosedError

    pid = _new_prompt(db)
    db.decide_prompt(prompt_id=pid, decision="approve_once", approver_agent_id="primary")
    with db._write_conn() as conn:
        conn.execute(
            "UPDATE pending_prompts SET consumed_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), pid),
        )
    with pytest.raises(PromptClosedError):
        db.decide_prompt(prompt_id=pid, decision="approve_template", approver_agent_id="x")

    assert _decisions(db, prompt_id=pid)["count"] == 1


# --- summary -----------------------------------------------------------------------


def test_summary_includes_decisions_count(db: Persistence) -> None:
    ps = _seed(db)
    _age(db, "prompt_id", ps["p1"], timedelta(days=3))

    assert db.audit_query(mode="summary")["decisions"] == 3
    since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    assert db.audit_query(mode="summary", since=since)["decisions"] == 1
    until = since
    assert db.audit_query(mode="summary", until=until)["decisions"] == 2


# --- ignored filters ---------------------------------------------------------------


def test_decisions_mode_ignores_inapplicable_filters_with_notes(db: Persistence) -> None:
    _seed(db)

    noisy = _decisions(
        db,
        approval_rule_kind="verb",
        approval_rule_outcome="honoured",
        decision="denied",
        flagged_only=True,
        rule_kind="verb",
        cause="deny",
    )

    assert noisy["count"] == 3  # none of them narrowed anything
    for name in (
        "approval_rule_kind",
        "approval_rule_outcome",
        "decision",
        "flagged_only",
        "rule_kind",
        "cause",
    ):
        assert any(f"{name} ignored" in n and "decisions" in n for n in noisy["notes"]), name


@pytest.mark.parametrize("mode", ["rows", "summary", "revocations"])
def test_decision_only_filters_noted_in_other_modes(db: Persistence, mode: str) -> None:
    _seed(db)

    result = db.audit_query(mode=mode, effective_decision="deny", approver_agent_id="bob")

    for name in ("effective_decision", "approver_agent_id"):
        assert any(f"{name} ignored" in n and "decisions" in n for n in result["notes"]), name


def test_prompt_id_note_in_rows_mode_mentions_decisions(db: Persistence) -> None:
    result = db.audit_query(mode="rows", prompt_id="x")
    assert any("prompt_id" in n and "decisions" in n for n in result["notes"])


# --- guards: existing modes unchanged -----------------------------------------------


def test_rows_and_revocations_unchanged(db: Persistence, tmp_path: Path) -> None:
    pid = _new_prompt(db)
    _link_rules(db, pid, tmp_path)
    db.decide_prompt(prompt_id=pid, decision="deny", approver_agent_id="alice")

    rev = db.audit_query(mode="revocations")
    assert rev["count"] == 3
    assert rev["notes"] == []
    assert "decisions" not in rev
    rows = db.audit_query(mode="rows")
    assert rows["rows"] == []
    assert rows["notes"] == []
    assert db.audit_query(mode="summary")["revocations"] == 3


def test_request_model_existing_modes_still_valid() -> None:
    for mode in ("rows", "summary", "revocations"):
        assert AuditRequest(mode=mode).mode == mode
    with pytest.raises(ValidationError):
        AuditRequest(mode="bogus")  # type: ignore[arg-type]
