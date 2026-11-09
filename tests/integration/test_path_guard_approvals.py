"""Issue #75: persisted approvals must not lower a tier raised by path_guard.

A template/verb approval is keyed on a template (`echo x > <file_arg>`) shared by
in-cwd and out-of-cwd redirects, so it must never unlock the guarded variants. A
one-shot approve_token for the exact prompt still works.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shell_runner import executor
from shell_runner.persistence import Persistence
from shell_runner.server import app

AGENT = "focused-code-modifier"  # AUTO_CAPPED cap
TEMPLATE = "echo x > <file_arg>"


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Persistence:
    fresh = Persistence(db_path=tmp_path / "test.sqlite3")
    monkeypatch.setattr("shell_runner.server.db", fresh)
    return fresh


@pytest.fixture(autouse=True)
def _jail_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor, "_CWD_ROOTS", [tmp_path.resolve()])


@pytest.fixture
def db(_fresh_db: Persistence) -> Persistence:
    return _fresh_db


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def outside() -> Iterator[Path]:
    """A harmless target outside cwd, /tmp and the roots (removed afterwards)."""
    p = Path("/var/tmp") / f"sr75_{uuid.uuid4().hex}"
    yield p
    p.unlink(missing_ok=True)


def _classify(client: TestClient, command: str, cwd: Path) -> dict:
    resp = client.post("/classify", json={"command": command, "cwd": str(cwd), "agent_id": AGENT})
    assert resp.status_code == 200
    return resp.json()


def _execute(client: TestClient, command: str, cwd: Path, **extra: str) -> dict:
    body = {"command": command, "cwd": str(cwd), "agent_id": AGENT, **extra}
    resp = client.post("/execute", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _approve(client: TestClient, prompt_id: str, decision: str) -> dict:
    resp = client.post(
        "/approve_pending",
        json={"prompt_id": prompt_id, "decision": decision, "approver_agent_id": "primary"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize("global_scope", [False, True])
def test_persisted_template_approval_does_not_unlock_guarded(
    client: TestClient, db: Persistence, tmp_path: Path, global_scope: bool
) -> None:
    db.create_template_approval(
        template=TEMPLATE, agent_id=None if global_scope else AGENT, approved_tier=2
    )
    assert _classify(client, "echo x > notes.txt", tmp_path)["decision_preview"] == "would_execute"
    for cmd in ("echo x >> ~/.bashrc", "echo x > /var/tmp/sr75_probe"):
        got = _classify(client, cmd, tmp_path)
        assert got["decision_preview"] == "would_prompt", cmd
        assert got["command_tier"] == 3


def test_persisted_verb_approval_does_not_unlock_guarded(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    db.create_verb_approval(verb="echo", cwd_prefix=str(tmp_path), agent_id=AGENT, approved_tier=2)
    got = _classify(client, "echo x > /var/tmp/sr75_probe", tmp_path)
    assert got["decision_preview"] == "would_prompt"
    assert got["category"] == "redirect_outside_cwd"


def test_persisted_approval_does_not_unlock_recursive_grep(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    db.create_verb_approval(verb="grep", cwd_prefix=str(tmp_path), agent_id=AGENT, approved_tier=2)
    got = _classify(client, "grep -r password ~", tmp_path)
    assert got["decision_preview"] == "would_prompt"


def test_one_shot_token_executes_guarded_command_exactly_once(
    client: TestClient, tmp_path: Path, outside: Path
) -> None:
    cmd = f"echo x > {outside}"
    first = _execute(client, cmd, tmp_path)
    assert first["decision"] == "prompt_required"
    approved = _approve(client, first["prompt"]["id"], "approve_once")
    assert approved["template_promoted"] is False

    done = _execute(client, cmd, tmp_path, approve_token=approved["approve_token"])
    assert done["decision"] == "executed"
    assert outside.read_text() == "x\n"

    reuse = client.post(
        "/execute",
        json={
            "command": cmd,
            "cwd": str(tmp_path),
            "agent_id": AGENT,
            "approve_token": approved["approve_token"],
        },
    )
    assert reuse.status_code == 403
    assert _execute(client, cmd, tmp_path)["decision"] == "prompt_required"


@pytest.mark.parametrize(
    "decision", ["approve_template", "approve_template_global", "approve_verb"]
)
def test_reusable_approval_of_guarded_prompt_degrades_to_once(
    client: TestClient, db: Persistence, tmp_path: Path, outside: Path, decision: str
) -> None:
    cmd = f"echo x > {outside}"
    prompt_id = _execute(client, cmd, tmp_path)["prompt"]["id"]
    approved = _approve(client, prompt_id, decision)

    assert approved["approve_token"] is not None  # the one-shot still works
    assert approved["template_promoted"] is False
    assert approved["verb_promoted"] is False
    assert approved["catalog_entry_id"] is None
    assert "one-shot" in (approved["scope_warning"] or "")
    assert db.get_template_approved_tier(TEMPLATE, AGENT) is None
    assert db.get_verb_approved_tier("echo", str(tmp_path), AGENT) is None
    # neither the guarded command nor its in-cwd twin gained a standing approval
    assert _classify(client, cmd, tmp_path)["decision_preview"] == "would_prompt"
    assert _classify(client, "echo x > notes.txt", tmp_path)["decision_preview"] == "would_execute"


def test_deny_stays_denied_despite_approvals(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    db.create_template_approval(template=TEMPLATE, agent_id=None, approved_tier=2)
    db.create_verb_approval(verb="echo", cwd_prefix=str(tmp_path), agent_id=AGENT, approved_tier=2)
    got = _classify(client, "echo x > ~/.ssh/authorized_keys", tmp_path)
    assert got["decision_preview"] == "would_deny"
    assert got["category"] == "redirect_credential"
    assert _execute(client, "echo x > ~/.ssh/authorized_keys", tmp_path)["decision"] == "denied"
