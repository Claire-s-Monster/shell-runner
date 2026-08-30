"""Unit tests for seed approval mechanism (Persistence.seed_approvals)."""

from __future__ import annotations

from pathlib import Path

import pytest

from shell_runner.catalog import Tier
from shell_runner.normalizer import normalize
from shell_runner.persistence import Persistence
from shell_runner.seeds import DEFAULT_SEED_APPROVALS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _db(tmp_path: Path) -> Persistence:
    return Persistence(db_path=tmp_path / "test.sqlite3")


def _row_count(db: Persistence) -> int:
    with db._conn() as conn:
        row = conn.execute("SELECT COUNT(*) FROM template_approvals").fetchone()
        return int(row[0])


def _fetch_row(db: Persistence, template: str) -> dict | None:
    with db._conn() as conn:
        row = conn.execute(
            "SELECT template, agent_id, approved_tier FROM template_approvals WHERE template = ?",
            (template,),
        ).fetchone()
        return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Test 1: fresh DB — all seeds inserted at T2 with agent_id IS NULL
# ---------------------------------------------------------------------------


def test_seeds_inserts_into_empty_db(tmp_path: Path) -> None:
    db = _db(tmp_path)
    n = db.seed_approvals(DEFAULT_SEED_APPROVALS)

    assert n == len(DEFAULT_SEED_APPROVALS)
    assert _row_count(db) == len(DEFAULT_SEED_APPROVALS)

    for seed in DEFAULT_SEED_APPROVALS:
        row = _fetch_row(db, seed.template)
        assert row is not None, f"missing seed template: {seed.template!r}"
        assert row["agent_id"] is None, "seed must be a global (agent_id IS NULL) row"
        assert row["approved_tier"] == int(Tier.AUTO_CAPPED)


# ---------------------------------------------------------------------------
# Test 2: idempotent — second run inserts nothing
# ---------------------------------------------------------------------------


def test_seeds_idempotent(tmp_path: Path) -> None:
    db = _db(tmp_path)
    first = db.seed_approvals(DEFAULT_SEED_APPROVALS)
    second = db.seed_approvals(DEFAULT_SEED_APPROVALS)

    assert first == len(DEFAULT_SEED_APPROVALS)
    assert second == 0
    assert _row_count(db) == len(DEFAULT_SEED_APPROVALS)


# ---------------------------------------------------------------------------
# Test 3: pre-existing user approval for one template — that row is untouched
# ---------------------------------------------------------------------------


def test_seeds_skip_existing_user_approval(tmp_path: Path) -> None:
    db = _db(tmp_path)
    target = DEFAULT_SEED_APPROVALS[0].template

    # Insert a user approval at T4 (less permissive than T2)
    db.create_template_approval(
        template=target,
        agent_id=None,
        approved_tier=int(Tier.ALWAYS_APPROVE),
    )

    seeds = DEFAULT_SEED_APPROVALS
    n = db.seed_approvals(seeds)

    # One template was already present — should be skipped
    assert n == len(seeds) - 1

    # User row must remain at T4, not overwritten by T2
    row = _fetch_row(db, target)
    assert row is not None
    assert row["approved_tier"] == int(Tier.ALWAYS_APPROVE)


# ---------------------------------------------------------------------------
# Test 4: agent-specific approval for one template — skip check is global
# ---------------------------------------------------------------------------


def test_seeds_skip_agent_specific_approval(tmp_path: Path) -> None:
    db = _db(tmp_path)
    target = DEFAULT_SEED_APPROVALS[1].template

    # Insert an agent-scoped approval for this template
    db.create_template_approval(
        template=target,
        agent_id="foo-agent",
        approved_tier=int(Tier.AUTO_CAPPED),
    )

    n = db.seed_approvals(DEFAULT_SEED_APPROVALS)

    # That template already had a row (agent-scoped) — must be skipped
    assert n == len(DEFAULT_SEED_APPROVALS) - 1

    # The agent-specific row must still be there
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT agent_id FROM template_approvals WHERE template = ?",
            (target,),
        ).fetchall()

    agent_ids = [r["agent_id"] for r in rows]
    # Only the original agent-scoped row; no new global row
    assert "foo-agent" in agent_ids
    assert None not in agent_ids


# ---------------------------------------------------------------------------
# Test 5: seed templates round-trip through normalizer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_cmd", "expected_template"),
    [
        (
            "curl https://raw.githubusercontent.com/org/repo/main/file.txt",
            "curl <safe_url>",
        ),
        (
            "curl https://api.github.com/repos/org/repo/releases | jq .",
            "curl <safe_url> | <safe_pipe>",
        ),
        (
            "curl https://raw.githubusercontent.com/org/repo/main/f.txt | grep foo | head -20",
            "curl <safe_url> | <safe_pipe> | <safe_pipe>",
        ),
    ],
)
def test_default_seed_list_normalizes_to_same_form(raw_cmd: str, expected_template: str) -> None:
    result = normalize(raw_cmd, cwd="/tmp")
    assert result.template == expected_template, (
        f"normalize({raw_cmd!r}) -> {result.template!r}, expected {expected_template!r}"
    )
    # Confirm the expected template is present in the seed list
    seed_templates = {s.template for s in DEFAULT_SEED_APPROVALS}
    assert expected_template in seed_templates, (
        f"{expected_template!r} not found in DEFAULT_SEED_APPROVALS"
    )
