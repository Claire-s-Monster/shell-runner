"""Re-deciding a prompt is guarded and recorded (issue #96)."""

# ruff: noqa: F811  (fixtures imported from test_deny_revokes_rules are used as arguments)

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shell_runner.persistence import DEFAULT_PROMPT_TTL_S, Persistence, PromptClosedError
from tests.unit.test_deny_revokes_rules import CMD_A, calls, client, db  # noqa: F401
from tests.unit.test_rule_revocations import _link_rules, _new_prompt, _revocations


def _row(db: Persistence, pid: str) -> dict[str, Any]:
    with db._conn() as conn:
        return dict(conn.execute("SELECT * FROM pending_prompts WHERE id = ?", (pid,)).fetchone())


def _history(db: Persistence) -> list[dict[str, Any]]:
    with db._conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM prompt_decisions ORDER BY id")]


def _set(db: Persistence, pid: str, **cols: str) -> None:
    sets = {
        "consumed_at": "UPDATE pending_prompts SET consumed_at = ? WHERE id = ?",
        "expires_at": "UPDATE pending_prompts SET expires_at = ? WHERE id = ?",
    }
    with db._write_conn() as conn:
        for col, val in cols.items():
            conn.execute(sets[col], (val, pid))


def _post(client: TestClient, pid: str, decision: str) -> Any:
    return client.post(
        "/approve_pending",
        json={"prompt_id": pid, "decision": decision, "approver_agent_id": "primary"},
    )


def _past() -> str:
    return (datetime.now(UTC) - timedelta(seconds=5)).isoformat()


def test_consumed_prompt_refuses_approve_with_409_and_row_unchanged(
    client: TestClient, db: Persistence
) -> None:
    pid = _new_prompt(db)
    db.decide_prompt(prompt_id=pid, decision="approve_once", approver_agent_id="primary")
    _set(db, pid, consumed_at=datetime.now(UTC).isoformat())
    before = _row(db, pid)
    n_hist = len(_history(db))

    resp = _post(client, pid, "approve_template")

    assert resp.status_code == 409
    assert "consumed" in resp.json()["detail"]
    assert _row(db, pid) == before
    assert len(_history(db)) == n_hist


def test_consumed_prompt_deny_revokes_rules_but_leaves_row(db: Persistence, tmp_path: Path) -> None:
    pid = _new_prompt(db)
    db.decide_prompt(prompt_id=pid, decision="approve_once", approver_agent_id="primary")
    _link_rules(db, pid, tmp_path)
    _set(db, pid, consumed_at=datetime.now(UTC).isoformat())
    before = _row(db, pid)

    result = db.decide_prompt(prompt_id=pid, decision="deny", approver_agent_id="other")

    assert result.revoked_rules == 3
    assert result.prompt_updated is False
    assert result.prompt_state == "consumed"
    assert _row(db, pid) == before
    assert [r["cause"] for r in _revocations(db) if r["prompt_id"] == pid][-3:] == ["deny"] * 3
    last = _history(db)[-1]
    assert (last["prompt_state"], last["prompt_updated"], last["revoked_rules"]) == (
        "consumed",
        0,
        3,
    )


def test_approve_once_on_expired_prompt_is_409_and_not_revived(
    client: TestClient, db: Persistence
) -> None:
    pid = _new_prompt(db)
    _set(db, pid, expires_at=_past())
    before = _row(db, pid)

    resp = _post(client, pid, "approve_once")

    assert resp.status_code == 409
    assert "expired" in resp.json()["detail"]
    assert _row(db, pid) == before
    assert _history(db) == []


def test_closed_prompt_error_carries_state(db: Persistence) -> None:
    pid = _new_prompt(db)
    _set(db, pid, expires_at=_past())
    with pytest.raises(PromptClosedError) as exc:
        db.decide_prompt(prompt_id=pid, decision="approve_verb", approver_agent_id="primary")
    assert exc.value.state == "expired"


def test_redecide_away_from_approve_once_resets_expiry(db: Persistence) -> None:
    pid = _new_prompt(db)
    created = datetime.fromisoformat(_row(db, pid)["created_at"])
    db.decide_prompt(prompt_id=pid, decision="approve_once", approver_agent_id="primary")
    assert datetime.fromisoformat(_row(db, pid)["expires_at"]) > created + timedelta(
        seconds=DEFAULT_PROMPT_TTL_S + 60
    )

    db.decide_prompt(prompt_id=pid, decision="approve_template", approver_agent_id="primary")

    row = _row(db, pid)
    assert row["approve_decision"] == "approve_template"
    assert datetime.fromisoformat(row["expires_at"]) == created + timedelta(
        seconds=DEFAULT_PROMPT_TTL_S
    )


def test_redecide_keeps_expiry_when_previous_was_not_approve_once(db: Persistence) -> None:
    pid = _new_prompt(db)
    db.decide_prompt(prompt_id=pid, decision="approve_template", approver_agent_id="primary")
    before = _row(db, pid)["expires_at"]
    db.decide_prompt(prompt_id=pid, decision="approve_verb", approver_agent_id="primary")
    assert _row(db, pid)["expires_at"] == before


def test_restore_after_closed_deny_does_not_rearm_approval(db: Persistence, tmp_path: Path) -> None:
    pid = db.create_pending_prompt(
        agent_id="worker",
        cwd=str(tmp_path),
        raw_cmd=CMD_A,
        normalized_template="kill -9 <n>",
        command_tier=4,
        matched_rule_category=None,
        decision_path=[],
        detach=True,
    )
    token = db.decide_prompt(
        prompt_id=pid, decision="approve_once", approver_agent_id="primary"
    ).token
    assert token is not None
    assert db.consume_approve_token(token=token) is not None
    db.decide_prompt(prompt_id=pid, decision="deny", approver_agent_id="primary")

    assert db.restore_approval(pid) is False
    assert db.consume_approve_token(token=token) is None
    assert _row(db, pid)["consumed_at"] is not None


def test_restore_after_consume_still_works_without_deny(db: Persistence, tmp_path: Path) -> None:
    pid = db.create_pending_prompt(
        agent_id="worker",
        cwd=str(tmp_path),
        raw_cmd=CMD_A,
        normalized_template="kill -9 <n>",
        command_tier=4,
        matched_rule_category=None,
        decision_path=[],
        detach=True,
    )
    token = db.decide_prompt(
        prompt_id=pid, decision="approve_once", approver_agent_id="primary"
    ).token
    assert token is not None
    assert db.consume_approve_token(token=token) is not None

    assert db.restore_approval(pid) is True
    assert db.consume_approve_token(token=token) is not None


@pytest.mark.parametrize(
    "kwargs", [{"decision": "maybe"}, {"decision": "deny", "requested_decision": "x"}]
)
def test_decide_prompt_rejects_unknown_decision(db: Persistence, kwargs: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="decision"):
        db.decide_prompt(prompt_id="p", approver_agent_id="primary", **kwargs)


def test_deny_unknown_id_is_not_applied(client: TestClient, db: Persistence) -> None:
    resp = _post(client, "no-such-prompt", "deny")

    assert resp.status_code == 200
    assert resp.json()["applied"] is False
    assert _history(db) == []


def test_deny_unknown_id_with_orphan_rules_is_applied(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    pid = _new_prompt(db)
    _link_rules(db, pid, tmp_path)
    with db._write_conn() as conn:
        conn.execute("DELETE FROM pending_prompts WHERE id = ?", (pid,))

    resp = _post(client, pid, "deny")

    assert resp.status_code == 200
    assert resp.json()["applied"] is True
    last = _history(db)[-1]
    assert (last["prompt_state"], last["revoked_rules"]) == ("missing", 3)


def test_deny_on_live_undecided_prompt_is_applied(client: TestClient, db: Persistence) -> None:
    pid = _new_prompt(db)
    assert _row(db, pid)["approve_decision"] is None

    resp = _post(client, pid, "deny")

    assert resp.status_code == 200
    assert resp.json()["applied"] is True
    assert _row(db, pid)["approve_decision"] == "deny"
    last = _history(db)[-1]
    assert (last["prompt_state"], last["prompt_updated"]) == ("live", 1)


def test_downgrade_stores_approve_once_and_history(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    pid = db.create_pending_prompt(
        agent_id="worker",
        cwd=str(tmp_path),
        raw_cmd=CMD_A,
        normalized_template="kill -9 <n>",
        command_tier=4,
        matched_rule_category=None,
        decision_path=[],
        detach=True,
    )

    resp = _post(client, pid, "approve_template")

    assert resp.status_code == 200, resp.text
    assert resp.json()["approve_token"]
    assert "downgraded" in resp.json()["scope_warning"]
    assert _row(db, pid)["approve_decision"] == "approve_once"
    last = _history(db)[-1]
    assert last["requested_decision"] == "approve_template"
    assert last["effective_decision"] == "approve_once"
    assert last["previous_decision"] is None
    assert (last["prompt_state"], last["prompt_updated"]) == ("live", 1)
    assert last["approver_agent_id"] == "primary"


def test_history_is_append_only_across_redecisions(db: Persistence) -> None:
    pid = _new_prompt(db)
    db.decide_prompt(prompt_id=pid, decision="approve_once", approver_agent_id="primary")
    db.decide_prompt(prompt_id=pid, decision="deny", approver_agent_id="primary", reason="no")

    rows = _history(db)
    assert [r["effective_decision"] for r in rows] == ["approve_once", "deny"]
    assert rows[1]["previous_decision"] == "approve_once"
    assert rows[1]["reason"] == "no"


def test_prompt_decisions_survive_retention(db: Persistence) -> None:
    pid = _new_prompt(db)
    db.decide_prompt(prompt_id=pid, decision="deny", approver_agent_id="primary")
    ancient = (datetime.now(UTC) - timedelta(days=5000)).isoformat()
    with db._write_conn() as conn:
        conn.execute("UPDATE prompt_decisions SET decided_at = ?", (ancient,))
        conn.execute(
            "UPDATE pending_prompts SET created_at = ?, expires_at = ?", (ancient, ancient)
        )

    db.cleanup_expired_prompts(approver_retention_seconds=1)
    db.cleanup_old_shell_calls(retention_seconds=1)

    assert len(_history(db)) == 1
