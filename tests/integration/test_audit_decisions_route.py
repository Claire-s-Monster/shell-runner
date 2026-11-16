"""POST /audit (shell_audit) mode='decisions' end to end (issue #106)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import shell_runner.server as srv
from shell_runner.persistence import Persistence
from shell_runner.server import app


@pytest.fixture(autouse=True)
def _fresh_db_per_test(fresh_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shell_runner.server.db", Persistence(db_path=fresh_db))


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _prompt(agent_id: str = "agent-a") -> str:
    return srv.db.create_pending_prompt(
        agent_id=agent_id,
        cwd="/tmp",
        raw_cmd="pip install requests",
        normalized_template="pip install <pkg>",
        command_tier=4,
        matched_rule_category="package",
        decision_path=["T4 match"],
    )


def _decide(client: TestClient, pid: str, decision: str, approver: str = "primary") -> Any:
    return client.post(
        "/approve_pending",
        json={
            "prompt_id": pid,
            "decision": decision,
            "approver_agent_id": approver,
            "reason": f"because {decision}",
        },
    )


def _audit(client: TestClient, **body: Any) -> dict[str, Any]:
    resp = client.post("/audit", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_decisions_mode_via_route(client: TestClient) -> None:
    pid = _prompt()
    assert _decide(client, pid, "approve_template").status_code == 200
    assert _decide(client, pid, "deny", approver="primary-session-bob").status_code == 200

    data = _audit(client, mode="decisions", since="1h", prompt_id=pid)

    assert data["mode"] == "decisions"
    assert data["count"] == 2
    assert data["truncated"] is False
    assert [r["effective_decision"] for r in data["rows"]] == ["deny", "approve_template"]
    assert data["rows"][0]["approver_agent_id"] == "primary-session-bob"
    assert data["rows"][0]["reason"] == "because deny"
    assert data["rows"][0]["previous_decision"] == "approve_template"
    assert _audit(client, mode="decisions", approver_agent_id="primary")["count"] == 1
    assert _audit(client, mode="decisions", effective_decision="deny")["count"] == 1
    assert _audit(client, mode="decisions", limit=1)["truncated"] is True
    assert _audit(client, mode="summary")["decisions"] == 2


def test_closed_prompt_deny_visible_via_route(client: TestClient) -> None:
    pid = _prompt()
    assert _decide(client, pid, "approve_once").status_code == 200
    with srv.db._write_conn() as conn:
        conn.execute(
            "UPDATE pending_prompts SET consumed_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (pid,),
        )
    assert _decide(client, pid, "approve_template").status_code == 409
    assert _decide(client, pid, "deny").status_code == 200

    data = _audit(client, mode="decisions", prompt_id=pid)

    assert [r["prompt_state"] for r in data["rows"]] == ["consumed", "live"]


def test_decisions_validation(client: TestClient) -> None:
    assert (
        client.post("/audit", json={"mode": "decisions", "effective_decision": "bogus"}).status_code
        == 422
    )
    assert client.post("/audit", json={"mode": "decisions", "since": "x"}).status_code == 400
    assert client.post("/audit", json={"mode": "bogus"}).status_code == 422


def test_existing_modes_unchanged_via_route(client: TestClient) -> None:
    data = _audit(client)
    assert data["mode"] == "rows"
    assert data["notes"] == []
    assert _audit(client, mode="revocations")["notes"] == []
