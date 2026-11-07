"""Integration tests for approver recording and the /audit (shell_audit) route."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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


def _prompt(raw_cmd: str = "pip install requests", agent_id: str = "test-agent") -> str:
    return srv.db.create_pending_prompt(
        agent_id=agent_id,
        cwd="/tmp",
        raw_cmd=raw_cmd,
        normalized_template="pip install <pkg>",
        command_tier=4,
        matched_rule_category="package",
        decision_path=["T4 match"],
    )


def _scalar(sql: str, *params: Any) -> Any:
    with srv.db._conn() as conn:
        row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# approver recording
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "decision",
    ["approve_once", "approve_template", "approve_template_global", "approve_verb", "deny"],
)
def test_approver_recorded_on_prompt_for_every_decision(client: TestClient, decision: str) -> None:
    pid = _prompt()
    resp = client.post(
        "/approve_pending",
        json={"prompt_id": pid, "decision": decision, "approver_agent_id": "primary-session-x"},
    )
    assert resp.status_code == 200, resp.text
    assert (
        _scalar("SELECT approver_agent_id FROM pending_prompts WHERE id = ?", pid)
        == "primary-session-x"
    )


@pytest.mark.parametrize("decision", ["approve_template", "approve_template_global"])
def test_approver_recorded_on_template_approval(client: TestClient, decision: str) -> None:
    pid = _prompt()
    resp = client.post(
        "/approve_pending",
        json={"prompt_id": pid, "decision": decision, "approver_agent_id": "primary"},
    )
    assert resp.status_code == 200, resp.text
    assert (
        _scalar("SELECT approved_by FROM template_approvals WHERE approved_via_prompt_id = ?", pid)
        == "primary"
    )


def test_approver_recorded_on_verb_approval_and_upsert_updates_it(client: TestClient) -> None:
    for approver in ("primary", "primary-session-2"):
        pid = _prompt()
        resp = client.post(
            "/approve_pending",
            json={"prompt_id": pid, "decision": "approve_verb", "approver_agent_id": approver},
        )
        assert resp.status_code == 200, resp.text
    assert _scalar("SELECT COUNT(*) FROM verb_approvals") == 1  # replaced, not duplicated
    assert _scalar("SELECT approved_by FROM verb_approvals") == "primary-session-2"


# ---------------------------------------------------------------------------
# /audit
# ---------------------------------------------------------------------------


def _call(
    agent_id: str, decision: str = "executed", ago: timedelta = timedelta(0), **kw: Any
) -> str:
    defaults: dict[str, Any] = dict(
        agent_id=agent_id,
        cwd="/tmp",
        raw_cmd="echo hi",
        normalized_template="echo <word>",
        command_tier=1,
        final_tier=1,
        decision=decision,
        matched_rule_pattern="p",
        matched_rule_category="c",
        exit_code=0,
        stdout_bytes=0,
        stderr_bytes=0,
        duration_ms=1,
        decision_path=[],
        normalizer_warnings=[],
    )
    defaults.update(kw)
    call_id = srv.db.record_call(**defaults)
    ts = (datetime.now(UTC) - ago).isoformat()
    with srv.db._write_conn() as conn:
        conn.execute("UPDATE shell_calls SET ts = ? WHERE id = ?", (ts, call_id))
    return call_id


def _audit(client: TestClient, **body: Any) -> dict[str, Any]:
    resp = client.post("/audit", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def seeded() -> None:
    _call("agent-a", "executed", timedelta(hours=1))
    _call("agent-a", "denied", timedelta(hours=2))
    _call("agent-a2", "prompt_required", timedelta(days=2))
    _call("other", "executed", timedelta(days=10))


def test_rows_mode_shape_and_order(client: TestClient, seeded: None) -> None:
    data = _audit(client)
    assert data["mode"] == "rows"
    assert data["count"] == 4
    assert data["truncated"] is False
    assert [r["agent_id"] for r in data["rows"]] == ["agent-a", "agent-a", "agent-a2", "other"]
    assert set(data["rows"][0]) >= {
        "ts",
        "agent_id",
        "cwd",
        "raw_cmd",
        "command_tier",
        "final_tier",
        "decision",
        "matched_rule_category",
        "matched_rule_pattern",
        "exit_code",
        "approver_agent_id",
    }


def test_agent_exact_and_prefix(client: TestClient, seeded: None) -> None:
    assert _audit(client, agent_id="agent-a")["count"] == 2
    assert _audit(client, agent_id_prefix="agent-a")["count"] == 3
    assert _audit(client, agent_id_prefix="agent_a")["count"] == 0  # no LIKE wildcards


def test_window_absolute_and_relative(client: TestClient, seeded: None) -> None:
    assert _audit(client, since="24h")["count"] == 2
    assert _audit(client, since="3d")["count"] == 3
    assert _audit(client, until="3d")["count"] == 1
    since = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    until = (datetime.now(UTC) - timedelta(hours=1, minutes=30)).isoformat()
    assert _audit(client, since=since, until=until)["count"] == 2


def test_bad_time_is_400(client: TestClient) -> None:
    assert client.post("/audit", json={"since": "whenever"}).status_code == 400


def test_decision_filter(client: TestClient, seeded: None) -> None:
    data = _audit(client, decision="denied")
    assert [r["decision"] for r in data["rows"]] == ["denied"]


def test_summary_aggregation(client: TestClient, seeded: None) -> None:
    data = _audit(client, mode="summary", agent_id_prefix="agent-a")
    by_agent = {e["agent_id"]: e for e in data["summary"]}
    a = by_agent["agent-a"]
    assert a["total_calls"] == 2
    assert a["by_decision"] == {"executed": 1, "denied": 1}
    assert a["denied_count"] == 1
    assert a["prompt_count"] == 0
    assert a["first_ts"] < a["last_ts"]
    assert by_agent["agent-a2"]["prompt_count"] == 1
    assert data["summary"][0]["agent_id"] == "agent-a"  # busiest first
    assert data["rows"] is None


def test_limit_and_cap(client: TestClient) -> None:
    for _ in range(5):
        _call("agent-a")
    data = _audit(client, limit=2)
    assert data["count"] == 2
    assert data["truncated"] is True
    assert _audit(client, limit=5000)["count"] == 5  # clamped to 1000, not rejected


def test_flagged_only_without_column_returns_note(client: TestClient, seeded: None) -> None:
    data = _audit(client, flagged_only=True)
    assert data["count"] == 4  # filter not applied
    assert any("flagged" in n for n in data["notes"])
    assert _audit(client)["notes"] == []


def test_flagged_only_applies_when_column_exists(client: TestClient) -> None:
    with srv.db._write_conn() as conn:
        conn.execute("ALTER TABLE shell_calls ADD COLUMN flagged INTEGER NOT NULL DEFAULT 0")
    call_id = _call("agent-a")
    _call("agent-a")
    with srv.db._write_conn() as conn:
        conn.execute("UPDATE shell_calls SET flagged = 1 WHERE id = ?", (call_id,))

    data = _audit(client, flagged_only=True)

    assert data["count"] == 1
    assert data["notes"] == []
    assert data["rows"][0]["flagged"] == 1


def test_rows_include_approver_from_matching_prompt(client: TestClient) -> None:
    pid = _prompt(raw_cmd="pip install requests", agent_id="agent-a")
    resp = client.post(
        "/approve_pending",
        json={"prompt_id": pid, "decision": "approve_once", "approver_agent_id": "primary"},
    )
    assert resp.status_code == 200
    _call("agent-a", "executed", raw_cmd="pip install requests")
    _call("agent-a", "executed", raw_cmd="ls")

    rows = {r["raw_cmd"]: r for r in _audit(client)["rows"]}

    assert rows["pip install requests"]["approver_agent_id"] == "primary"
    assert rows["pip install requests"]["approve_decision"] == "approve_once"
    assert rows["ls"]["approver_agent_id"] is None


def test_audit_via_mcp_tool(client: TestClient, seeded: None) -> None:
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "shell_audit", "arguments": {"mode": "summary"}},
        },
    )
    assert resp.status_code == 200, resp.text
    assert "agent-a" in resp.text
