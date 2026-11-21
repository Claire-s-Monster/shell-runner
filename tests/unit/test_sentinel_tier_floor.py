"""Issue #117: a command whose template leaks a masking sentinel never auto-executes.

The masked part (`$VAR`, `$(..)`) inside a larger word was never classified, so the base
tier of the outer verb says nothing about it. The classifier raises T1/T2 to T3 (never
lowers a tier), so every path (execute, shell_classify, re-check, approve) agrees.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shell_runner import executor
from shell_runner.catalog import Tier
from shell_runner.classifier import _require_approval, classify
from shell_runner.persistence import Persistence
from shell_runner.shellprep import template_has_sentinel

AGENT = "focused-code-modifier"
# Outer verb `echo` is auto-log (T1); `x$FOO` leaks a sentinel into the template.
# Harmless to really run: it only prints.
LEAKY_LOW = "echo x$FOO"
# Same shape on a T2-capped agent result and a T3 outer verb (`pwd` is already T3 here).
LEAKY_ALREADY_T3 = "pwd x$FOO"
PLAIN_T1 = "echo hello"


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
    from shell_runner.server import app

    return TestClient(app)


def _execute(client: TestClient, command: str, cwd: Path, **extra: str) -> dict:
    body = {"command": command, "cwd": str(cwd), "agent_id": AGENT, **extra}
    resp = client.post("/execute", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _rule_rows(db: Persistence) -> int:
    with sqlite3.connect(db.db_path) as conn:
        return sum(
            conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
            for t in ("template_approvals", "verb_approvals")
        )


def test_sentinel_on_low_tier_verb_is_raised_to_t3(tmp_path: Path) -> None:
    plain = classify(PLAIN_T1, str(tmp_path), AGENT)
    assert plain.command_tier in (Tier.AUTO_LOG, Tier.AUTO_CAPPED)
    cls = classify(LEAKY_LOW, str(tmp_path), AGENT)
    assert template_has_sentinel(cls.template)
    assert cls.command_tier == Tier.APPROVE_ONCE
    assert cls.tier == Tier.APPROVE_ONCE
    assert any("#117" in p and "sentinel" in p for p in cls.decision_path)


def test_sentinel_on_t3_verb_unchanged(tmp_path: Path) -> None:
    cls = classify(LEAKY_ALREADY_T3, str(tmp_path), AGENT)
    assert template_has_sentinel(cls.template)
    assert cls.command_tier == Tier.APPROVE_ONCE
    assert not any("#117" in p for p in cls.decision_path)


@pytest.mark.parametrize("tier", [Tier.DENY, Tier.APPROVE_ONCE, Tier.ALWAYS_APPROVE])
def test_raise_never_lowers_t0_t3_t4(tmp_path: Path, tier: Tier) -> None:
    base = classify(LEAKY_LOW, str(tmp_path), AGENT)
    forced = dataclasses.replace(base, tier=tier, command_tier=tier, decision_path=())
    out = _require_approval(forced, "x")
    assert (out.tier, out.command_tier) == (tier, tier)
    assert out.decision_path == ()


def test_t4_sentinel_command_stays_t4(tmp_path: Path) -> None:
    cls = classify("sudo rm -rf x$FOO", str(tmp_path), "primary")
    assert template_has_sentinel(cls.template)
    assert cls.command_tier in (Tier.DENY, Tier.ALWAYS_APPROVE)


def test_execute_sentinel_t1_command_prompts(client: TestClient, tmp_path: Path) -> None:
    out = _execute(client, LEAKY_LOW, tmp_path)
    assert out["decision"] == "prompt_required"


def test_shell_classify_reports_raised_tier(client: TestClient, tmp_path: Path) -> None:
    resp = client.post(
        "/classify", json={"command": LEAKY_LOW, "cwd": str(tmp_path), "agent_id": AGENT}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["command_tier"] == int(Tier.APPROVE_ONCE)
    assert body["effective_tier"] == int(Tier.APPROVE_ONCE)
    assert body["decision_preview"] == "would_prompt"


def test_approve_once_then_token_executes_and_creates_no_rule(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    first = _execute(client, LEAKY_LOW, tmp_path)
    assert first["decision"] == "prompt_required"
    resp = client.post(
        "/approve_pending",
        json={
            "prompt_id": first["prompt"]["id"],
            "decision": "approve_once",
            "approver_agent_id": "primary",
        },
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["approve_token"]
    assert token
    done = _execute(client, LEAKY_LOW, tmp_path, approve_token=token)
    assert done["decision"] == "executed"
    assert _rule_rows(db) == 0
    assert _execute(client, LEAKY_LOW, tmp_path)["decision"] == "prompt_required"


def test_non_sentinel_t1_command_still_auto_executes(client: TestClient, tmp_path: Path) -> None:
    cls = classify(PLAIN_T1, str(tmp_path), AGENT)
    assert not template_has_sentinel(cls.template)
    assert _execute(client, PLAIN_T1, tmp_path)["decision"] == "executed"
