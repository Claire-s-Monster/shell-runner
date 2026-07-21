"""Integration tests for template approval persistence flow.

End-to-end via FastAPI TestClient: approve_template / approve_template_global
decisions now persist and unlock auto-execution on subsequent requests.

Uses `nonexistent_cmd_xyz` as a reliable T3 (APPROVE_ONCE / fallthrough) command.
Agent `focused-ghc-ci-analyzer` has ALWAYS_APPROVE cap so it can execute anything
once a template is approved.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shell_runner.persistence import Persistence
from shell_runner.server import app


@pytest.fixture(autouse=True)
def _fresh_db_per_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the module-level db singleton for a fresh per-test instance."""
    fresh = Persistence(db_path=tmp_path / "test.sqlite3")
    monkeypatch.setattr("shell_runner.server.db", fresh)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# Agents used in tests
_AGENT_A = "focused-ghc-ci-analyzer"   # ALWAYS_APPROVE cap
_AGENT_B = "focused-patch-generator"   # ALWAYS_APPROVE cap
_T3_CMD = "nonexistent_cmd_xyz --flag"
_T3_CWD = "/tmp"


def _execute(client: TestClient, command: str, agent_id: str) -> dict:
    resp = client.post(
        "/execute",
        json={"command": command, "cwd": _T3_CWD, "agent_id": agent_id},
    )
    assert resp.status_code == 200
    return resp.json()


def _classify(client: TestClient, command: str, agent_id: str) -> dict:
    resp = client.post(
        "/classify",
        json={"command": command, "cwd": _T3_CWD, "agent_id": agent_id},
    )
    assert resp.status_code == 200
    return resp.json()


def _approve(
    client: TestClient,
    prompt_id: str,
    decision: str,
    promote_to_tier: int | None = None,
) -> dict:
    body: dict = {"prompt_id": prompt_id, "decision": decision}
    if promote_to_tier is not None:
        body["promote_to_tier"] = promote_to_tier
    resp = client.post("/approve_pending", json=body)
    return resp


# ---------------------------------------------------------------------------
# Basic approve_template response
# ---------------------------------------------------------------------------


def test_approve_template_marks_template_promoted_true(client: TestClient) -> None:
    # First call yields prompt_required
    data = _execute(client, _T3_CMD, _AGENT_A)
    assert data["decision"] == "prompt_required"
    prompt_id = data["prompt"]["id"]

    resp = _approve(client, prompt_id, "approve_template", promote_to_tier=2)
    assert resp.status_code == 200
    result = resp.json()
    assert result["applied"] is True
    assert result["template_promoted"] is True
    assert result["catalog_entry_id"] is not None


# ---------------------------------------------------------------------------
# Global approval unlocks for a different agent
# ---------------------------------------------------------------------------


def test_approve_template_global_unlocks_for_different_agent(client: TestClient) -> None:
    # Agent A triggers the prompt
    data = _execute(client, _T3_CMD, _AGENT_A)
    assert data["decision"] == "prompt_required"
    prompt_id = data["prompt"]["id"]

    # Approve globally
    resp = _approve(client, prompt_id, "approve_template_global", promote_to_tier=2)
    assert resp.status_code == 200
    assert resp.json()["template_promoted"] is True

    # Agent B's same command now auto-executes (gets executed or running, not prompt_required)
    data_b = _execute(client, _T3_CMD, _AGENT_B)
    assert data_b["decision"] in ("executed", "running")


# ---------------------------------------------------------------------------
# Agent-scoped approval does NOT unlock for a different agent
# ---------------------------------------------------------------------------


def test_approve_template_agent_only_does_not_unlock_other_agent(client: TestClient) -> None:
    # Agent A triggers prompt
    data = _execute(client, _T3_CMD, _AGENT_A)
    assert data["decision"] == "prompt_required"
    prompt_id = data["prompt"]["id"]

    # Agent-scoped approval for agent A only
    resp = _approve(client, prompt_id, "approve_template", promote_to_tier=2)
    assert resp.status_code == 200

    # Agent B still gets prompt_required (approval is agent-scoped)
    data_b = _execute(client, _T3_CMD, _AGENT_B)
    assert data_b["decision"] == "prompt_required"


# ---------------------------------------------------------------------------
# Default promote_to_tier is T2 (AUTO_CAPPED = 2)
# ---------------------------------------------------------------------------


def test_approve_template_default_promote_to_tier_is_T2(client: TestClient) -> None:
    data = _execute(client, _T3_CMD, _AGENT_A)
    assert data["decision"] == "prompt_required"
    prompt_id = data["prompt"]["id"]

    # No promote_to_tier supplied — should default to T2
    resp = _approve(client, prompt_id, "approve_template")
    assert resp.status_code == 200
    result = resp.json()
    assert result["template_promoted"] is True
    # Subsequent call with same agent should auto-execute at T2
    data2 = _execute(client, _T3_CMD, _AGENT_A)
    assert data2["decision"] in ("executed", "running")


# ---------------------------------------------------------------------------
# Validation: promote_to_tier=0 (DENY) is invalid
# ---------------------------------------------------------------------------


def test_approve_template_with_too_permissive_tier_returns_400(client: TestClient) -> None:
    data = _execute(client, _T3_CMD, _AGENT_A)
    assert data["decision"] == "prompt_required"
    prompt_id = data["prompt"]["id"]

    resp = _approve(client, prompt_id, "approve_template", promote_to_tier=0)
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "DENY" in detail or "invalid" in detail.lower()


# ---------------------------------------------------------------------------
# Validation: promote_to_tier >= original command_tier returns 400
# ---------------------------------------------------------------------------


def test_approve_template_with_non_promoting_tier_returns_400(client: TestClient) -> None:
    # T3 command → command_tier = 3 (APPROVE_ONCE)
    data = _execute(client, _T3_CMD, _AGENT_A)
    assert data["decision"] == "prompt_required"
    original_tier = data["prompt"]["tier"]  # effective tier (may be capped)
    prompt_id = data["prompt"]["id"]

    # promote_to_tier = 3 (same as original) should fail
    resp = _approve(client, prompt_id, "approve_template", promote_to_tier=3)
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "promote_to_tier" in detail or "permissive" in detail.lower()


# ---------------------------------------------------------------------------
# SECURITY (issue #26 P2 hardening): no promotion may exceed AUTO_CAPPED.
# ---------------------------------------------------------------------------


def test_approve_template_promote_to_auto_log_rejected(client: TestClient) -> None:
    data = _execute(client, _T3_CMD, _AGENT_A)
    assert data["decision"] == "prompt_required"
    prompt_id = data["prompt"]["id"]

    # AUTO_LOG (tier 1) exceeds the promotion ceiling.
    resp = _approve(client, prompt_id, "approve_template", promote_to_tier=1)
    assert resp.status_code == 400
    assert "AUTO_CAPPED" in resp.json()["detail"]


def test_approve_template_promote_to_auto_capped_still_succeeds(client: TestClient) -> None:
    data = _execute(client, _T3_CMD, _AGENT_A)
    assert data["decision"] == "prompt_required"
    prompt_id = data["prompt"]["id"]

    resp = _approve(client, prompt_id, "approve_template", promote_to_tier=2)
    assert resp.status_code == 200
    assert resp.json()["template_promoted"] is True


# ---------------------------------------------------------------------------
# /classify reflects template approval (dry-run)
# ---------------------------------------------------------------------------


def test_classify_dry_run_reflects_template_approval(client: TestClient) -> None:
    # Before approval: classify shows APPROVE_ONCE (tier 3)
    cls_before = _classify(client, _T3_CMD, _AGENT_A)
    assert cls_before["effective_tier"] == 3  # APPROVE_ONCE

    # Create prompt then approve
    data = _execute(client, _T3_CMD, _AGENT_A)
    assert data["decision"] == "prompt_required"
    prompt_id = data["prompt"]["id"]
    resp = _approve(client, prompt_id, "approve_template", promote_to_tier=2)
    assert resp.status_code == 200

    # After approval: classify shows AUTO_CAPPED (tier 2)
    cls_after = _classify(client, _T3_CMD, _AGENT_A)
    assert cls_after["effective_tier"] == 2  # AUTO_CAPPED
    assert cls_after["decision_preview"] == "would_execute"
