"""Integration tests for the approve-identity guard, approval echo, /pending
read-only inspection, and token-binding regressions (issues #36, #37).

Uses the FastAPI TestClient (in-process ASGI), matching the conventions in
test_mcp_endpoint.py / test_server.py: a fresh per-test Persistence instance
swapped onto shell_runner.server.db, and the autouse
tests/integration/conftest.py `_allow_all_cwd_roots` fixture (jail root "/")
for tests that don't need a restricted cwd jail.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import shell_runner.server as srv
from shell_runner.normalizer import normalize
from shell_runner.persistence import Persistence
from shell_runner.server import app

# ---------------------------------------------------------------------------
# fixtures (mirrors test_server.py / test_mcp_endpoint.py conventions)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_db_per_test(fresh_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the module-level `db` singleton for a fresh instance per test."""
    fresh = Persistence(db_path=fresh_db)
    monkeypatch.setattr("shell_runner.server.db", fresh)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _create_prompt(
    *,
    agent_id: str = "test-agent",
    cwd: str = "/tmp",
    raw_cmd: str = "pip install requests",
    normalized_template: str = "pip install <pkg>",
    command_tier: int = 4,
    category: str | None = "package",
) -> str:
    db_inst: Persistence = srv.db
    return db_inst.create_pending_prompt(
        agent_id=agent_id,
        cwd=cwd,
        raw_cmd=raw_cmd,
        normalized_template=normalized_template,
        command_tier=command_tier,
        matched_rule_category=category,
        decision_path=["T4 match"],
    )


# ---------------------------------------------------------------------------
# A) Approver guard (issue #36)
# ---------------------------------------------------------------------------


def test_approve_pending_omitted_approver_is_rejected(client: TestClient) -> None:
    """approver_agent_id OMITTED -> 403 (headline fix: this used to succeed
    unconditionally, skipping the self-approval and capability checks)."""
    pid = _create_prompt(agent_id="test-agent")
    resp = client.post("/approve_pending", json={"prompt_id": pid, "decision": "approve_once"})
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert "approver_agent_id" in detail
    assert "required" in detail


def test_approve_pending_primary_approver_succeeds(client: TestClient) -> None:
    pid = _create_prompt(agent_id="test-agent")
    resp = client.post(
        "/approve_pending",
        json={"prompt_id": pid, "decision": "approve_once", "approver_agent_id": "primary"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["applied"] is True
    assert data["approve_token"] is not None


def test_approve_pending_suffixed_primary_id_succeeds(client: TestClient) -> None:
    """issue #36 repro: 'primary-session-482f1f98' used to be rejected by the
    guard (it was routed through lookup_agent_cap, which does not recognise
    suffixed primary ids) despite being a genuine primary session identity.
    It must now succeed."""
    pid = _create_prompt(agent_id="test-agent")
    resp = client.post(
        "/approve_pending",
        json={
            "prompt_id": pid,
            "decision": "approve_once",
            "approver_agent_id": "primary-session-482f1f98",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["applied"] is True
    assert data["approve_token"] is not None


def test_approve_pending_self_approval_rejected(client: TestClient) -> None:
    """approver_agent_id equal to the prompt's executing agent_id -> 403."""
    pid = _create_prompt(agent_id="focused-code-modifier")
    resp = client.post(
        "/approve_pending",
        json={
            "prompt_id": pid,
            "decision": "approve_once",
            "approver_agent_id": "focused-code-modifier",
        },
    )
    assert resp.status_code == 403
    assert "self-approval" in resp.json()["detail"]


def test_approve_pending_non_primary_approver_lacks_capability(client: TestClient) -> None:
    """A real, non-primary agent (distinct from the executor) -> 403 lacking
    approval capability."""
    pid = _create_prompt(agent_id="test-agent")
    resp = client.post(
        "/approve_pending",
        json={
            "prompt_id": pid,
            "decision": "approve_once",
            "approver_agent_id": "focused-code-modifier",
        },
    )
    assert resp.status_code == 403
    assert "approval capability" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# B) Approval echo (issue #37)
# ---------------------------------------------------------------------------


def test_approve_pending_echoes_approved_detail(client: TestClient) -> None:
    pid = _create_prompt(
        agent_id="test-agent",
        cwd="/tmp",
        raw_cmd="pip install requests",
        normalized_template="pip install <pkg>",
        command_tier=4,
    )
    resp = client.post(
        "/approve_pending",
        json={"prompt_id": pid, "decision": "approve_once", "approver_agent_id": "primary"},
    )
    assert resp.status_code == 200
    data = resp.json()
    approved = data["approved"]
    assert approved is not None
    assert approved["raw_cmd"] == "pip install requests"
    assert approved["normalized_template"] == "pip install <pkg>"
    assert approved["cwd"] == "/tmp"
    assert approved["agent_id"] == "test-agent"
    assert approved["command_tier"] == 4


def test_approve_pending_scope_warning_present_for_path_placeholder(
    client: TestClient, tmp_path: Path
) -> None:
    """When normalized_template contains a path placeholder, scope_warning is
    non-null and matches approved.scope_warning. Use a real absolute-path
    command run through the actual normalizer so the placeholder is genuine."""
    cwd = str(tmp_path)
    # "touch" is not in READ_ONLY_FILE_VERBS (unlike e.g. "cat"), so the
    # normalizer's path classification survives into the final template
    # instead of being collapsed to <file_arg>.
    raw_cmd = "touch /opt/some/other/file.txt"
    template = normalize(raw_cmd, cwd).template
    assert "<abs_path>" in template  # sanity: normalizer really produced a placeholder

    pid = _create_prompt(
        agent_id="test-agent",
        cwd=cwd,
        raw_cmd=raw_cmd,
        normalized_template=template,
        command_tier=3,
        category="fs-read",
    )
    resp = client.post(
        "/approve_pending",
        json={"prompt_id": pid, "decision": "approve_once", "approver_agent_id": "primary"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["scope_warning"] is not None
    assert data["approved"]["scope_warning"] == data["scope_warning"]
    assert "<abs_path>" in data["scope_warning"]


def test_approve_pending_scope_warning_null_without_placeholder(client: TestClient) -> None:
    pid = _create_prompt(
        agent_id="test-agent",
        cwd="/tmp",
        raw_cmd="pip install requests",
        normalized_template="pip install <pkg>",
        command_tier=4,
    )
    resp = client.post(
        "/approve_pending",
        json={"prompt_id": pid, "decision": "approve_once", "approver_agent_id": "primary"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["scope_warning"] is None


# ---------------------------------------------------------------------------
# C) /pending (issue #37)
# ---------------------------------------------------------------------------


def test_pending_with_prompt_id_returns_full_detail(client: TestClient) -> None:
    pid = _create_prompt(
        agent_id="test-agent",
        cwd="/tmp",
        raw_cmd="pip install requests",
        normalized_template="pip install <pkg>",
        command_tier=4,
    )
    resp = client.post("/pending", json={"prompt_id": pid})
    assert resp.status_code == 200
    prompts = resp.json()["prompts"]
    assert len(prompts) == 1
    detail = prompts[0]
    assert detail["id"] == pid
    assert detail["raw_cmd"] == "pip install requests"
    assert detail["normalized_template"] == "pip install <pkg>"
    assert detail["cwd"] == "/tmp"
    assert detail["agent_id"] == "test-agent"
    assert detail["command_tier"] == 4


def test_pending_with_unknown_prompt_id_returns_404(client: TestClient) -> None:
    resp = client.post("/pending", json={"prompt_id": "00000000-0000-0000-0000-000000000000"})
    assert resp.status_code == 404


def test_pending_without_prompt_id_lists_outstanding(client: TestClient) -> None:
    pid1 = _create_prompt(agent_id="test-agent", raw_cmd="pip install a")
    pid2 = _create_prompt(agent_id="test-agent", raw_cmd="pip install b")
    resp = client.post("/pending", json={})
    assert resp.status_code == 200
    ids = {p["id"] for p in resp.json()["prompts"]}
    assert {pid1, pid2} <= ids


def test_pending_does_not_mutate_prompt(client: TestClient) -> None:
    """Calling /pending must not change approve_decision/consumed_at, and the
    prompt must still be approvable afterwards."""
    pid = _create_prompt(agent_id="test-agent")

    resp = client.post("/pending", json={"prompt_id": pid})
    assert resp.status_code == 200

    row = srv.db.get_pending_prompt(pid)
    assert row["approve_decision"] is None
    assert row["consumed_at"] is None

    approve_resp = client.post(
        "/approve_pending",
        json={"prompt_id": pid, "decision": "approve_once", "approver_agent_id": "primary"},
    )
    assert approve_resp.status_code == 200
    assert approve_resp.json()["approve_token"] is not None


# ---------------------------------------------------------------------------
# D) Token binding no longer burns the approval (issue #36, secondary obs. 1)
# ---------------------------------------------------------------------------


def test_execute_token_wrong_cwd_then_correct_cwd_succeeds(
    client: TestClient, tmp_path: Path
) -> None:
    bound_cwd = str(tmp_path)
    wrong_cwd = str(tmp_path.parent)
    # A fast, local, side-effect-free command: this path actually invokes the
    # real subprocess executor on success, so avoid anything that touches the
    # network (e.g. pip install).
    raw_cmd = "echo approved"

    pid = _create_prompt(
        agent_id="focused-code-modifier",
        cwd=bound_cwd,
        raw_cmd=raw_cmd,
        normalized_template="echo <word>",
        command_tier=4,
    )
    approve_resp = client.post(
        "/approve_pending",
        json={"prompt_id": pid, "decision": "approve_once", "approver_agent_id": "primary"},
    )
    assert approve_resp.status_code == 200
    token = approve_resp.json()["approve_token"]
    assert token is not None

    # Wrong cwd -> 403, detail names cwd and shows the bound value.
    bad_resp = client.post(
        "/execute",
        json={
            "command": raw_cmd,
            "cwd": wrong_cwd,
            "agent_id": "focused-code-modifier",
            "approve_token": token,
        },
    )
    assert bad_resp.status_code == 403
    bad_detail = bad_resp.json()["detail"]
    assert "cwd" in bad_detail
    assert bound_cwd in bad_detail

    # issue #36 secondary-observation-1 regression: the failed mismatched
    # attempt above must NOT have consumed the token. Re-calling with the
    # correct command/cwd/agent using the SAME token must now succeed.
    good_resp = client.post(
        "/execute",
        json={
            "command": raw_cmd,
            "cwd": bound_cwd,
            "agent_id": "focused-code-modifier",
            "approve_token": token,
        },
    )
    assert good_resp.status_code == 200, good_resp.text
    assert good_resp.json()["decision"] == "executed"


def test_execute_token_reuse_after_success_rejected(client: TestClient, tmp_path: Path) -> None:
    bound_cwd = str(tmp_path)
    raw_cmd = "echo approved"

    pid = _create_prompt(
        agent_id="focused-code-modifier",
        cwd=bound_cwd,
        raw_cmd=raw_cmd,
        normalized_template="echo <word>",
        command_tier=4,
    )
    approve_resp = client.post(
        "/approve_pending",
        json={"prompt_id": pid, "decision": "approve_once", "approver_agent_id": "primary"},
    )
    token = approve_resp.json()["approve_token"]

    first = client.post(
        "/execute",
        json={
            "command": raw_cmd,
            "cwd": bound_cwd,
            "agent_id": "focused-code-modifier",
            "approve_token": token,
        },
    )
    assert first.status_code == 200
    assert first.json()["decision"] == "executed"

    second = client.post(
        "/execute",
        json={
            "command": raw_cmd,
            "cwd": bound_cwd,
            "agent_id": "focused-code-modifier",
            "approve_token": token,
        },
    )
    assert second.status_code == 403


# ---------------------------------------------------------------------------
# E) cwd rejected at prompt time (issue #36, secondary obs. 2)
# ---------------------------------------------------------------------------


def test_execute_cwd_outside_allowed_roots_denied_creates_no_prompt(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A T3/T4 command with a cwd outside the allowed roots is denied at
    prompt-creation time and no pending prompt is created for it."""
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    outside_cwd = str(tmp_path / "outside")

    import shell_runner.executor

    monkeypatch.setenv("SHELL_RUNNER_CWD_ROOT", str(allowed_root))
    importlib.reload(shell_runner.executor)
    try:
        resp = client.post(
            "/execute",
            json={
                "command": "pip install requests",
                "cwd": outside_cwd,
                "agent_id": "focused-ghc-ci-analyzer",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["decision"] == "denied"
        assert "escapes allowed root" in data["stderr"]

        pending_resp = client.post("/pending", json={})
        assert pending_resp.status_code == 200
        raw_cmds = {p["raw_cmd"] for p in pending_resp.json()["prompts"]}
        assert "pip install requests" not in raw_cmds
    finally:
        # Restore the shared "/" jail root the autouse fixture set up, so
        # later tests (and this fixture's own teardown) see consistent state.
        monkeypatch.setenv("SHELL_RUNNER_CWD_ROOT", "/")
        importlib.reload(shell_runner.executor)
