"""Durable runner rules are bound to the runner's manifest hashes (issue #98, item 4).

A template/verb rule granted for a runner (make, pixi run, npm run, ...) stores the
manifest hashes the approver saw (manifest_pins). It is honoured only while the current
manifest(s) match; otherwise the command falls through to a normal (pinned) prompt, and
approving that prompt merges the new hashes into the rule. Nothing real runs:
_dispatch_approved is spied.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shell_runner import executor, script_pin
from shell_runner import server as srv
from shell_runner.classifier import classify
from shell_runner.models import ExecuteResponse
from shell_runner.persistence import Persistence

AGENT = "focused-code-modifier"  # AUTO_CAPPED cap

RUNNERS = [
    pytest.param("make deploy_x", "Makefile", id="make"),
    pytest.param("pixi run test", "pixi.toml", id="pixi"),
    pytest.param("npm run build", "package.json", id="npm"),
]
VERB_RUNNERS = RUNNERS[1:]


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Persistence:
    fresh = Persistence(db_path=tmp_path / "test.sqlite3")
    monkeypatch.setattr(srv, "db", fresh)
    return fresh


@pytest.fixture(autouse=True)
def _jail_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor, "_CWD_ROOTS", [tmp_path.resolve()])


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


def _project(root: Path, name: str, manifest: str, text: str = "v1\n") -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / manifest).write_text(text)
    return directory


def _preview(client: TestClient, command: str, cwd: Path) -> str:
    resp = client.post("/classify", json={"command": command, "cwd": str(cwd), "agent_id": AGENT})
    assert resp.status_code == 200
    return resp.json()["decision_preview"]


def _execute(client: TestClient, command: str, cwd: Path) -> dict:
    body = {"command": command, "cwd": str(cwd), "agent_id": AGENT}
    resp = client.post("/execute", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _grant(client: TestClient, command: str, cwd: Path, decision: str) -> None:
    """Run `command`, expect a prompt, and approve it with a reusable `decision`."""
    first = _execute(client, command, cwd)
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


def _rows(db: Persistence, table: str) -> list[sqlite3.Row]:
    with sqlite3.connect(db.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608


def _edit(directory: Path, manifest: str) -> None:
    path = directory / manifest
    path.write_text(path.read_text() + "# edited\n")


# --- templates -------------------------------------------------------------------


@pytest.mark.parametrize(("command", "manifest"), RUNNERS)
def test_template_rule_follows_the_manifest(
    client: TestClient,
    db: Persistence,
    calls: list[Any],
    tmp_path: Path,
    command: str,
    manifest: str,
) -> None:
    proj = _project(tmp_path, "a", manifest)
    _grant(client, command, proj, "approve_template")
    stored = json.loads(_rows(db, "template_approvals")[0]["manifest_pins"])
    assert set(stored) == {str((proj / manifest).resolve())}

    assert _preview(client, command, proj) == "would_execute"
    assert _execute(client, command, proj)["decision"] == "executed"

    _edit(proj, manifest)
    assert _preview(client, command, proj) == "would_prompt"
    again = _execute(client, command, proj)
    assert again["decision"] == "prompt_required"
    assert "manifest" in again["prompt"]["why"]
    assert len(calls) == 1

    # approving the re-prompt updates the map; the rule auto-runs again
    _grant_prompt = client.post(
        "/approve_pending",
        json={
            "prompt_id": again["prompt"]["id"],
            "decision": "approve_template",
            "approver_agent_id": "primary",
        },
    )
    assert _grant_prompt.status_code == 200
    assert _preview(client, command, proj) == "would_execute"
    assert _execute(client, command, proj)["decision"] == "executed"
    assert len(_rows(db, "template_approvals")) == 1


@pytest.mark.parametrize(("command", "manifest"), RUNNERS)
def test_global_template_prompts_in_another_project_then_merges(
    client: TestClient,
    db: Persistence,
    calls: list[Any],
    tmp_path: Path,
    command: str,
    manifest: str,
) -> None:
    proj_a = _project(tmp_path, "a", manifest, "alpha\n")
    proj_b = _project(tmp_path, "b", manifest, "beta\n")
    _grant(client, command, proj_a, "approve_template_global")
    assert _preview(client, command, proj_a) == "would_execute"
    assert _preview(client, command, proj_b) == "would_prompt"

    _grant(client, command, proj_b, "approve_template_global")
    stored = json.loads(_rows(db, "template_approvals")[0]["manifest_pins"])
    assert set(stored) == {str((proj / manifest).resolve()) for proj in (proj_a, proj_b)}
    assert _preview(client, command, proj_a) == "would_execute"
    assert _preview(client, command, proj_b) == "would_execute"
    assert _execute(client, command, proj_a)["decision"] == "executed"
    assert _execute(client, command, proj_b)["decision"] == "executed"
    assert len(calls) == 2


def test_agent_row_is_used_when_the_global_row_is_not_honoured(
    client: TestClient, tmp_path: Path
) -> None:
    command, manifest = "pixi run test", "pixi.toml"
    proj_a = _project(tmp_path, "a", manifest, "alpha\n")
    proj_b = _project(tmp_path, "b", manifest, "beta\n")
    _grant(client, command, proj_a, "approve_template_global")
    _grant(client, command, proj_b, "approve_template")  # agent-scoped row for b
    assert _preview(client, command, proj_b) == "would_execute"
    assert _preview(client, command, proj_a) == "would_execute"


# --- verbs -----------------------------------------------------------------------


@pytest.mark.parametrize(("command", "manifest"), VERB_RUNNERS)
def test_verb_rule_follows_the_manifest(
    client: TestClient,
    db: Persistence,
    calls: list[Any],
    tmp_path: Path,
    command: str,
    manifest: str,
) -> None:
    proj = _project(tmp_path, "a", manifest)
    _grant(client, command, proj, "approve_verb")
    assert set(json.loads(_rows(db, "verb_approvals")[0]["manifest_pins"])) == {
        str((proj / manifest).resolve())
    }
    assert _preview(client, command, proj) == "would_execute"
    assert _execute(client, command, proj)["decision"] == "executed"

    _edit(proj, manifest)
    assert _preview(client, command, proj) == "would_prompt"
    again = _execute(client, command, proj)
    assert again["decision"] == "prompt_required"
    assert "manifest" in again["prompt"]["why"]

    _grant_prompt = client.post(
        "/approve_pending",
        json={
            "prompt_id": again["prompt"]["id"],
            "decision": "approve_verb",
            "approver_agent_id": "primary",
        },
    )
    assert _grant_prompt.status_code == 200
    assert _preview(client, command, proj) == "would_execute"
    assert len(_rows(db, "verb_approvals")) == 1


@pytest.mark.parametrize(("command", "manifest"), VERB_RUNNERS)
def test_verb_rule_prompts_in_a_nested_project_then_covers_both(
    client: TestClient, db: Persistence, tmp_path: Path, command: str, manifest: str
) -> None:
    outer = _project(tmp_path, "ws", manifest, "outer\n")
    inner = _project(outer, "sub", manifest, "inner\n")
    _grant(client, command, outer, "approve_verb")
    assert _preview(client, command, outer) == "would_execute"
    assert _preview(client, command, inner) == "would_prompt"  # inside the prefix, other manifest

    # the inner prompt's rule has prefix=inner, a different row from the outer one
    _grant(client, command, inner, "approve_verb")
    rows = _rows(db, "verb_approvals")
    assert len(rows) == 2
    assert {k for row in rows for k in json.loads(row["manifest_pins"])} == {
        str((outer / manifest).resolve()),
        str((inner / manifest).resolve()),
    }
    assert _preview(client, command, outer) == "would_execute"
    assert _preview(client, command, inner) == "would_execute"


def test_verb_max_tier_counts_only_honoured_rows(db: Persistence, tmp_path: Path) -> None:
    proj = _project(tmp_path, "a", "pixi.toml")
    path = str((proj / "pixi.toml").resolve())
    good = script_pin.manifest_pins("pixi run test", str(proj))
    assert good == {path: good[path]}  # type: ignore[index]
    with sqlite3.connect(db.db_path) as conn:
        for prefix, tier, pins in (
            (str(tmp_path), 3, {path: "0" * 64}),  # stale hash, most restrictive tier
            (str(proj), 2, good),
        ):
            conn.execute(
                "INSERT INTO verb_approvals (verb, cwd_prefix, agent_id, approved_tier, "
                "approved_at, manifest_pins) VALUES ('pixi', ?, NULL, ?, 'now', ?)",
                (prefix, tier, json.dumps(pins)),
            )
    assert db.get_verb_approved_tier("pixi", str(proj), AGENT, manifest_pins=good) == 2  # type: ignore[arg-type]
    assert (
        db.get_verb_approved_tier("pixi", str(proj), AGENT, manifest_pins={path: "f" * 64}) is None
    )


# --- legacy / non-runner / migration ---------------------------------------------


def test_legacy_runner_rule_without_pins_prompts(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    proj = _project(tmp_path, "a", "pixi.toml")
    template = classify("pixi run test", str(proj), AGENT).template
    db.create_template_approval(template=template, agent_id=None, approved_tier=2)
    db.create_verb_approval(verb="pixi", cwd_prefix=str(proj), agent_id=None, approved_tier=2)
    assert _preview(client, "pixi run test", proj) == "would_prompt"
    assert _preview(client, "pixi run other", proj) == "would_prompt"


def test_unpinnable_manifest_never_honours_a_runner_rule(
    client: TestClient, db: Persistence, calls: list[Any], tmp_path: Path
) -> None:
    proj = _project(tmp_path, "a", "Makefile")
    _grant(client, "make deploy_x", proj, "approve_template")
    assert _preview(client, "make deploy_x", proj) == "would_execute"

    (proj / "Makefile").unlink()
    os.mkfifo(proj / "Makefile")  # not a readable regular file: UNPINNABLE
    assert _preview(client, "make deploy_x", proj) == "would_prompt"
    assert _execute(client, "make deploy_x", proj)["decision"] == "prompt_required"
    assert len(calls) == 0


def test_legacy_rule_prompts_then_runs_after_template_approval(
    client: TestClient, db: Persistence, calls: list[Any], tmp_path: Path
) -> None:
    proj = _project(tmp_path, "a", "Makefile")
    template = classify("make deploy_x", str(proj), AGENT).template
    db.create_template_approval(template=template, agent_id=AGENT, approved_tier=2)
    assert _rows(db, "template_approvals")[0]["manifest_pins"] is None
    assert _preview(client, "make deploy_x", proj) == "would_prompt"

    _grant(client, "make deploy_x", proj, "approve_template")
    rows = _rows(db, "template_approvals")
    assert len(rows) == 1
    assert rows[0]["manifest_pins"] is not None
    result = _execute(client, "make deploy_x", proj)
    assert result["decision"] == "executed"
    assert len(calls) == 1


def test_merge_keeps_other_manifests_trusted(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    command, manifest = "make deploy_x", "Makefile"
    proj_a = _project(tmp_path, "a", manifest, "alpha\n")
    proj_b = _project(tmp_path, "b", manifest, "beta\n")
    _grant(client, command, proj_a, "approve_template_global")
    _grant(client, command, proj_b, "approve_template_global")
    path_b = str((proj_b / manifest).resolve())
    hash_b = json.loads(_rows(db, "template_approvals")[0]["manifest_pins"])[path_b]

    _edit(proj_a, manifest)
    assert _preview(client, command, proj_a) == "would_prompt"
    assert _preview(client, command, proj_b) == "would_execute"
    _grant(client, command, proj_a, "approve_template_global")

    rows = _rows(db, "template_approvals")
    assert len(rows) == 1
    assert json.loads(rows[0]["manifest_pins"])[path_b] == hash_b
    assert _preview(client, command, proj_a) == "would_execute"
    assert _preview(client, command, proj_b) == "would_execute"
    assert "replaced" in {r["cause"] for r in _rows(db, "rule_revocations")}


@pytest.mark.parametrize("decision", ["approve_template", "approve_verb"])
def test_runner_plus_script_is_downgraded_to_one_shot(
    client: TestClient, db: Persistence, tmp_path: Path, decision: str
) -> None:
    proj = _project(tmp_path, "a", "x.py", "print(1)\n")
    _grant(client, "uv run x.py", proj, decision)
    assert _rows(db, "template_approvals") == []
    assert _rows(db, "verb_approvals") == []
    assert _execute(client, "uv run x.py", proj)["decision"] == "executed"  # the one-shot token
    assert _execute(client, "uv run x.py", proj)["decision"] == "prompt_required"


def test_no_manifest_is_read_without_rule_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    proj = _project(tmp_path, "a", "Makefile")
    seen: list[str] = []
    real = script_pin.manifest_pins

    def _counting(command: str, cwd: str) -> Any:
        seen.append(command)
        return real(command, cwd)

    monkeypatch.setattr(script_pin, "manifest_pins", _counting)
    assert _preview(client, "make deploy_x", proj) == "would_prompt"
    assert seen == []


def test_manifest_is_read_once_when_template_and_verb_rows_exist(
    client: TestClient, db: Persistence, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    proj = _project(tmp_path, "a", "pixi.toml")
    db.create_template_approval(
        template=classify("pixi run test", str(proj), AGENT).template,
        agent_id=None,
        approved_tier=2,
    )
    db.create_verb_approval(verb="pixi", cwd_prefix=str(proj), agent_id=None, approved_tier=2)
    seen: list[str] = []
    real = script_pin.manifest_pins

    def _counting(command: str, cwd: str) -> Any:
        seen.append(command)
        return real(command, cwd)

    monkeypatch.setattr(script_pin, "manifest_pins", _counting)
    assert _preview(client, "pixi run test", proj) == "would_prompt"
    assert len(seen) == 1


def test_non_runner_template_is_unaffected(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    command = "curl https://example.com/data.json"
    cls = classify(command, str(tmp_path), AGENT)
    assert _preview(client, command, tmp_path) == "would_prompt"
    db.create_template_approval(template=cls.template, agent_id=None, approved_tier=2)
    assert _preview(client, command, tmp_path) == "would_execute"


def test_migration_adds_manifest_pins_to_an_existing_db(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE template_approvals (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "template TEXT NOT NULL, agent_id TEXT, approved_tier INTEGER NOT NULL, "
            "approved_at TEXT NOT NULL, approved_via_prompt_id TEXT)"
        )
        conn.execute(
            "CREATE TABLE verb_approvals (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "verb TEXT NOT NULL, cwd_prefix TEXT NOT NULL, agent_id TEXT, "
            "approved_tier INTEGER NOT NULL, approved_at TEXT NOT NULL, "
            "approved_via_prompt_id TEXT)"
        )
        conn.execute(
            "INSERT INTO template_approvals (template, agent_id, approved_tier, approved_at) "
            "VALUES ('pixi run test', NULL, 2, 'then')"
        )
    Persistence(db_path=path)
    with sqlite3.connect(path) as conn:
        for table in ("template_approvals", "verb_approvals"):
            columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}  # noqa: S608
            assert "manifest_pins" in columns
        assert conn.execute("SELECT manifest_pins FROM template_approvals").fetchone() == (None,)
