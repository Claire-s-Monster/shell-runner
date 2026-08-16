"""Integration tests for shell-runner HTTP API via FastAPI TestClient."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shell_runner.catalog import all_rules
from shell_runner.persistence import Persistence
from shell_runner.server import app, db


@pytest.fixture(autouse=True)
def _fresh_db_per_test(fresh_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the module-level `db` singleton for a fresh instance per test."""
    fresh = Persistence(db_path=fresh_db)
    monkeypatch.setattr("shell_runner.server.db", fresh)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# /execute tests
# ---------------------------------------------------------------------------


def test_execute_ls_tmp_auto_executes(client: TestClient) -> None:
    """ls /tmp with a high-trust agent (ALWAYS_APPROVE cap) should execute."""
    resp = client.post(
        "/execute",
        json={
            "command": "ls /tmp",
            "cwd": "/tmp",
            "agent_id": "focused-ghc-ci-analyzer",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "executed"
    assert data["exit_code"] == 0


def test_execute_rm_rf_root_is_denied(client: TestClient) -> None:
    resp = client.post(
        "/execute",
        json={
            "command": "rm -rf /",
            "cwd": "/tmp",
            "agent_id": "focused-ghc-ci-analyzer",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "denied"
    assert data["tier"] == 0


def test_execute_pip_install_prompts(client: TestClient) -> None:
    resp = client.post(
        "/execute",
        json={
            "command": "pip install requests",
            "cwd": "/tmp",
            "agent_id": "focused-ghc-ci-analyzer",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "prompt_required"
    assert data["tier"] >= 3
    assert data["prompt"] is not None
    assert "id" in data["prompt"]


def test_execute_sudo_is_denied(client: TestClient) -> None:
    resp = client.post(
        "/execute",
        json={
            "command": "sudo apt update",
            "cwd": "/tmp",
            "agent_id": "focused-ghc-ci-analyzer",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "denied"


# ---------------------------------------------------------------------------
# /classify tests
# ---------------------------------------------------------------------------


def test_classify_ls_would_execute(client: TestClient) -> None:
    resp = client.post(
        "/classify",
        json={
            "command": "ls /tmp",
            "cwd": "/tmp",
            "agent_id": "focused-ghc-ci-analyzer",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision_preview"] == "would_execute"


def test_classify_pip_install_would_prompt(client: TestClient) -> None:
    resp = client.post(
        "/classify",
        json={
            "command": "pip install requests",
            "cwd": "/tmp",
            "agent_id": "focused-ghc-ci-analyzer",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision_preview"] == "would_prompt"


def test_classify_rm_rf_root_would_deny(client: TestClient) -> None:
    resp = client.post(
        "/classify",
        json={
            "command": "rm -rf /",
            "cwd": "/tmp",
            "agent_id": "focused-ghc-ci-analyzer",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision_preview"] == "would_deny"


# ---------------------------------------------------------------------------
# /approve_pending tests
# ---------------------------------------------------------------------------


def test_approve_pending_approve_once_returns_token(client: TestClient, fresh_db: Path) -> None:
    import shell_runner.server as srv

    # Create a pending prompt directly
    db_inst: Persistence = srv.db
    pid = db_inst.create_pending_prompt(
        agent_id="test-agent",
        cwd="/tmp",
        raw_cmd="pip install x",
        normalized_template="pip install <pkg>",
        command_tier=4,
        matched_rule_category="package",
        decision_path=["T4 match"],
    )

    resp = client.post(
        "/approve_pending",
        json={"prompt_id": pid, "decision": "approve_once", "approver_agent_id": "primary"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["applied"] is True
    assert data["approve_token"] is not None
    assert len(data["approve_token"]) == 36


def test_approve_pending_deny_returns_no_token(client: TestClient, fresh_db: Path) -> None:
    import shell_runner.server as srv

    db_inst: Persistence = srv.db
    pid = db_inst.create_pending_prompt(
        agent_id="test-agent",
        cwd="/tmp",
        raw_cmd="pip install x",
        normalized_template="pip install <pkg>",
        command_tier=4,
        matched_rule_category=None,
        decision_path=[],
    )

    resp = client.post(
        "/approve_pending",
        json={"prompt_id": pid, "decision": "deny", "approver_agent_id": "primary"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["applied"] is True
    assert data["approve_token"] is None


# ---------------------------------------------------------------------------
# /approve_pending approver-identity guard (issue #29, Layer-1)
# ---------------------------------------------------------------------------


def test_approve_pending_self_approval_rejected(client: TestClient, fresh_db: Path) -> None:
    """approver_agent_id equal to the prompt's executing agent_id -> 403."""
    import shell_runner.server as srv

    db_inst: Persistence = srv.db
    pid = db_inst.create_pending_prompt(
        agent_id="test-agent",
        cwd="/tmp",
        raw_cmd="pip install x",
        normalized_template="pip install <pkg>",
        command_tier=4,
        matched_rule_category="package",
        decision_path=["T4 match"],
    )

    resp = client.post(
        "/approve_pending",
        json={
            "prompt_id": pid,
            "decision": "approve_once",
            "approver_agent_id": "test-agent",
        },
    )
    assert resp.status_code == 403
    assert "self-approval" in resp.json()["detail"]


def test_approve_pending_non_deny_approver_rejected(client: TestClient, fresh_db: Path) -> None:
    """approver_agent_id without DENY capability -> 403."""
    import shell_runner.server as srv

    db_inst: Persistence = srv.db
    pid = db_inst.create_pending_prompt(
        agent_id="test-agent",
        cwd="/tmp",
        raw_cmd="pip install x",
        normalized_template="pip install <pkg>",
        command_tier=4,
        matched_rule_category="package",
        decision_path=["T4 match"],
    )

    resp = client.post(
        "/approve_pending",
        json={
            "prompt_id": pid,
            "decision": "approve_once",
            # Distinct from the executor, but not a DENY-cap identity.
            "approver_agent_id": "focused-shell-runner",
        },
    )
    assert resp.status_code == 403
    assert "approval capability" in resp.json()["detail"]


def test_approve_pending_primary_approver_succeeds(client: TestClient, fresh_db: Path) -> None:
    """approver_agent_id = 'primary' (DENY cap), distinct from executor -> succeeds."""
    import shell_runner.server as srv

    db_inst: Persistence = srv.db
    pid = db_inst.create_pending_prompt(
        agent_id="test-agent",
        cwd="/tmp",
        raw_cmd="pip install x",
        normalized_template="pip install <pkg>",
        command_tier=4,
        matched_rule_category="package",
        decision_path=["T4 match"],
    )

    resp = client.post(
        "/approve_pending",
        json={
            "prompt_id": pid,
            "decision": "approve_once",
            "approver_agent_id": "primary",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["applied"] is True
    assert data["approve_token"] is not None


def test_approve_pending_omitted_approver_is_rejected(
    client: TestClient, fresh_db: Path
) -> None:
    """approver_agent_id omitted -> 403 (issue #36).

    This test previously asserted the omitted path SUCCEEDED; #36 made that
    path 403 because it skipped the self-approval and capability checks
    entirely, making omission strictly more permissive than supplying an
    identity.
    """
    import shell_runner.server as srv

    db_inst: Persistence = srv.db
    pid = db_inst.create_pending_prompt(
        agent_id="test-agent",
        cwd="/tmp",
        raw_cmd="pip install x",
        normalized_template="pip install <pkg>",
        command_tier=4,
        matched_rule_category="package",
        decision_path=["T4 match"],
    )

    resp = client.post(
        "/approve_pending",
        json={"prompt_id": pid, "decision": "approve_once"},
    )
    assert resp.status_code == 403
    assert "approver_agent_id" in resp.json()["detail"]
    assert "required" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Full approve_token flow
# ---------------------------------------------------------------------------


def test_execute_with_valid_approve_token_executes(client: TestClient, fresh_db: Path) -> None:
    import shell_runner.server as srv

    db_inst: Persistence = srv.db
    pid = db_inst.create_pending_prompt(
        agent_id="test-agent",
        cwd="/tmp",
        raw_cmd="echo approved",
        normalized_template="echo <word>",
        command_tier=4,
        matched_rule_category=None,
        decision_path=[],
    )
    token = db_inst.approve_prompt(prompt_id=pid, decision="approve_once")
    assert token is not None

    resp = client.post(
        "/execute",
        json={
            "command": "echo approved",
            "cwd": "/tmp",
            "agent_id": "test-agent",
            "approve_token": token,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "executed"
    assert data["exit_code"] == 0
    assert "approved" in data["stdout"]


def test_execute_with_bad_approve_token_returns_403(client: TestClient) -> None:
    resp = client.post(
        "/execute",
        json={
            "command": "echo hi",
            "cwd": "/tmp",
            "agent_id": "test-agent",
            "approve_token": "00000000-0000-0000-0000-000000000000",
        },
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def test_health_returns_ok(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["catalog_size"] == len(all_rules())
    assert isinstance(data["total_calls_24h"], int)
