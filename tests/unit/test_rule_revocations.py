"""Rule revocations are audited and queryable through shell_audit (issue #100)."""

# ruff: noqa: F811  (fixtures imported from test_deny_revokes_rules are used as arguments)

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shell_runner.persistence import Persistence, PromotionRule
from tests.unit.test_deny_revokes_rules import (  # noqa: F401  (fixtures)
    CMD_A,
    _decide,
    _post,
    _prompt,
    _rule_rows,
    calls,
    client,
    db,
)


def _new_prompt(db: Persistence, agent: str = "worker") -> str:
    return db.create_pending_prompt(
        agent_id=agent,
        cwd="/tmp",
        raw_cmd="x",
        normalized_template="x",
        command_tier=4,
        matched_rule_category=None,
        decision_path=[],
    )


def _link_rules(db: Persistence, pid: str, tmp_path: Path) -> dict[str, int]:
    """One scoped template, one global template and one verb rule on prompt pid."""
    with db._write_conn() as conn:
        return {
            "template": Persistence._insert_template_approval(
                conn,
                template="tmpl scoped",
                agent_id="worker",
                approved_tier=2,
                approved_via_prompt_id=pid,
                approved_by="primary",
            ),
            "template_global": Persistence._insert_template_approval(
                conn,
                template="tmpl global",
                agent_id=None,
                approved_tier=2,
                approved_via_prompt_id=pid,
                approved_by="primary",
            ),
            "verb": Persistence._insert_verb_approval(
                conn,
                verb="myverb",
                cwd_prefix=str(tmp_path),
                agent_id="worker",
                approved_tier=2,
                approved_via_prompt_id=pid,
                approved_by="primary",
            ),
        }


def _revocations(db: Persistence) -> list[dict[str, Any]]:
    with db._conn() as conn:
        rows = conn.execute("SELECT * FROM rule_revocations ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def test_deny_records_one_row_per_revoked_rule(db: Persistence, tmp_path: Path) -> None:
    pid = _new_prompt(db)
    ids = _link_rules(db, pid, tmp_path)

    result = db.decide_prompt(
        prompt_id=pid, decision="deny", approver_agent_id="primary", reason="too broad"
    )

    assert result.revoked_rules == 3
    raw = _revocations(db)
    assert len(raw) == result.revoked_rules == 3
    rows = {r["rule_kind"]: r for r in raw}
    assert set(rows) == {"template", "template_global", "verb"}
    for kind, row in rows.items():
        assert row["rule_id"] == ids[kind]
        assert row["prompt_id"] == pid
        assert row["rule_prompt_id"] == pid
        assert row["cause"] == "deny"
        assert row["approver_agent_id"] == "primary"
        assert row["reason"] == "too broad"
        assert row["revoked_at"]
    assert rows["template"]["rule_text"] == "tmpl scoped"
    assert rows["template"]["rule_agent_id"] == "worker"
    assert rows["template_global"]["rule_text"] == "tmpl global"
    assert rows["template_global"]["rule_agent_id"] is None
    assert rows["verb"]["rule_text"] == "myverb"
    assert rows["verb"]["cwd_prefix"] == str(tmp_path)
    assert rows["verb"]["rule_agent_id"] == "worker"
    assert rows["template"]["cwd_prefix"] is None


def test_deny_through_route_records_reason(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    prompt = _prompt(client, CMD_A, tmp_path)
    _decide(client, prompt["id"], "approve_template")
    resp = client.post(
        "/approve_pending",
        json={
            "prompt_id": prompt["id"],
            "decision": "deny",
            "approver_agent_id": "primary",
            "reason": "changed my mind",
        },
    )
    assert resp.status_code == 200, resp.text
    (row,) = _revocations(db)
    assert row["reason"] == "changed my mind"
    assert row["approver_agent_id"] == "primary"
    assert row["rule_kind"] == "template"
    assert row["prompt_id"] == prompt["id"]


def test_deny_revoking_nothing_records_nothing(db: Persistence) -> None:
    pid = _new_prompt(db)

    result = db.decide_prompt(prompt_id=pid, decision="deny", approver_agent_id="primary")

    assert result.revoked_rules == 0
    assert _revocations(db) == []


def test_orphaned_prompt_deny_records(db: Persistence, tmp_path: Path) -> None:
    pid = _new_prompt(db)
    ids = _link_rules(db, pid, tmp_path)
    with db._write_conn() as conn:
        conn.execute("DELETE FROM pending_prompts WHERE id = ?", (pid,))

    result = db.decide_prompt(
        prompt_id=pid, decision="deny", approver_agent_id="primary", reason="purged"
    )

    assert result.revoked_rules == 3
    rows = _revocations(db)
    assert len(rows) == result.revoked_rules == 3
    assert {r["rule_kind"] for r in rows} == {"template", "template_global", "verb"}
    assert {r["rule_id"] for r in rows} == set(ids.values())
    assert all(r["reason"] == "purged" and r["prompt_id"] == pid for r in rows)


def test_failed_audit_insert_rolls_back_the_revocation(db: Persistence, tmp_path: Path) -> None:
    pid = _new_prompt(db)
    _link_rules(db, pid, tmp_path)
    with db._write_conn() as conn:
        conn.execute(
            "CREATE TRIGGER boom BEFORE INSERT ON rule_revocations "
            "WHEN NEW.rule_kind = 'verb' BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )

    with pytest.raises(sqlite3.DatabaseError, match="boom"):
        db.decide_prompt(prompt_id=pid, decision="deny", approver_agent_id="primary")

    assert _rule_rows(db, pid) == 3  # nothing deleted
    assert _revocations(db) == []  # earlier inserts rolled back too
    row = db.get_pending_prompt(pid)
    assert row is not None
    assert row["approve_decision"] is None  # decision rolled back as well
    with db._write_conn() as conn:
        conn.execute("DROP TRIGGER boom")
        assert not conn.in_transaction


# --- shell_audit mode=revocations ---------------------------------------------


def _seed_revocations(db: Persistence, tmp_path: Path) -> dict[str, str]:
    """Two denies: prompt p1 (agent worker, approver alice), p2 (agent other, approver bob)."""
    p1 = _new_prompt(db, "worker")
    _link_rules(db, p1, tmp_path)
    db.decide_prompt(prompt_id=p1, decision="deny", approver_agent_id="alice", reason="r1")
    p2 = _new_prompt(db, "other")
    with db._write_conn() as conn:
        Persistence._insert_template_approval(
            conn,
            template="tmpl other",
            agent_id="other-agent",
            approved_tier=2,
            approved_via_prompt_id=p2,
            approved_by="bob",
        )
    db.decide_prompt(prompt_id=p2, decision="deny", approver_agent_id="bob", reason="r2")
    return {"p1": p1, "p2": p2}


def test_audit_revocations_row_shape_and_order(db: Persistence, tmp_path: Path) -> None:
    ps = _seed_revocations(db, tmp_path)
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    with db._write_conn() as conn:
        conn.execute(
            "UPDATE rule_revocations SET revoked_at = ? WHERE prompt_id = ?", (old, ps["p1"])
        )

    result = db.audit_query(mode="revocations")

    assert result["mode"] == "revocations"
    assert result["count"] == 4
    assert result["truncated"] is False
    assert result["notes"] == []
    rows = result["rows"]
    assert rows[0]["prompt_id"] == ps["p2"]  # newest first
    assert [r["ts"] for r in rows] == sorted((r["ts"] for r in rows), reverse=True)
    assert set(rows[0]) == {
        "ts",
        "prompt_id",
        "approver_agent_id",
        "rule_kind",
        "rule_id",
        "rule_text",
        "rule_agent_id",
        "cwd_prefix",
        "reason",
        "cause",
        "rule_prompt_id",
    }


def test_audit_revocations_since_until(db: Persistence, tmp_path: Path) -> None:
    ps = _seed_revocations(db, tmp_path)
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    with db._write_conn() as conn:
        conn.execute(
            "UPDATE rule_revocations SET revoked_at = ? WHERE prompt_id = ?", (old, ps["p1"])
        )
    cut = (datetime.now(UTC) - timedelta(days=1)).isoformat()

    assert {r["prompt_id"] for r in db.audit_query(mode="revocations", since=cut)["rows"]} == {
        ps["p2"]
    }
    assert {r["prompt_id"] for r in db.audit_query(mode="revocations", until=cut)["rows"]} == {
        ps["p1"]
    }


def test_audit_revocations_agent_filters_match_approver_or_rule_agent(
    db: Persistence, tmp_path: Path
) -> None:
    ps = _seed_revocations(db, tmp_path)

    by_approver = db.audit_query(mode="revocations", agent_id="alice")
    assert {r["prompt_id"] for r in by_approver["rows"]} == {ps["p1"]}
    by_rule_agent = db.audit_query(mode="revocations", agent_id="other-agent")
    assert {r["prompt_id"] for r in by_rule_agent["rows"]} == {ps["p2"]}
    assert db.audit_query(mode="revocations", agent_id="nobody")["count"] == 0

    by_prefix_approver = db.audit_query(mode="revocations", agent_id_prefix="bo")
    assert {r["prompt_id"] for r in by_prefix_approver["rows"]} == {ps["p2"]}
    by_prefix_rule = db.audit_query(mode="revocations", agent_id_prefix="work")
    assert {r["prompt_id"] for r in by_prefix_rule["rows"]} == {ps["p1"]}
    assert db.audit_query(mode="revocations", agent_id_prefix="w_rk")["count"] == 0


def test_audit_revocations_prompt_id_and_rule_kind(db: Persistence, tmp_path: Path) -> None:
    ps = _seed_revocations(db, tmp_path)

    by_prompt = db.audit_query(mode="revocations", prompt_id=ps["p1"])
    assert by_prompt["count"] == 3
    by_kind = db.audit_query(mode="revocations", rule_kind="template")
    assert {r["rule_kind"] for r in by_kind["rows"]} == {"template"}
    assert by_kind["count"] == 2
    both = db.audit_query(mode="revocations", prompt_id=ps["p1"], rule_kind="verb")
    assert [r["rule_text"] for r in both["rows"]] == ["myverb"]


def test_audit_revocations_limit_truncates(db: Persistence, tmp_path: Path) -> None:
    _seed_revocations(db, tmp_path)

    result = db.audit_query(mode="revocations", limit=2)

    assert result["count"] == 2
    assert result["truncated"] is True
    assert db.audit_query(mode="revocations", limit=4)["truncated"] is False


def test_audit_other_modes_unaffected_and_note_ignored_params(
    db: Persistence, tmp_path: Path
) -> None:
    _seed_revocations(db, tmp_path)

    rows = db.audit_query(mode="rows", prompt_id="x", rule_kind="verb")
    assert rows["rows"] == []
    assert rows["count"] == 0
    assert any("prompt_id" in n and "revocations" in n for n in rows["notes"])
    assert any("rule_kind" in n and "revocations" in n for n in rows["notes"])

    clean = db.audit_query(mode="rows")
    assert clean["notes"] == []
    assert "rule_kind" not in str(clean)


def test_audit_summary_includes_revocation_count(db: Persistence, tmp_path: Path) -> None:
    ps = _seed_revocations(db, tmp_path)
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    with db._write_conn() as conn:
        conn.execute(
            "UPDATE rule_revocations SET revoked_at = ? WHERE prompt_id = ?", (old, ps["p1"])
        )

    assert db.audit_query(mode="summary")["revocations"] == 4
    since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    assert db.audit_query(mode="summary", since=since)["revocations"] == 1


def test_audit_revocations_ordered_by_id_desc_not_timestamp(
    db: Persistence, tmp_path: Path
) -> None:
    _seed_revocations(db, tmp_path)
    future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    with db._write_conn() as conn:
        conn.execute("UPDATE rule_revocations SET revoked_at = ? WHERE id = 1", (future,))
    with db._conn() as conn:
        expected = [r[0] for r in conn.execute("SELECT id FROM rule_revocations ORDER BY id DESC")]

    result = db.audit_query(mode="revocations")

    assert expected == [4, 3, 2, 1]
    assert [r["rule_text"] for r in result["rows"]] == [
        "tmpl other",
        "myverb",
        "tmpl global",
        "tmpl scoped",
    ]


def test_audit_revocations_notes_for_ignored_filters(db: Persistence, tmp_path: Path) -> None:
    _seed_revocations(db, tmp_path)

    noisy = db.audit_query(mode="revocations", decision="denied", flagged_only=True)

    assert noisy["count"] == 4  # the ignored filters did not narrow anything
    assert any("decision ignored" in n and "revocations" in n for n in noisy["notes"])
    assert any("flagged_only ignored" in n and "revocations" in n for n in noisy["notes"])
    assert db.audit_query(mode="revocations")["notes"] == []


def test_audit_cause_filter_and_note_in_other_modes(db: Persistence, tmp_path: Path) -> None:
    old, new = _new_prompt(db), _new_prompt(db)
    rule = PromotionRule(kind="template", key="k", agent_id="worker", approved_tier=2)
    db.decide_prompt(prompt_id=old, decision="approve_template", rule=rule)
    db.decide_prompt(prompt_id=new, decision="approve_template", rule=rule)  # replaces old's rule
    db.decide_prompt(prompt_id=new, decision="deny")

    replaced = db.audit_query(mode="revocations", cause="replaced")
    denied = db.audit_query(mode="revocations", cause="deny")

    assert replaced["count"] == 1
    assert replaced["rows"][0]["rule_prompt_id"] == old
    assert denied["count"] == 1
    assert denied["rows"][0]["rule_prompt_id"] == new
    assert db.audit_query(mode="revocations", cause="approve_verb")["count"] == 0
    assert db.audit_query(mode="revocations")["count"] == 2
    assert any("cause ignored" in n for n in db.audit_query(mode="rows", cause="deny")["notes"])


@pytest.mark.parametrize(
    ("decision", "rule_kwargs", "kind", "text"),
    [
        (
            "approve_template",
            {"kind": "template", "key": "k", "agent_id": "worker"},
            "template",
            "k",
        ),
        (
            "approve_template_global",
            {"kind": "template", "key": "g", "agent_id": None},
            "template_global",
            "g",
        ),
        ("approve_verb", {"kind": "verb", "key": "v", "agent_id": "worker"}, "verb", "v"),
    ],
)
def test_replaced_rule_from_another_prompt_is_recorded(
    db: Persistence,
    tmp_path: Path,
    decision: str,
    rule_kwargs: dict[str, Any],
    kind: str,
    text: str,
) -> None:
    rule = PromotionRule(approved_tier=2, cwd_prefix=str(tmp_path), **rule_kwargs)
    old, new = _new_prompt(db), _new_prompt(db)
    old_rule_id = db.decide_prompt(prompt_id=old, decision=decision, rule=rule).rule_id
    assert _revocations(db) == []  # a first rule displaces nothing

    result = db.decide_prompt(
        prompt_id=new,
        decision=decision,
        approver_agent_id="primary",
        reason="supersede",
        rule=rule,
    )

    assert result.revoked_rules == 0  # behaviour unchanged: the replace is not a revocation
    raw = _revocations(db)
    assert len(raw) == 1
    (row,) = raw
    assert row["cause"] == "replaced"
    assert row["prompt_id"] == new
    assert row["rule_prompt_id"] == old
    assert row["rule_id"] == old_rule_id
    assert row["rule_kind"] == kind
    assert row["rule_text"] == text
    assert row["approver_agent_id"] == "primary"
    assert row["reason"] == "supersede"
    assert row["cwd_prefix"] == (str(tmp_path) if kind == "verb" else None)
    assert _rule_rows(db, old) == 0
    assert _rule_rows(db, new) == 1


@pytest.mark.parametrize("decision", ["deny", "approve_once", "approve_template"])
def test_cause_is_the_triggering_decision(db: Persistence, tmp_path: Path, decision: str) -> None:
    pid = _new_prompt(db)
    _link_rules(db, pid, tmp_path)
    rule = (
        PromotionRule(kind="template", key="tmpl scoped", agent_id="worker", approved_tier=2)
        if decision == "approve_template"
        else None
    )

    result = db.decide_prompt(prompt_id=pid, decision=decision, rule=rule)

    raw = _revocations(db)
    assert len(raw) == result.revoked_rules == 3  # re-created rule is not a 'replaced' row
    assert {r["cause"] for r in raw} == {decision}
    assert {r["rule_prompt_id"] for r in raw} == {pid}


# --- schema and retention -------------------------------------------------------


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def test_table_created_on_fresh_db(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite3"
    Persistence(db_path=path)
    assert "rule_revocations" in _tables(path)


def test_table_added_to_existing_pre_100_db(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    first = Persistence(db_path=path)
    first.close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE rule_revocations")
    conn.commit()
    conn.close()
    assert "rule_revocations" not in _tables(path)

    db = Persistence(db_path=path)

    assert "rule_revocations" in _tables(path)
    assert db.audit_query(mode="revocations")["count"] == 0


def test_cleanup_functions_never_purge_revocations(db: Persistence, tmp_path: Path) -> None:
    _seed_revocations(db, tmp_path)
    ancient = (datetime.now(UTC) - timedelta(days=5000)).isoformat()
    with db._write_conn() as conn:
        conn.execute("UPDATE rule_revocations SET revoked_at = ?", (ancient,))
        conn.execute(
            "UPDATE pending_prompts SET created_at = ?, expires_at = ?", (ancient, ancient)
        )

    db.cleanup_expired_prompts(approver_retention_seconds=1)
    db.cleanup_old_shell_calls(retention_seconds=1)

    assert len(_revocations(db)) == 4
