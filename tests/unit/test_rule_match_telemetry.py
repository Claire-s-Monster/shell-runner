"""shell_calls records which template/verb approval rule promoted a command (issue #104).

Nothing real runs: executor.execute is faked.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shell_runner import executor
from shell_runner import server as srv
from shell_runner.classifier import classify
from shell_runner.executor import ExecutionResult
from shell_runner.persistence import Persistence

AGENT = "focused-code-modifier"  # AUTO_CAPPED cap
COMMAND = "pip install requests"  # T3 without a rule
RULE_COLUMNS = (
    "approval_rule_kind",
    "approval_rule_id",
    "approval_rule_text",
    "approval_rule_outcome",
)


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Persistence:
    fresh = Persistence(db_path=tmp_path / "test.sqlite3")
    monkeypatch.setattr(srv, "db", fresh)
    monkeypatch.setattr(executor, "_CWD_ROOTS", [tmp_path.resolve()])
    return fresh


@pytest.fixture(autouse=True)
def fake_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(**_: Any) -> ExecutionResult:
        return ExecutionResult(0, "ok", "", None, None, 1, False)

    monkeypatch.setattr(srv, "execute", _fake)
    monkeypatch.setattr(srv, "inline_budget_s", lambda: 0)


@pytest.fixture
def client() -> TestClient:
    return TestClient(srv.app)


def _execute(client: TestClient, command: str, cwd: Path, **extra: Any) -> dict:
    body = {"command": command, "cwd": str(cwd), "agent_id": AGENT, **extra}
    resp = client.post("/execute", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _last_row(db: Persistence) -> dict:
    return db.telemetry_query(limit=1)[0]


def _rule(row: dict) -> tuple[Any, ...]:
    return tuple(row[c] for c in RULE_COLUMNS)


def _template() -> str:
    return classify(COMMAND, "/tmp", AGENT).template


def test_template_rule_is_recorded(client: TestClient, db: Persistence, tmp_path: Path) -> None:
    rule_id = db.create_template_approval(template=_template(), agent_id=AGENT, approved_tier=2)
    assert _execute(client, COMMAND, tmp_path)["decision"] == "executed"
    assert _rule(_last_row(db)) == ("template", rule_id, _template(), "honoured")


def test_global_template_rule_is_recorded(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    rule_id = db.create_template_approval(template=_template(), agent_id=None, approved_tier=2)
    assert _execute(client, COMMAND, tmp_path)["decision"] == "executed"
    assert _rule(_last_row(db)) == ("template_global", rule_id, _template(), "honoured")


def test_verb_rule_is_recorded_with_its_cwd_prefix(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    rule_id = db.create_verb_approval(
        verb="pip", cwd_prefix=str(tmp_path), agent_id=AGENT, approved_tier=2
    )
    assert _execute(client, COMMAND, tmp_path)["decision"] == "executed"
    assert _rule(_last_row(db)) == ("verb", rule_id, f"pip @ {tmp_path}", "honoured")


def test_verb_max_records_the_row_that_supplied_the_tier(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    db.create_verb_approval(verb="pip", cwd_prefix=str(tmp_path), agent_id=None, approved_tier=1)
    strict = db.create_verb_approval(
        verb="pip", cwd_prefix=str(sub), agent_id=AGENT, approved_tier=2
    )
    assert _execute(client, COMMAND, sub)["decision"] == "executed"
    assert _rule(_last_row(db)) == ("verb", strict, f"pip @ {sub}", "honoured")


def test_unpromoted_commands_have_no_rule(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    assert _execute(client, "ls", tmp_path)["decision"] == "executed"
    assert _rule(_last_row(db)) == (None, None, None, None)
    assert _execute(client, COMMAND, tmp_path)["decision"] == "prompt_required"
    assert _rule(_last_row(db)) == (None, None, None, None)


def test_rule_is_recorded_on_the_inline_budget_divert_path(
    client: TestClient, db: Persistence, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rule_id = db.create_template_approval(template=_template(), agent_id=AGENT, approved_tier=2)
    monkeypatch.setattr(srv, "inline_budget_s", lambda: 1)

    async def _run(**_: Any) -> tuple[ExecutionResult | None, str | None]:
        return ExecutionResult(0, "ok", "", None, None, 1, False), None

    monkeypatch.setattr(srv, "_run_with_inline_budget", _run)
    assert _execute(client, COMMAND, tmp_path, timeout_s=30)["decision"] == "executed"
    assert _rule(_last_row(db)) == ("template", rule_id, _template(), "honoured")


def test_manifest_changed_runner_rule_is_recorded_as_not_approved(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    makefile = proj / "Makefile"
    makefile.write_text("deploy_x:\n\t@true\n")
    first = _execute(client, "make deploy_x", proj)
    assert first["decision"] == "prompt_required"
    resp = client.post(
        "/approve_pending",
        json={
            "prompt_id": first["prompt"]["id"],
            "decision": "approve_template",
            "approver_agent_id": "primary",
        },
    )
    assert resp.status_code == 200, resp.text
    with sqlite3.connect(db.db_path) as conn:
        rule_id, template = conn.execute("SELECT id, template FROM template_approvals").fetchone()

    makefile.write_text(makefile.read_text() + "# edited\n")
    assert _execute(client, "make deploy_x", proj)["decision"] == "prompt_required"
    assert _rule(_last_row(db)) == ("template", rule_id, template, "manifest_not_approved")


def test_audit_rows_expose_the_columns_and_filter(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    rule_id = db.create_template_approval(template=_template(), agent_id=AGENT, approved_tier=2)
    _execute(client, COMMAND, tmp_path)
    _execute(client, "ls", tmp_path)

    rows = client.post("/audit", json={}).json()["rows"]
    assert len(rows) == 2
    assert {r["approval_rule_kind"] for r in rows} == {"template", None}

    only = client.post("/audit", json={"approval_rule_kind": "template"}).json()["rows"]
    assert [r["raw_cmd"] for r in only] == [COMMAND]
    assert only[0]["approval_rule_id"] == rule_id
    assert only[0]["approval_rule_text"] == _template()
    assert only[0]["approval_rule_outcome"] == "honoured"

    none = client.post("/audit", json={"approval_rule_kind": "verb"}).json()["rows"]
    assert none == []
    blocked = client.post("/audit", json={"approval_rule_outcome": "manifest_not_approved"})
    assert blocked.json()["rows"] == []


def test_migration_adds_the_columns_to_an_existing_database(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE shell_calls (id TEXT PRIMARY KEY, ts TEXT NOT NULL, "
            "agent_id TEXT NOT NULL, cwd TEXT NOT NULL, raw_cmd TEXT NOT NULL, "
            "normalized_template TEXT NOT NULL, command_tier INTEGER NOT NULL, "
            "final_tier INTEGER NOT NULL, decision TEXT NOT NULL, "
            "matched_rule_pattern TEXT, matched_rule_category TEXT, exit_code INTEGER, "
            "output_bytes_stdout INTEGER, output_bytes_stderr INTEGER, duration_ms INTEGER, "
            "decision_path_json TEXT, normalizer_warnings_json TEXT)"
        )
    conn.close()

    Persistence(db_path=path)
    with sqlite3.connect(path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(shell_calls)")}
    assert set(RULE_COLUMNS) <= cols
