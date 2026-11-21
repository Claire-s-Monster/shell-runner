"""Issue #98 item 2, end to end: include-dir flags are approve-once, preload files are pinned.

Nothing real runs: _dispatch_approved is spied.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shell_runner import executor, script_pin
from shell_runner import server as srv
from shell_runner.models import ExecuteResponse
from shell_runner.persistence import Persistence

AGENT = "focused-code-modifier"  # AUTO_CAPPED cap
REUSABLE = ["approve_template", "approve_template_global", "approve_verb"]
PERL_CMD = "perl -Ilib x.pl"
NODE_CMD = "node -r ./hook.js app.js"


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Persistence:
    fresh = Persistence(db_path=tmp_path / "test.sqlite3")
    monkeypatch.setattr(srv, "db", fresh)
    return fresh


@pytest.fixture(autouse=True)
def _jail_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor, "_CWD_ROOTS", [tmp_path.resolve()])


@pytest.fixture(autouse=True)
def _files(tmp_path: Path) -> None:
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "m.pm").write_text("1;\n")
    (tmp_path / "x.pl").write_text("print 1;\n")
    (tmp_path / "hook.js").write_text("// hook\n")
    (tmp_path / "app.js").write_text("console.log(1)\n")


@pytest.fixture
def db(_fresh_db: Persistence) -> Persistence:
    return _fresh_db


@pytest.fixture
def client() -> TestClient:
    return TestClient(srv.app)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    recorded: list[Any] = []

    async def _spy(req: Any, ctx: Any, *args: Any, **kwargs: Any) -> ExecuteResponse:
        recorded.append(req)
        return ExecuteResponse(
            decision="executed",
            tier=ctx.final_tier,
            matched_rule=None,
            exit_code=0,
            stdout="",
            stderr="",
            stdout_full_path=None,
            stderr_full_path=None,
            duration_ms=0,
            telemetry_id="spy",
        )

    monkeypatch.setattr(srv, "_dispatch_approved", _spy)
    return recorded


def _execute(client: TestClient, command: str, cwd: Path, **extra: str) -> dict:
    body = {"command": command, "cwd": str(cwd), "agent_id": AGENT, **extra}
    resp = client.post("/execute", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _approve_raw(client: TestClient, prompt_id: str, decision: str) -> Any:
    return client.post(
        "/approve_pending",
        json={"prompt_id": prompt_id, "decision": decision, "approver_agent_id": "primary"},
    )


def _count(db: Persistence, table: str) -> int:
    with sqlite3.connect(db.db_path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608


@pytest.mark.parametrize("decision", ["approve_once", *REUSABLE])
def test_include_dir_flag_is_approve_once_only(
    client: TestClient,
    db: Persistence,
    calls: list[Any],
    tmp_path: Path,
    decision: str,
) -> None:
    prompt = _execute(client, PERL_CMD, tmp_path)["prompt"]
    row = db.get_pending_prompt(prompt["id"])
    assert row is not None
    # non-durable, not unpinnable: the prompt carries a real pin
    assert row["script_sha256"] != script_pin.UNPINNABLE

    resp = _approve_raw(client, prompt["id"], decision)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["template_promoted"] is False
    assert body["verb_promoted"] is False
    if decision != "approve_once":
        note = json.dumps(body)
        assert "downgraded to a one-shot approval" in note, body
        assert "non_durable" in note, body
    assert _count(db, "template_approvals") == 0
    assert _count(db, "verb_approvals") == 0
    assert calls == []

    # the returned token runs the command exactly once
    run = _execute(client, PERL_CMD, tmp_path, approve_token=body["approve_token"])
    assert run["decision"] == "executed"
    assert len(calls) == 1
    assert _count(db, "template_approvals") == 0
    assert _count(db, "verb_approvals") == 0


def test_preload_file_change_after_approval_reprompts(
    client: TestClient, calls: list[Any], tmp_path: Path
) -> None:
    first = _execute(client, NODE_CMD, tmp_path)
    assert first["decision"] == "prompt_required", first
    assert _approve_raw(client, first["prompt"]["id"], "approve_once").status_code == 200

    (tmp_path / "hook.js").write_text("// hook\nrequire('child_process')\n")

    again = _execute(client, NODE_CMD, tmp_path)
    assert again["decision"] == "prompt_required", again
    assert again["prompt"]["id"] != first["prompt"]["id"]
    assert "changed" in again["prompt"]["why"]
    assert calls == []
