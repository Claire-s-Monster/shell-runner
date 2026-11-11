"""Inline-code T4 rules (`-c`, `source`) are never promotable.

Approving such a prompt with approve_template / approve_template_global /
approve_verb degrades to a one-shot approval, and persisted template / verb
approvals (e.g. rows created while the shape was still T3) are ignored for any
command containing a non-promotable segment, including compound and wrapped ones.
Other T4 rules (kill -9, chown, ...) keep their promotable behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shell_runner import executor
from shell_runner.catalog import T4_ALWAYS_APPROVE
from shell_runner.classifier import classify
from shell_runner.persistence import Persistence
from shell_runner.server import app

AGENT = "focused-code-modifier"  # AUTO_CAPPED cap


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


def _template(command: str, cwd: Path) -> str:
    return classify(command, str(cwd), AGENT).template


def _preview(client: TestClient, command: str, cwd: Path) -> str:
    resp = client.post("/classify", json={"command": command, "cwd": str(cwd), "agent_id": AGENT})
    assert resp.status_code == 200
    return resp.json()["decision_preview"]


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


def _seed(db: Persistence, command: str, verb: str, cwd: Path) -> None:
    db.create_template_approval(template=_template(command, cwd), agent_id=AGENT, approved_tier=2)
    db.create_template_approval(template=_template(command, cwd), agent_id=None, approved_tier=2)
    db.create_verb_approval(verb=verb, cwd_prefix=str(cwd), agent_id=AGENT, approved_tier=2)


def test_catalog_marks_exactly_the_inline_code_rules_non_promotable() -> None:
    flagged = [r.pattern for r in T4_ALWAYS_APPROVE if not r.promotable]
    # bash family, python, source, interpreter fed by <heredoc> / by <process_subst:..>
    assert len(flagged) == 5
    assert r"^source\s+\S+$" in flagged
    assert any(p.endswith(r"\s+<heredoc>(?:\s|$)") for p in flagged)
    assert any(p.startswith("^python") for p in flagged)
    assert any(p.startswith("^(?:bash|sh") for p in flagged)


# --- (a)/(b) approval path ---------------------------------------------------


@pytest.mark.parametrize(
    ("command", "decision", "verb"),
    [
        ("python3 -c 'x'", "approve_template", "python3"),
        ("python3 -c 'x'", "approve_template_global", "python3"),
        ("bash -lc 'x'", "approve_verb", "bash"),
        ("python3 -c 'x'", "approve_verb", "python3"),
        ("source ./env.sh", "approve_template", "source"),
    ],
)
def test_reusable_approval_downgrades_to_one_shot(
    client: TestClient, db: Persistence, tmp_path: Path, command: str, decision: str, verb: str
) -> None:
    first = _execute(client, command, tmp_path)
    assert first["decision"] == "prompt_required"
    approved = _approve(client, first["prompt"]["id"], decision)

    assert approved["approve_token"] is not None
    assert approved["template_promoted"] is False
    assert approved["verb_promoted"] is False
    assert approved["catalog_entry_id"] is None
    assert approved["promoted_tier"] is None
    assert "never promotable" in (approved["scope_warning"] or "")
    assert db.get_template_approved_tier(_template(command, tmp_path), AGENT) is None
    assert db.get_verb_approved_tier(verb, str(tmp_path), AGENT) is None

    if command.startswith("source"):
        return  # the sourced file does not exist; one-shot execution is covered below
    done = _execute(client, command, tmp_path, approve_token=approved["approve_token"])
    assert done["decision"] == "executed"
    assert _execute(client, command, tmp_path)["decision"] == "prompt_required"


def test_approve_once_and_deny_still_work(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    prompt_id = _execute(client, "python3 -c 'pass'", tmp_path)["prompt"]["id"]
    once = _approve(client, prompt_id, "approve_once")
    assert once["approve_token"] is not None
    assert (
        _execute(client, "python3 -c 'pass'", tmp_path, approve_token=once["approve_token"])[
            "decision"
        ]
        == "executed"
    )

    prompt_id = _execute(client, "python3 -c 'pass'", tmp_path)["prompt"]["id"]
    denied = _approve(client, prompt_id, "deny")
    assert denied["approve_token"] is None


# --- (c)/(e) stale rows ignored at classify/execute time -----------------------


@pytest.mark.parametrize(
    ("command", "verb"),
    [
        ("python3 -Ic 'x'", "python3"),
        ("python3 -c 'x'", "python3"),
        ("bash -lc 'x'", "bash"),
        ("source ./env.sh", "source"),
        # compound
        ("echo hi && python3 -c 'x'", "echo"),
        ("cd sub && python3 -c 'x'", "python3"),
        ("python3 -c 'x' | head -1", "python3"),
        # wrapped
        ("nohup bash -lc 'x'", "nohup"),
        ("env FOO=1 python3 -c 'x'", "env"),
        ("timeout 5 python3 -Ic 'x'", "timeout"),
    ],
)
def test_stale_approvals_ignored_for_inline_code(
    client: TestClient, db: Persistence, tmp_path: Path, command: str, verb: str
) -> None:
    (tmp_path / "sub").mkdir()
    _seed(db, command, verb, tmp_path)
    assert _preview(client, command, tmp_path) == "would_prompt"
    assert _execute(client, command, tmp_path)["decision"] == "prompt_required"


# --- (d)/(f) unchanged behaviour -----------------------------------------------


def test_promotable_t4_still_promotes(client: TestClient, db: Persistence, tmp_path: Path) -> None:
    assert _preview(client, "kill -9 12345", tmp_path) == "would_prompt"
    _seed(db, "kill -9 12345", "kill", tmp_path)
    assert _preview(client, "kill -9 12345", tmp_path) == "would_execute"


def test_promotable_t4_approve_template_still_creates_row(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    prompt_id = _execute(client, "kill -9 12345", tmp_path)["prompt"]["id"]
    approved = _approve(client, prompt_id, "approve_template")
    assert approved["template_promoted"] is True
    assert db.get_template_approved_tier(_template("kill -9 12345", tmp_path), AGENT) == 2


def test_unrelated_t3_template_still_promotes(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    command = "mytool_unknown --flag"
    assert _preview(client, command, tmp_path) == "would_prompt"
    db.create_template_approval(
        template=_template(command, tmp_path), agent_id=AGENT, approved_tier=2
    )
    assert _preview(client, command, tmp_path) == "would_execute"
