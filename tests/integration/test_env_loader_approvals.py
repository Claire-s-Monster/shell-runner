"""Issue #98 item 1, end to end: code-loading env vars make a command non-durable.

`PYTHONPATH=. cmd` is still approvable once (bound to the script content) but can never
become, or be matched by, a reusable rule. Nothing real runs: _dispatch_approved is spied.
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
SCRIPT_CMD = "bash ./x.sh"
RUNNER_CMD = "make deploy_x"  # promotable via a template rule, unlike a bare script


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
    (tmp_path / "x.sh").write_text("echo hi\n")
    (tmp_path / "Makefile").write_text("deploy_x:\n\t@echo hi\n")


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


def _execute(client: TestClient, command: str, cwd: Path) -> dict:
    body = {"command": command, "cwd": str(cwd), "agent_id": AGENT}
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


def _grant_runner_rule(client: TestClient, cwd: Path) -> None:
    first = _execute(client, RUNNER_CMD, cwd)
    assert first["decision"] == "prompt_required", first
    resp = _approve_raw(client, first["prompt"]["id"], "approve_template")
    assert resp.status_code == 200, resp.text
    assert resp.json()["template_promoted"] is True
    assert _execute(client, RUNNER_CMD, cwd)["decision"] == "executed"


# --- 1. never auto-runs --------------------------------------------------------


def test_env_loader_prefix_prompts_despite_a_rule(
    client: TestClient, calls: list[Any], tmp_path: Path
) -> None:
    _grant_runner_rule(client, tmp_path)
    calls.clear()
    data = _execute(client, f"PYTHONPATH=. {RUNNER_CMD}", tmp_path)
    assert data["decision"] == "prompt_required"
    assert calls == []


def test_env_loader_prefix_on_script_prompts(
    client: TestClient, calls: list[Any], tmp_path: Path
) -> None:
    assert _execute(client, f"PYTHONPATH=. {SCRIPT_CMD}", tmp_path)["decision"] == "prompt_required"
    assert calls == []


# --- 2. approve_once works, bound to the script --------------------------------


@pytest.mark.parametrize("base", [SCRIPT_CMD, RUNNER_CMD])
def test_approve_once_then_retry_executes(
    client: TestClient, db: Persistence, calls: list[Any], tmp_path: Path, base: str
) -> None:
    command = f"PYTHONPATH=. {base}"
    prompt = _execute(client, command, tmp_path)["prompt"]
    row = db.get_pending_prompt(prompt["id"])
    assert row is not None
    assert row["script_sha256"] != script_pin.UNPINNABLE
    resp = _approve_raw(client, prompt["id"], "approve_once")
    assert resp.status_code == 200, resp.text
    assert _execute(client, command, tmp_path)["decision"] == "executed"
    assert len(calls) == 1


def test_approve_once_is_bound_to_script_content(
    client: TestClient, calls: list[Any], tmp_path: Path
) -> None:
    command = f"PYTHONPATH=. {SCRIPT_CMD}"
    prompt = _execute(client, command, tmp_path)["prompt"]
    assert _approve_raw(client, prompt["id"], "approve_once").status_code == 200
    (tmp_path / "x.sh").write_text("echo rewritten\n")
    assert _execute(client, command, tmp_path)["decision"] == "prompt_required"
    assert calls == []


# --- 3. reusable decisions are refused / never persisted -----------------------


@pytest.mark.parametrize("base", [SCRIPT_CMD, RUNNER_CMD])
@pytest.mark.parametrize("decision", REUSABLE)
def test_reusable_decisions_create_no_rule(
    client: TestClient,
    db: Persistence,
    calls: list[Any],
    tmp_path: Path,
    decision: str,
    base: str,
) -> None:
    command = f"PYTHONPATH=. {base}"
    prompt = _execute(client, command, tmp_path)["prompt"]
    row = db.get_pending_prompt(prompt["id"])
    assert row is not None
    # non-durable, not unpinnable: the prompt carries a real pin
    assert row["script_sha256"] != script_pin.UNPINNABLE

    resp = _approve_raw(client, prompt["id"], decision)
    # S11 (Sourcery comment #11): degraded to one-shot, never refused with a 4xx
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["template_promoted"] is False
    assert body["verb_promoted"] is False
    note = json.dumps(body)
    assert "downgraded to a one-shot approval" in note, body
    assert "non_durable" in note, body
    assert _count(db, "template_approvals") == 0
    assert _count(db, "verb_approvals") == 0
    assert calls == []
    # the returned token runs the command exactly once
    token = body["approve_token"]
    body_req = {"command": command, "cwd": str(tmp_path), "agent_id": AGENT, "approve_token": token}
    run = client.post("/execute", json=body_req)
    assert run.status_code == 200, run.text
    assert run.json()["decision"] == "executed"
    assert len(calls) == 1
    assert _count(db, "template_approvals") == 0
    assert _count(db, "verb_approvals") == 0


# S1 (Sourcery comment #1): a non-durable command prompts even when the catalog auto-runs it
def test_s1_non_durable_auto_command_prompts(
    client: TestClient, calls: list[Any], tmp_path: Path
) -> None:
    assert _execute(client, "env PYTHONPATH=. pwd", tmp_path)["decision"] == "prompt_required"
    assert calls == []


# S1 guard
def test_s1_harmless_assignment_auto_command_runs(
    client: TestClient, calls: list[Any], tmp_path: Path
) -> None:
    assert _execute(client, "pwd", tmp_path)["decision"] == "executed"
    assert len(calls) == 1


# --- 4. an existing rule does not cover the env-prefixed form -------------------


def test_existing_rule_does_not_cover_env_prefixed_form(
    client: TestClient, db: Persistence, calls: list[Any], tmp_path: Path
) -> None:
    _grant_runner_rule(client, tmp_path)
    calls.clear()
    for command in (
        f"PYTHONPATH=. {RUNNER_CMD}",
        f"env LD_PRELOAD=/tmp/a.so {RUNNER_CMD}",
        f"export BASH_ENV=/tmp/e; {RUNNER_CMD}",
    ):
        assert _execute(client, command, tmp_path)["decision"] == "prompt_required", command
    assert calls == []
    assert _execute(client, RUNNER_CMD, tmp_path)["decision"] == "executed"


# --- 4b. verb / global-template rules must not cover the env-prefixed form ------

PIXI_CMD = "pixi run test"  # a runner whose verb rule is allowed (make is not verb-eligible)


def _grant_via_api(client: TestClient, cwd: Path, command: str, decision: str) -> None:
    first = _execute(client, command, cwd)
    assert first["decision"] == "prompt_required", first
    resp = _approve_raw(client, first["prompt"]["id"], decision)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["template_promoted"] or body["verb_promoted"], body


@pytest.mark.parametrize("decision", ["approve_verb", "approve_template_global"])
def test_verb_or_global_rule_does_not_cover_env_prefixed_form(
    client: TestClient, calls: list[Any], tmp_path: Path, decision: str
) -> None:
    (tmp_path / "pixi.toml").write_text("v1\n")
    _grant_via_api(client, tmp_path, PIXI_CMD, decision)
    assert _execute(client, PIXI_CMD, tmp_path)["decision"] == "executed"
    calls.clear()
    for command in (
        f"PYTHONPATH=. {PIXI_CMD}",
        f"env LD_PRELOAD=/tmp/a.so {PIXI_CMD}",
        f"export BASH_ENV=/tmp/e; {PIXI_CMD}",
    ):
        assert _execute(client, command, tmp_path)["decision"] == "prompt_required", command
    assert calls == []


@pytest.mark.parametrize("decision", ["approve_verb", "approve_template_global"])
def test_verb_or_global_rule_guard_harmless_form_still_runs(
    client: TestClient, calls: list[Any], tmp_path: Path, decision: str
) -> None:
    (tmp_path / "pixi.toml").write_text("v1\n")
    _grant_via_api(client, tmp_path, PIXI_CMD, decision)
    calls.clear()
    expected = "executed" if decision == "approve_verb" else "prompt_required"
    # a verb rule covers the verb; a global template rule covers only the exact template
    assert _execute(client, f"FOO=1 {PIXI_CMD}", tmp_path)["decision"] == expected
    assert _execute(client, PIXI_CMD, tmp_path)["decision"] == "executed"


# --- 5. guard: harmless assignments still use the rule --------------------------


def test_harmless_assignment_still_auto_runs_with_rule(
    client: TestClient, calls: list[Any], tmp_path: Path
) -> None:
    # The rule template keeps the assignment, so grant it on the harmless form itself.
    command = f"FOO=1 {RUNNER_CMD}"
    first = _execute(client, command, tmp_path)
    assert first["decision"] == "prompt_required", first
    resp = _approve_raw(client, first["prompt"]["id"], "approve_template")
    assert resp.status_code == 200, resp.text
    assert resp.json()["template_promoted"] is True
    calls.clear()
    assert _execute(client, command, tmp_path)["decision"] == "executed"
    assert len(calls) == 1
