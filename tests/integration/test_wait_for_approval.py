"""Integration tests for shell_wait prompt mode: the requesting agent waits for
a primary session's decision and receives the approve_token.

Uses the FastAPI TestClient (in-process ASGI) with a fresh per-test Persistence,
matching test_approve_guard.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import shell_runner.server as srv
from shell_runner.persistence import Persistence
from shell_runner.server import app

AGENT = "waiting-agent"
COMMAND = "expr 40 + 2"  # unknown verb -> T3 prompt; harmless and fast to run


@pytest.fixture(autouse=True)
def _fresh_db_per_test(fresh_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shell_runner.server.db", Persistence(db_path=fresh_db))
    monkeypatch.setattr("shell_runner.server.WAIT_POLL_INTERVAL_S", 0.01)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _request_prompt(client: TestClient, cwd: Path, agent_id: str = AGENT) -> str:
    resp = client.post("/execute", json={"command": COMMAND, "cwd": str(cwd), "agent_id": agent_id})
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "prompt_required"
    return data["prompt"]["id"]


def _decide(client: TestClient, prompt_id: str, decision: str = "approve_once") -> dict:
    resp = client.post(
        "/approve_pending",
        json={"prompt_id": prompt_id, "decision": decision, "approver_agent_id": "primary"},
    )
    assert resp.status_code == 200
    return resp.json()


def _wait(
    client: TestClient, prompt_id: str, agent_id: str | None = AGENT, wait_s: float = 0
) -> dict:
    body: dict = {"prompt_id": prompt_id, "wait_s": wait_s}
    if agent_id is not None:
        body["agent_id"] = agent_id
    resp = client.post("/tools/shell_wait", json=body)
    assert resp.status_code == 200
    return resp.json()


def test_prompt_required_carries_next_steps(client: TestClient, tmp_path: Path) -> None:
    resp = client.post(
        "/execute", json={"command": COMMAND, "cwd": str(tmp_path), "agent_id": AGENT}
    )
    prompt = resp.json()["prompt"]
    assert prompt["id"] in prompt["next_steps"]
    assert "shell_wait(prompt_id=" in prompt["next_steps"]
    assert "shell_approve_pending" in prompt["next_steps"]


def test_full_flow_wait_returns_token_and_execute_succeeds(
    client: TestClient, tmp_path: Path
) -> None:
    pid = _request_prompt(client, tmp_path)
    assert _wait(client, pid)["state"] == "waiting"  # before approval

    approved = _decide(client, pid)
    waited = _wait(client, pid)
    assert waited["state"] == "approved"
    assert waited["approve_token"] == approved["approve_token"]

    resp = client.post(
        "/execute",
        json={
            "command": COMMAND,
            "cwd": str(tmp_path),
            "agent_id": AGENT,
            "approve_token": waited["approve_token"],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["decision"] == "executed"
    assert resp.json()["stdout"].strip() == "42"

    # Single-use: the token is spent, so the prompt no longer yields one.
    again = _wait(client, pid)
    assert again["state"] == "error"
    assert again["approve_token"] is None
    reuse = client.post(
        "/execute",
        json={
            "command": COMMAND,
            "cwd": str(tmp_path),
            "agent_id": AGENT,
            "approve_token": waited["approve_token"],
        },
    )
    assert reuse.status_code == 403


def test_wait_unblocks_when_approved_during_the_wait(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _request_prompt(client, tmp_path)
    original = srv.db.get_pending_prompt
    calls = {"n": 0}

    def _approve_on_third_read(prompt_id: str) -> dict | None:
        calls["n"] += 1
        if calls["n"] == 3:  # 1: ownership check, 2: first poll, 3: after one sleep
            srv.db.approve_prompt(prompt_id=prompt_id, decision="approve_once")
        return original(prompt_id)

    monkeypatch.setattr(srv.db, "get_pending_prompt", _approve_on_third_read)
    waited = _wait(client, pid, wait_s=5)
    assert waited["state"] == "approved"
    assert waited["approve_token"]


def test_other_agent_is_rejected_and_gets_no_token(client: TestClient, tmp_path: Path) -> None:
    pid = _request_prompt(client, tmp_path)
    token = _decide(client, pid)["approve_token"]
    waited = _wait(client, pid, agent_id="some-other-agent")
    assert waited["state"] == "error"
    assert waited["approve_token"] is None
    assert token not in str(waited)
    # Indistinguishable from an unknown prompt.
    unknown = _wait(client, "no-such-prompt", agent_id="some-other-agent")
    assert unknown["error"] == waited["error"].replace(pid, "no-such-prompt")


def test_missing_agent_id_is_rejected(client: TestClient, tmp_path: Path) -> None:
    pid = _request_prompt(client, tmp_path)
    _decide(client, pid)
    waited = _wait(client, pid, agent_id=None)
    assert waited["state"] == "error"
    assert waited["approve_token"] is None


def test_denied_state(client: TestClient, tmp_path: Path) -> None:
    pid = _request_prompt(client, tmp_path)
    _decide(client, pid, "deny")
    waited = _wait(client, pid)
    assert waited["state"] == "denied"
    assert waited["approve_token"] is None


def test_unanswered_prompt_past_ttl_is_expired(client: TestClient) -> None:
    pid = srv.db.create_pending_prompt(
        agent_id=AGENT,
        cwd="/tmp",
        raw_cmd=COMMAND,
        normalized_template="expr <num> + <num>",
        command_tier=3,
        matched_rule_category=None,
        decision_path=[],
        ttl_seconds=-1,
    )
    waited = _wait(client, pid)
    assert waited["state"] == "expired"
    assert waited["approve_token"] is None


def test_approved_but_expired_prompt_withholds_token(client: TestClient) -> None:
    pid = srv.db.create_pending_prompt(
        agent_id=AGENT,
        cwd="/tmp",
        raw_cmd=COMMAND,
        normalized_template="expr <num> + <num>",
        command_tier=3,
        matched_rule_category=None,
        decision_path=[],
        ttl_seconds=-1,
    )
    _decide(client, pid, "approve_template")  # does not extend the TTL
    waited = _wait(client, pid)
    assert waited["state"] == "expired"
    assert waited["approve_token"] is None


def test_swept_prompt_reports_error_not_found(client: TestClient, tmp_path: Path) -> None:
    pid = _request_prompt(client, tmp_path)
    with srv.db._write_conn() as conn:
        conn.execute("DELETE FROM pending_prompts WHERE id = ?", (pid,))
    assert _wait(client, pid)["state"] == "error"


def test_job_id_and_prompt_id_are_mutually_exclusive(client: TestClient) -> None:
    both = client.post(
        "/tools/shell_wait", json={"job_id": "j", "prompt_id": "p", "agent_id": AGENT}
    ).json()
    neither = client.post("/tools/shell_wait", json={}).json()
    assert both["state"] == neither["state"] == "error"
    assert "exactly one" in both["error"]
