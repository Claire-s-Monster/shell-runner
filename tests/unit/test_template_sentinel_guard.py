"""Issue #117 (step 1): a template with a leaked ``__TKSENTINEL_<n>`` is never a rule.

Template approvals match by exact string equality, so a leaked sentinel acts as a
wildcard. It must never be stored as a template approval (the approve_template prompt
degrades to approve_once) and a row that has one must never match.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shell_runner import executor
from shell_runner.classifier import classify
from shell_runner.persistence import Persistence
from shell_runner.server import _apply_template_approval, app
from shell_runner.shellprep import SENTINEL_PREFIX, template_has_sentinel

AGENT = "focused-code-modifier"
# `kill -9` prompts (T4, promotable); the substitution inside a larger word leaks a sentinel.
# (`export PATH=$(date)/bin:$PATH` leaks too, but the script-pin guard refuses its approval.)
LEAKY = "kill -9 pre$(date)suffix"
LEAKY_EVIL = "kill -9 pre$(rm -rf ~/x)suffix"


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


def _rows(db: Persistence) -> int:
    with sqlite3.connect(db.db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM template_approvals").fetchone()[0]


def test_helper() -> None:
    assert template_has_sentinel(f"export PATH={SENTINEL_PREFIX}0/bin")
    assert template_has_sentinel(f"{SENTINEL_PREFIX}3")
    assert not template_has_sentinel("export PATH=<var>/bin")
    assert not template_has_sentinel("")


def test_leaky_command_really_produces_sentinel_template(tmp_path: Path) -> None:
    assert template_has_sentinel(classify(LEAKY, str(tmp_path), AGENT).template)


def test_persistence_refuses_sentinel_template(db: Persistence) -> None:
    with pytest.raises(ValueError, match="#117"):
        db.create_template_approval(
            template=f"export PATH={SENTINEL_PREFIX}0/bin", agent_id=AGENT, approved_tier=2
        )
    assert _rows(db) == 0


def test_approve_template_on_sentinel_template_downgrades(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    first = _execute(client, LEAKY, tmp_path)
    assert first["decision"] == "prompt_required"
    approved = _approve(client, first["prompt"]["id"], "approve_template")

    assert approved["template_promoted"] is False
    assert approved["catalog_entry_id"] is None
    assert approved["approve_token"] is not None
    assert "#117" in (approved["scope_warning"] or "")
    assert _rows(db) == 0

    done = _execute(client, LEAKY, tmp_path, approve_token=approved["approve_token"])
    assert done["decision"] == "executed"
    assert _execute(client, LEAKY, tmp_path)["decision"] == "prompt_required"


def test_sentinel_row_inserted_via_raw_sql_is_not_matched(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    template = classify(LEAKY, str(tmp_path), AGENT).template
    assert template == classify(LEAKY_EVIL, str(tmp_path), AGENT).template
    with sqlite3.connect(db.db_path) as conn:
        conn.execute(
            "INSERT INTO template_approvals (template, agent_id, approved_tier, approved_at) "
            "VALUES (?, NULL, 2, CURRENT_TIMESTAMP)",
            (template,),
        )
    assert db.match_template_approval(template, AGENT).tier is None
    assert _execute(client, LEAKY_EVIL, tmp_path)["decision"] == "prompt_required"


def _verb_rows(db: Persistence) -> int:
    with sqlite3.connect(db.db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM verb_approvals").fetchone()[0]


def _insert_verb_rule(db: Persistence, cwd: Path) -> None:
    with sqlite3.connect(db.db_path) as conn:
        conn.execute(
            "INSERT INTO verb_approvals (verb, cwd_prefix, agent_id, approved_tier, approved_at) "
            "VALUES ('kill', ?, ?, 2, CURRENT_TIMESTAMP)",
            (str(cwd.resolve()), AGENT),
        )


def test_approve_verb_on_sentinel_template_downgrades(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    first = _execute(client, LEAKY, tmp_path)
    assert first["decision"] == "prompt_required"
    approved = _approve(client, first["prompt"]["id"], "approve_verb")

    assert approved["approve_token"] is not None
    assert "#117" in (approved["scope_warning"] or "")
    assert _verb_rows(db) == 0

    done = _execute(client, LEAKY, tmp_path, approve_token=approved["approve_token"])
    assert done["decision"] == "executed"


def test_verb_rule_does_not_promote_sentinel_template(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    _insert_verb_rule(db, tmp_path)
    assert _execute(client, LEAKY_EVIL, tmp_path)["decision"] == "prompt_required"


def test_verb_rule_still_promotes_normal_command(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    _insert_verb_rule(db, tmp_path)
    assert db.match_verb_approval("kill", str(tmp_path.resolve()), AGENT).tier == 2
    cls = classify("kill -9 12345", str(tmp_path), AGENT)
    assert not template_has_sentinel(cls.template)
    promoted = _apply_template_approval(cls, AGENT, str(tmp_path))
    assert any(p.startswith("approved_verb_T2") for p in promoted.decision_path)


def test_normal_template_still_approved_and_matched(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    command = "kill -9 12345"
    prompt_id = _execute(client, command, tmp_path)["prompt"]["id"]
    approved = _approve(client, prompt_id, "approve_template")
    assert approved["template_promoted"] is True
    assert _rows(db) == 1
    template = classify(command, str(tmp_path), AGENT).template
    assert not template_has_sentinel(template)
    assert db.match_template_approval(template, AGENT).tier == 2
