"""Denying a prompt revokes the reusable rules it created (issue #94)."""

from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

import pytest
from fastapi.testclient import TestClient

from shell_runner import executor
from shell_runner import server as srv
from shell_runner.classifier import classify
from shell_runner.models import ExecuteResponse
from shell_runner.persistence import Persistence, PromotionRule

AGENT = "focused-code-modifier"
CMD_A = "kill -9 12345"  # promotable T4
CMD_B = "kill -9 54321"  # same normalized template as CMD_A
T3_CMD = "mytool_unknown --flag"


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Persistence:
    fresh = Persistence(db_path=tmp_path / "test.sqlite3")
    monkeypatch.setattr(srv, "db", fresh)
    monkeypatch.setattr(executor, "_CWD_ROOTS", [tmp_path.resolve()])
    monkeypatch.setenv("SHELL_RUNNER_JOB_DIR", str(tmp_path / "jobs"))
    return fresh


@pytest.fixture
def client(db: Persistence) -> TestClient:
    return TestClient(srv.app)


@pytest.fixture
def calls(db: Persistence, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
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


def _post(client: TestClient, cmd: str, cwd: Path) -> Any:
    return client.post("/execute", json={"command": cmd, "cwd": str(cwd), "agent_id": AGENT})


def _prompt(client: TestClient, cmd: str, cwd: Path) -> dict:
    resp = _post(client, cmd, cwd)
    assert resp.status_code == 200, resp.text
    assert resp.json()["decision"] == "prompt_required"
    return resp.json()["prompt"]


def _decide(client: TestClient, prompt_id: str, decision: str) -> dict:
    resp = client.post(
        "/approve_pending",
        json={"prompt_id": prompt_id, "decision": decision, "approver_agent_id": "primary"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _rule_rows(db: Persistence, prompt_id: str) -> int:
    with db._conn() as conn:
        n = 0
        for table in ("template_approvals", "verb_approvals"):
            n += conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE approved_via_prompt_id = ?",  # noqa: S608
                (prompt_id,),
            ).fetchone()[0]
        return n


@pytest.mark.parametrize(
    "decision", ["approve_template", "approve_template_global", "approve_verb"]
)
def test_deny_revokes_reusable_rule(
    client: TestClient, calls: list[Any], db: Persistence, tmp_path: Path, decision: str
) -> None:
    prompt = _prompt(client, CMD_A, tmp_path)
    _decide(client, prompt["id"], decision)
    assert _rule_rows(db, prompt["id"]) == 1
    assert _post(client, CMD_A, tmp_path).json()["decision"] == "executed"  # rule works

    _decide(client, prompt["id"], "deny")

    assert _rule_rows(db, prompt["id"]) == 0
    calls.clear()
    assert _post(client, CMD_A, tmp_path).json()["decision"] == "prompt_required"
    assert calls == []


def test_deny_leaves_rule_relinked_to_later_prompt(
    client: TestClient, calls: list[Any], db: Persistence, tmp_path: Path
) -> None:
    assert classify(CMD_A, str(tmp_path), AGENT).template == (
        classify(CMD_B, str(tmp_path), AGENT).template
    )
    prompt_a = _prompt(client, CMD_A, tmp_path)
    prompt_b = _prompt(client, CMD_B, tmp_path)
    _decide(client, prompt_a["id"], "approve_template")
    _decide(client, prompt_b["id"], "approve_template")
    # the replace-semantics row now links to B, not A
    assert _rule_rows(db, prompt_a["id"]) == 0
    assert _rule_rows(db, prompt_b["id"]) == 1

    _decide(client, prompt_a["id"], "deny")

    assert _rule_rows(db, prompt_b["id"]) == 1
    assert _post(client, CMD_B, tmp_path).json()["decision"] == "executed"


def test_deny_of_prompt_without_rule_leaves_other_rules(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    db.create_template_approval(template="seed tmpl", agent_id=None, approved_tier=2)
    db.create_verb_approval(
        verb="seedverb", cwd_prefix=str(tmp_path), agent_id=None, approved_tier=2
    )
    prompt = _prompt(client, T3_CMD, tmp_path)

    _decide(client, prompt["id"], "deny")

    with db._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM template_approvals").fetchone()[0] >= 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM template_approvals WHERE template = 'seed tmpl'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM verb_approvals WHERE verb = 'seedverb'").fetchone()[
                0
            ]
            == 1
        )


def test_approve_once_after_approve_template_removes_rule(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    prompt = _prompt(client, CMD_A, tmp_path)
    _decide(client, prompt["id"], "approve_template")
    assert _rule_rows(db, prompt["id"]) == 1

    _decide(client, prompt["id"], "approve_once")

    assert _rule_rows(db, prompt["id"]) == 0


def test_reapprove_after_deny_leaves_exactly_one_rule(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    prompt = _prompt(client, CMD_A, tmp_path)
    _decide(client, prompt["id"], "approve_template")
    _decide(client, prompt["id"], "deny")
    assert _rule_rows(db, prompt["id"]) == 0

    _decide(client, prompt["id"], "approve_template")

    assert _rule_rows(db, prompt["id"]) == 1


@pytest.mark.parametrize(
    ("decision", "kind", "agent_id"),
    [
        ("approve_once", "template", AGENT),
        ("deny", "template", AGENT),
        ("approve_template", "verb", AGENT),
        ("approve_verb", "template", AGENT),
        ("approve_template", "template", None),  # global rule, non-global decision
        ("approve_template_global", "template", AGENT),  # scoped rule, global decision
    ],
)
def test_decide_prompt_rejects_mismatched_rule(
    client: TestClient,
    db: Persistence,
    tmp_path: Path,
    decision: str,
    kind: Literal["template", "verb"],
    agent_id: str | None,
) -> None:
    prompt = _prompt(client, CMD_A, tmp_path)
    rule = PromotionRule(
        kind=kind, key="x", agent_id=agent_id, approved_tier=2, cwd_prefix=str(tmp_path)
    )

    with pytest.raises(ValueError, match="rule"):
        db.decide_prompt(prompt_id=prompt["id"], decision=decision, rule=rule)

    row = db.get_pending_prompt(prompt["id"])
    assert row is not None
    assert row["approve_decision"] is None
    assert _rule_rows(db, prompt["id"]) == 0


def test_deny_of_purged_prompt_removes_orphaned_rules(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    prompt = _prompt(client, CMD_A, tmp_path)
    _decide(client, prompt["id"], "approve_template")
    assert _rule_rows(db, prompt["id"]) == 1
    with db._write_conn() as conn:
        conn.execute("DELETE FROM pending_prompts WHERE id = ?", (prompt["id"],))

    result = db.decide_prompt(prompt_id=prompt["id"], decision="deny")

    assert result.token is None
    assert result.revoked_rules == 1
    assert _rule_rows(db, prompt["id"]) == 0


def test_invalid_promote_to_tier_leaves_prompt_undecided(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    prompt = _prompt(client, CMD_A, tmp_path)

    resp = client.post(
        "/approve_pending",
        json={
            "prompt_id": prompt["id"],
            "decision": "approve_template",
            "approver_agent_id": "primary",
            "promote_to_tier": 4,  # not more permissive than the T4 command
        },
    )

    assert resp.status_code == 400
    row = db.get_pending_prompt(prompt["id"])
    assert row is not None
    assert row["approve_decision"] is None
    assert _rule_rows(db, prompt["id"]) == 0


def test_deny_racing_approve_template_leaves_no_rule(
    client: TestClient,
    calls: list[Any],
    db: Persistence,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deny landing right after the first write transaction of an approve
    (i.e. between "decision recorded" and "rule created" in a non-atomic
    handler) must not leave a rule behind. The hook fires on the exit of the
    first write transaction of the approve request, so it exercises the race
    window whether the handler uses one transaction or two.

    After #94 the approve is one transaction, so this verifies the final state
    (deny wins, no rule), not a true interleaving of the two writes."""
    prompt = _prompt(client, CMD_A, tmp_path)
    pid = prompt["id"]
    orig = Persistence._write_conn
    state = {"armed": False, "fired": 0}
    threads: list[threading.Thread] = []

    def _deny() -> None:
        _decide(TestClient(srv.app), pid, "deny")

    @contextmanager
    def _hooked(self: Persistence) -> Generator[Any, None, None]:
        with orig(self) as conn:
            yield conn
        if state["armed"]:
            state["armed"] = False
            state["fired"] += 1
            t = threading.Thread(target=_deny)
            t.start()
            threads.append(t)
            t.join(timeout=2)  # a lock held by the caller must not deadlock us

    monkeypatch.setattr(Persistence, "_write_conn", _hooked)
    state["armed"] = True
    _decide(client, pid, "approve_template")
    for t in threads:
        t.join()
    monkeypatch.setattr(Persistence, "_write_conn", orig)

    assert state["fired"] == 1  # the race window was exercised
    row = db.get_pending_prompt(pid)
    assert row is not None
    assert row["approve_decision"] == "deny"
    assert _rule_rows(db, pid) == 0
