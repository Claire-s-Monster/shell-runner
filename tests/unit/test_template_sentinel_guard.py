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
from shell_runner.catalog import Tier
from shell_runner.classifier import classify
from shell_runner.persistence import Persistence, PromotionRule
from shell_runner.seeds import SeedApproval
from shell_runner.server import _apply_template_approval, app
from shell_runner.shellprep import SENTINEL_PREFIX, template_has_sentinel

AGENT = "focused-code-modifier"
# `kill -9` prompts (T3, promotable); the substitution inside a larger word leaks a sentinel.
# (`export PATH=$(date)/bin:$PATH` leaks too, but the script-pin guard refuses its approval.)
# Tests run LEAKY for real with an approve token: the word expands to the non-numeric
# `prexsuffix`, so `kill` only reports an error and can never signal a process.
# `$(...)` forms are also path_guarded (unresolved write target).
LEAKY = "kill -9 pre$(echo x)suffix"
LEAKY_EVIL = "kill -9 pre$(echo evil)suffix"  # same template as LEAKY, only classified
# Leaks a sentinel but is NOT path_guarded: only ever classified, never executed.
UNGUARDED_LEAKY = "kill -9 x$FOO"


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
    for command in (LEAKY, LEAKY_EVIL, UNGUARDED_LEAKY):
        cls = classify(command, str(tmp_path), AGENT)
        assert template_has_sentinel(cls.template)
        assert cls.command_tier >= Tier.APPROVE_ONCE
    assert not classify(UNGUARDED_LEAKY, str(tmp_path), AGENT).path_guarded


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


def test_apply_template_approval_never_verb_promotes_sentinel_template(
    db: Persistence, tmp_path: Path
) -> None:
    """Direct call with an unguarded leaky command, so the verb path is really reached."""
    _insert_verb_rule(db, tmp_path)
    cls = classify(UNGUARDED_LEAKY, str(tmp_path), AGENT)
    assert template_has_sentinel(cls.template)
    assert not cls.path_guarded
    assert db.match_verb_approval("kill", str(tmp_path.resolve()), AGENT).tier == 2

    result = _apply_template_approval(cls, AGENT, str(tmp_path))

    assert result.command_tier == cls.command_tier
    assert result.tier == cls.tier
    assert not any(p.startswith("approved_") for p in result.decision_path)


def test_verb_rule_still_promotes_normal_command(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    _insert_verb_rule(db, tmp_path)
    assert db.match_verb_approval("kill", str(tmp_path.resolve()), AGENT).tier == 2
    cls = classify("kill -9 12345", str(tmp_path), AGENT)
    assert not template_has_sentinel(cls.template)
    promoted = _apply_template_approval(cls, AGENT, str(tmp_path))
    assert any(p.startswith("approved_verb_T2") for p in promoted.decision_path)


def _snapshot(db: Persistence) -> dict[str, list[tuple]]:
    tables = (
        "pending_prompts",
        "template_approvals",
        "verb_approvals",
        "prompt_decisions",
        "rule_revocations",
    )
    with sqlite3.connect(db.db_path) as conn:
        return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in tables}  # noqa: S608


@pytest.mark.parametrize(
    "rule",
    [
        PromotionRule("template", f"kill -9 pre{SENTINEL_PREFIX}0suffix", AGENT, 2),
        PromotionRule("verb", "kill", AGENT, 2, cwd_prefix="/tmp"),
    ],
    ids=["template", "verb"],
)
def test_decide_prompt_refuses_rule_for_sentinel_prompt(
    db: Persistence, tmp_path: Path, rule: PromotionRule
) -> None:
    prompt_id = db.create_pending_prompt(
        agent_id=AGENT,
        cwd=str(tmp_path),
        raw_cmd=LEAKY,
        normalized_template=f"kill -9 pre{SENTINEL_PREFIX}0suffix",
        command_tier=int(Tier.ALWAYS_APPROVE),
        matched_rule_category=None,
        decision_path=[],
    )
    before = _snapshot(db)
    decision = "approve_template" if rule.kind == "template" else "approve_verb"

    with pytest.raises(ValueError, match="masking sentinel"):
        db.decide_prompt(
            prompt_id=prompt_id, decision=decision, approver_agent_id="primary", rule=rule
        )

    assert _snapshot(db) == before
    assert before["pending_prompts"][0][0] == prompt_id


def test_seed_approvals_skips_sentinel_template(
    db: Persistence, caplog: pytest.LogCaptureFixture
) -> None:
    leaky = SeedApproval(
        template=f"curl {SENTINEL_PREFIX}0", tier=Tier.AUTO_LOG, category="x", reason="x"
    )
    normal = SeedApproval(template="curl <safe_url>", tier=Tier.AUTO_LOG, category="x", reason="x")
    with caplog.at_level("WARNING", logger="shell_runner.persistence"):
        assert db.seed_approvals([leaky, normal]) == 1
    assert "sentinel" in caplog.text
    with sqlite3.connect(db.db_path) as conn:
        stored = [r[0] for r in conn.execute("SELECT template FROM template_approvals")]
    assert stored == ["curl <safe_url>"]


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
