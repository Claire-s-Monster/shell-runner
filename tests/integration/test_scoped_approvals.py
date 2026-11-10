"""Issue #85: approval-proofing is scoped by risk.

Ordinary T3 writes (`git stash push`, `git remote add`, `git branch -d`) are approvable like
any T3 command; the moment a hazard option (`--git-dir`, `-c`, ...) is present the command
is approval-proof again.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shell_runner import executor
from shell_runner.persistence import Persistence
from shell_runner.server import app

AGENT = "focused-code-modifier"  # AUTO_CAPPED cap


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Persistence:
    fresh = Persistence(db_path=tmp_path / "test.sqlite3")
    monkeypatch.setattr("shell_runner.server.db", fresh)
    monkeypatch.setattr(executor, "_CWD_ROOTS", [tmp_path.resolve()])
    monkeypatch.setenv("SHELL_RUNNER_CONFIG_DIR", str(tmp_path / "no-rules"))
    return fresh


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _classify(client: TestClient, command: str, cwd: Path) -> dict:
    resp = client.post("/classify", json={"command": command, "cwd": str(cwd), "agent_id": AGENT})
    assert resp.status_code == 200
    return resp.json()


def _approve(client: TestClient, command: str, cwd: Path, decision: str) -> dict:
    first = client.post(
        "/execute", json={"command": command, "cwd": str(cwd), "agent_id": AGENT}
    ).json()
    assert first["decision"] == "prompt_required", first
    resp = client.post(
        "/approve_pending",
        json={
            "prompt_id": first["prompt"]["id"],
            "decision": decision,
            "approver_agent_id": "primary",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_approve_template_on_git_stash_push_unlocks_variants_without_hazards(
    client: TestClient, tmp_path: Path
) -> None:
    approved = _approve(client, "git stash push", tmp_path, "approve_template")
    assert approved["template_promoted"] is True
    assert "one-shot" not in (approved["scope_warning"] or "")

    assert _classify(client, "git stash push", tmp_path)["decision_preview"] == "would_execute"
    for hazard in ("git stash push --git-dir=x", "git -c k=v stash push"):
        got = _classify(client, hazard, tmp_path)
        assert got["decision_preview"] == "would_prompt", hazard
        assert got["command_tier"] == 3


def test_approve_verb_on_git_remote_add_never_unlocks_hazards(
    client: TestClient, tmp_path: Path
) -> None:
    approved = _approve(client, "git remote add x url", tmp_path, "approve_verb")
    assert approved["verb_promoted"] is True
    assert _classify(client, "git remote add y url", tmp_path)["decision_preview"] == (
        "would_execute"
    )
    for hazard in (
        "git fetch --upload-pack=x origin",
        "git -c core.pager=x log",
        "git remote add --git-dir=x a b",
        "git stash push --git-dir=x",
    ):
        got = _classify(client, hazard, tmp_path)
        assert got["decision_preview"] == "would_prompt", hazard


def test_approve_template_on_git_stash_push_is_one_shot_when_hazard_present(
    client: TestClient, tmp_path: Path
) -> None:
    approved = _approve(client, "git stash push --git-dir=x", tmp_path, "approve_template")
    assert approved["template_promoted"] is False
    assert "one-shot" in (approved["scope_warning"] or "")
    assert _classify(client, "git stash push", tmp_path)["decision_preview"] == "would_prompt"


@pytest.mark.parametrize(
    "command",
    [
        "awk 'BEGIN{system(\"x\")}'",
        "sed 's/a/b/e' f",
        "sed 'w out' f",
        "echo x > $OUT",
    ],
)
def test_guarded_shapes_are_still_one_shot_only(
    client: TestClient, tmp_path: Path, command: str
) -> None:
    approved = _approve(client, command, tmp_path, "approve_template")
    assert approved["template_promoted"] is False
    assert _classify(client, command, tmp_path)["decision_preview"] == "would_prompt"
