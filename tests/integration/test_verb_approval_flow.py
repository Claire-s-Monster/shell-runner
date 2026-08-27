"""Integration tests for verb-level approval promotion and approve_once
durability (issue #26 P2 Part A / B / precedence).

Uses `nonexistent_cmd_xyz` as a reliable T3 (APPROVE_ONCE / fallthrough)
command, same convention as test_template_approval_flow.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import shell_runner.server as server_module
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
_AGENT_A = "focused-ghc-ci-analyzer"  # ALWAYS_APPROVE cap — no cap restriction
_AGENT_CAPPED = "focused-shell-runner"  # APPROVE_ONCE cap — restrictive


def _execute(client: TestClient, command: str, cwd: str, agent_id: str) -> dict:
    resp = client.post(
        "/execute",
        json={"command": command, "cwd": cwd, "agent_id": agent_id},
    )
    assert resp.status_code == 200
    return resp.json()


def _approve(client: TestClient, prompt_id: str, decision: str, promote_to_tier: int | None = None):
    body: dict = {
        "prompt_id": prompt_id,
        "decision": decision,
        "approver_agent_id": "primary",
    }
    if promote_to_tier is not None:
        body["promote_to_tier"] = promote_to_tier
    return client.post("/approve_pending", json=body)


# ---------------------------------------------------------------------------
# 1. Verb promotion happy path: a different-args variant of the same verb
#    inside the approved cwd (or a subdirectory) auto-executes.
# ---------------------------------------------------------------------------


def test_approve_verb_promotion_auto_executes_variant_in_subdir(
    client: TestClient, tmp_path: Path
) -> None:
    cwd = str(tmp_path)
    data = _execute(client, "nonexistent_cmd_xyz --flag", cwd, _AGENT_A)
    assert data["decision"] == "prompt_required"
    prompt_id = data["prompt"]["id"]

    resp = _approve(client, prompt_id, "approve_verb", promote_to_tier=2)
    assert resp.status_code == 200
    body = resp.json()
    assert body["verb_promoted"] is True
    assert body["promoted_tier"] == 2

    sub = tmp_path / "sub"
    sub.mkdir()
    data2 = _execute(client, "nonexistent_cmd_xyz --different-flag", str(sub), _AGENT_A)
    assert data2["decision"] in ("executed", "running")


# ---------------------------------------------------------------------------
# 2. Verb promotion cwd bound: same verb OUTSIDE cwd_prefix still prompts.
# ---------------------------------------------------------------------------


def test_approve_verb_promotion_does_not_apply_outside_cwd_prefix(
    client: TestClient, tmp_path: Path
) -> None:
    cwd = str(tmp_path)
    data = _execute(client, "nonexistent_cmd_xyz --flag", cwd, _AGENT_A)
    assert data["decision"] == "prompt_required"
    prompt_id = data["prompt"]["id"]

    resp = _approve(client, prompt_id, "approve_verb", promote_to_tier=2)
    assert resp.status_code == 200
    assert resp.json()["verb_promoted"] is True

    outside = tmp_path.parent / f"sibling-{tmp_path.name}"
    # must exist: the T3/T4 path now rejects an unhonourable cwd before
    # minting a prompt (issue #36), so a nonexistent dir would return
    # "denied" and this test is about cwd-PREFIX scope, not cwd existence.
    outside.mkdir()
    data2 = _execute(client, "nonexistent_cmd_xyz --different-flag", str(outside), _AGENT_A)
    assert data2["decision"] == "prompt_required"


# ---------------------------------------------------------------------------
# 3. SECURITY: a hard-DENY command is never auto-executed even if a
#    verb_approval row exists for its verb.
# ---------------------------------------------------------------------------


def test_deny_command_never_promoted_by_verb_approval(client: TestClient, tmp_path: Path) -> None:
    cwd = str(tmp_path)
    # Simulate a pre-existing verb approval for "sudo" under this cwd subtree
    # (this can never happen via the normal approve_verb flow since a DENY
    # command never reaches prompt_required — but the guard must hold
    # regardless of how the row got there).
    server_module.db.create_verb_approval(
        verb="sudo", cwd_prefix=cwd, agent_id=None, approved_tier=1
    )
    data = _execute(client, "sudo apt-get update", cwd, _AGENT_A)
    assert data["decision"] == "denied"


# ---------------------------------------------------------------------------
# 3b. SECURITY: a hard-DENY command is never auto-executed even if a
#     template_approval row exists for its exact normalized template.
# ---------------------------------------------------------------------------


def test_deny_command_never_promoted_by_template_approval(
    client: TestClient, tmp_path: Path
) -> None:
    cwd = str(tmp_path)
    cmd = "sudo apt-get update"

    # Resolve the real normalized template via /classify so the approval row
    # is guaranteed to match what the classifier will look up. Also assert the
    # DENY precondition — otherwise this test could pass vacuously if "sudo"
    # ever stopped classifying as DENY.
    resp = client.post(
        "/classify",
        json={"command": cmd, "cwd": cwd, "agent_id": _AGENT_A},
    )
    assert resp.status_code == 200
    cls_body = resp.json()
    assert cls_body["command_tier"] == 0, "precondition: sudo must classify as DENY"
    template = cls_body["template"]

    # Simulate a pre-existing template approval (can never happen via the
    # normal flow, since a DENY command never reaches prompt_required — but
    # the guard must hold regardless of how the row got there).
    server_module.db.create_template_approval(
        template=template, agent_id=None, approved_tier=1
    )

    data = _execute(client, cmd, cwd, _AGENT_A)
    assert data["decision"] == "denied"


# ---------------------------------------------------------------------------
# 4. approve_once durability: a token-less re-submit matching (cmd, cwd,
#    agent) executes once; a second token-less attempt re-prompts.
# ---------------------------------------------------------------------------


def test_approve_once_durability_single_use_tokenless_execute(
    client: TestClient, tmp_path: Path
) -> None:
    cwd = str(tmp_path)
    cmd = "nonexistent_cmd_xyz --flag"
    data = _execute(client, cmd, cwd, _AGENT_A)
    assert data["decision"] == "prompt_required"
    prompt_id = data["prompt"]["id"]

    resp = _approve(client, prompt_id, "approve_once")
    assert resp.status_code == 200
    assert resp.json()["approve_token"] is not None

    # Token-less re-submit matching (cmd, cwd, agent) executes once.
    data2 = _execute(client, cmd, cwd, _AGENT_A)
    assert data2["decision"] in ("executed", "running")

    # Second token-less attempt: approval already consumed -> re-prompts.
    data3 = _execute(client, cmd, cwd, _AGENT_A)
    assert data3["decision"] == "prompt_required"


# ---------------------------------------------------------------------------
# 5. Cap preserved: a verb-promoted tier above the agent cap is still capped
#    down (existing behavior, re-applied on the verb-promotion path too).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 6. SECURITY (issue #26 P2 hardening): no promotion may exceed AUTO_CAPPED.
# ---------------------------------------------------------------------------


def test_approve_verb_promote_to_auto_log_rejected(client: TestClient, tmp_path: Path) -> None:
    cwd = str(tmp_path)
    data = _execute(client, "nonexistent_cmd_xyz --flag", cwd, _AGENT_A)
    assert data["decision"] == "prompt_required"
    prompt_id = data["prompt"]["id"]

    # AUTO_LOG (tier 1) exceeds the promotion ceiling.
    resp = _approve(client, prompt_id, "approve_verb", promote_to_tier=1)
    assert resp.status_code == 400
    assert "AUTO_CAPPED" in resp.json()["detail"]


def test_approve_verb_promote_to_auto_capped_still_succeeds(
    client: TestClient, tmp_path: Path
) -> None:
    cwd = str(tmp_path)
    data = _execute(client, "nonexistent_cmd_xyz --flag", cwd, _AGENT_A)
    assert data["decision"] == "prompt_required"
    prompt_id = data["prompt"]["id"]

    resp = _approve(client, prompt_id, "approve_verb", promote_to_tier=2)
    assert resp.status_code == 200
    assert resp.json()["verb_promoted"] is True


# ---------------------------------------------------------------------------
# 7. SECURITY (issue #26 P2 hardening): execution re-checks DENY against the
#    CURRENT catalog even for an already-consumed approve_token approval.
# ---------------------------------------------------------------------------


def test_execution_time_deny_recheck_refuses_stale_approval(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shell_runner.catalog import Tier
    from shell_runner.classifier import ClassificationResult

    cwd = str(tmp_path)
    cmd = "nonexistent_cmd_xyz --flag"
    data = _execute(client, cmd, cwd, _AGENT_A)
    assert data["decision"] == "prompt_required"
    prompt_id = data["prompt"]["id"]

    resp = _approve(client, prompt_id, "approve_once")
    assert resp.status_code == 200
    token = resp.json()["approve_token"]
    assert token is not None

    def _fake_deny_classify(command: str, cwd: str, agent_id: str | None, *, env=None):
        return ClassificationResult(
            tier=Tier.DENY,
            command_tier=Tier.DENY,
            agent_cap=Tier.ALWAYS_APPROVE,
            matched_rule=None,
            template=command,
            segments=(),
            normalizer_warnings=(),
            decision_path=("simulated_catalog_deny",),
        )

    # Simulate a catalog update (between approval and execution) that now
    # hard-DENYs this command -- the stale approve_token must not bypass it.
    monkeypatch.setattr("shell_runner.server.classify", _fake_deny_classify)

    exec_resp = client.post(
        "/execute",
        json={"command": cmd, "cwd": cwd, "agent_id": _AGENT_A, "approve_token": token},
    )
    assert exec_resp.status_code == 200
    assert exec_resp.json()["decision"] == "denied"


def test_verb_promotion_still_capped_by_agent_cap(client: TestClient, tmp_path: Path) -> None:
    cwd = str(tmp_path)
    data = _execute(client, "nonexistent_cmd_xyz --flag", cwd, _AGENT_CAPPED)
    assert data["decision"] == "prompt_required"
    prompt_id = data["prompt"]["id"]

    # Promote the verb to AUTO_CAPPED (tier 2) — the ceiling for any promotion
    # (issue #26 P2 hardening) — still more permissive than the APPROVE_ONCE
    # cap of _AGENT_CAPPED.
    resp = _approve(client, prompt_id, "approve_verb", promote_to_tier=2)
    assert resp.status_code == 200
    assert resp.json()["verb_promoted"] is True

    # A different-args variant is promoted at the verb level, but the agent's
    # own cap (APPROVE_ONCE) still restricts the effective tier -> re-prompts
    # rather than auto-executing.
    data2 = _execute(client, "nonexistent_cmd_xyz --other-flag", cwd, _AGENT_CAPPED)
    assert data2["decision"] == "prompt_required"
