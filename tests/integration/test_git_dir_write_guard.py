"""Issue #84: git-dir write guard end to end (approvals cannot unlock it; audit flags it)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shell_runner import executor
from shell_runner.persistence import Persistence
from shell_runner.server import app

AGENT = "focused-code-modifier"  # AUTO_CAPPED cap
TEMPLATE = "echo x >> <file_arg>"
PLANT = "echo '[core] fsmonitor = touch /tmp/pwn' >> .git/config"


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Persistence:
    fresh = Persistence(db_path=tmp_path / "test.sqlite3")
    monkeypatch.setattr("shell_runner.server.db", fresh)
    monkeypatch.setattr(executor, "_CWD_ROOTS", [tmp_path.resolve()])
    monkeypatch.setenv("SHELL_RUNNER_CONFIG_DIR", str(tmp_path / "no-rules"))
    return fresh


@pytest.fixture
def db(_fresh_db: Persistence) -> Persistence:
    return _fresh_db


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = (tmp_path / "repo").resolve()
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text("[core]\n")
    return root


def _classify(client: TestClient, command: str, cwd: Path) -> dict:
    resp = client.post("/classify", json={"command": command, "cwd": str(cwd), "agent_id": AGENT})
    assert resp.status_code == 200
    return resp.json()


def _execute(client: TestClient, command: str, cwd: Path) -> dict:
    resp = client.post("/execute", json={"command": command, "cwd": str(cwd), "agent_id": AGENT})
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize("global_scope", [False, True])
def test_template_approval_does_not_unlock_git_dir_write(
    client: TestClient, db: Persistence, repo: Path, global_scope: bool
) -> None:
    db.create_template_approval(
        template=TEMPLATE, agent_id=None if global_scope else AGENT, approved_tier=2
    )
    assert _classify(client, "echo x >> notes.txt", repo)["decision_preview"] == "would_execute"
    got = _classify(client, "echo x >> .git/config", repo)
    assert got["decision_preview"] == "would_prompt"
    assert got["command_tier"] == 3
    assert got["category"] == "git_dir_write"


def test_verb_approval_does_not_unlock_git_dir_write(
    client: TestClient, db: Persistence, repo: Path
) -> None:
    db.create_verb_approval(verb="echo", cwd_prefix=str(repo), agent_id=AGENT, approved_tier=2)
    assert _classify(client, PLANT, repo)["decision_preview"] == "would_prompt"


def test_approve_template_of_guarded_prompt_cannot_lower_variant(
    client: TestClient, db: Persistence, repo: Path
) -> None:
    prompt_id = _execute(client, PLANT, repo)["prompt"]["id"]
    resp = client.post(
        "/approve_pending",
        json={
            "prompt_id": prompt_id,
            "decision": "approve_template",
            "approver_agent_id": "primary",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["template_promoted"] is False
    assert _execute(client, "echo y >> .git/hooks/pre-commit", repo)["decision"] == (
        "prompt_required"
    )
    assert _execute(client, "echo y >> notes.txt", repo)["decision"] == "executed"


def test_guarded_attempt_is_not_run_and_is_flagged_in_audit(
    client: TestClient, db: Persistence, repo: Path
) -> None:
    before = (repo / ".git" / "config").read_text()
    _execute(client, "cat .git/config", repo)
    _execute(client, "echo x >> notes.txt", repo)
    body = _execute(client, PLANT, repo)
    assert body["decision"] == "prompt_required"
    assert (repo / ".git" / "config").read_text() == before

    resp = client.post("/audit", json={"flagged_only": True})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["notes"] == []
    (row,) = data["rows"]
    assert row["raw_cmd"] == PLANT
    assert row["flagged"] == 1
    assert row["matched_rule_category"] == "git_dir_write"
    (match,) = json.loads(row["protected_path_match"])
    assert match["glob"] == "builtin:.git"
    assert match["kind"] == "write"
    assert match["path"] == str(repo / ".git" / "config")
