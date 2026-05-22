"""Unit tests for find_similar_approved_templates and _compute_suggestions."""

from __future__ import annotations

from pathlib import Path

import pytest

from shell_runner.models import ExecuteSuggestion
from shell_runner.persistence import Persistence


def _db(tmp_path: Path) -> Persistence:
    return Persistence(db_path=tmp_path / "test.sqlite3")


def _approve(db: Persistence, template: str, tier: int, agent_id: str | None = None) -> None:
    db.create_template_approval(template=template, agent_id=agent_id, approved_tier=tier)


# ---------------------------------------------------------------------------
# Persistence layer
# ---------------------------------------------------------------------------


def test_returns_empty_when_no_approvals(tmp_path: Path) -> None:
    db = _db(tmp_path)
    result = db.find_similar_approved_templates("curl <url>", "agent-a")
    assert result == []


def test_excludes_exact_template_match(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _approve(db, "curl <safe_url>", tier=2)
    result = db.find_similar_approved_templates("curl <safe_url>", "agent-a")
    assert result == []


def test_verb_anchor_required(tmp_path: Path) -> None:
    db = _db(tmp_path)
    # Why: Jaccard would be high (shared tokens), but verb "curl" != "wget"
    _approve(db, "curl <safe_url> | jq", tier=2)
    result = db.find_similar_approved_templates("wget <safe_url>", "agent-a")
    assert result == []


def test_jaccard_ordering(tmp_path: Path) -> None:
    db = _db(tmp_path)
    # query = "curl <url> --verbose --header <hdr>"  (5 tokens)
    # high overlap: 4/5 shared tokens
    _approve(db, "curl <url> --verbose --header <hdr> --extra", tier=2, agent_id=None)
    # medium overlap: 3/5
    _approve(db, "curl <url> --verbose", tier=2, agent_id=None)
    # low overlap: 2/5 — may fall below min_similarity=0.5
    _approve(db, "curl <url>", tier=2, agent_id=None)

    query = "curl <url> --verbose --header <hdr>"
    result = db.find_similar_approved_templates(query, "agent-a", min_similarity=0.0)
    returned_templates = [r[0] for r in result]

    # Highest-overlap template should come first
    assert returned_templates[0] == "curl <url> --verbose --header <hdr> --extra"
    assert returned_templates[1] == "curl <url> --verbose"


def test_global_precedence_over_agent_scoped(tmp_path: Path) -> None:
    db = _db(tmp_path)
    template = "curl <url> --flag"
    _approve(db, template, tier=2, agent_id=None)       # global, more permissive
    _approve(db, template, tier=4, agent_id="agent-a")  # agent-scoped, less permissive

    result = db.find_similar_approved_templates("curl <url>", "agent-a", min_similarity=0.0)
    # Why: dedup must keep tier=2 (more permissive) and return exactly one row
    assert len(result) == 1
    assert result[0][1] == 2


def test_agent_scoped_isolation(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _approve(db, "curl <url> --flag", tier=2, agent_id="alpha")
    # Why: beta has no approval and there's no global; should see nothing
    result = db.find_similar_approved_templates("curl <url>", "beta", min_similarity=0.0)
    assert result == []


def test_example_raw_cmd_populated(tmp_path: Path) -> None:
    db = _db(tmp_path)
    template = "curl <url> --flag"
    _approve(db, template, tier=2)
    # Insert a matching shell_calls row with decision='executed'
    db.record_call(
        agent_id="agent-a",
        cwd="/tmp",
        raw_cmd="curl https://example.com --flag",
        normalized_template=template,
        command_tier=2,
        final_tier=2,
        decision="executed",
        matched_rule_pattern=None,
        matched_rule_category=None,
        exit_code=0,
        stdout_bytes=0,
        stderr_bytes=0,
        duration_ms=10,
        decision_path=[],
        normalizer_warnings=[],
    )

    result = db.find_similar_approved_templates("curl <url>", "agent-a", min_similarity=0.0)
    assert len(result) == 1
    assert result[0][3] == "curl https://example.com --flag"


# ---------------------------------------------------------------------------
# _compute_suggestions unit tests (server helper, no HTTP round-trip)
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_server(tmp_path: Path):
    """Yield (_compute_suggestions fn, Persistence) with server's db swapped."""
    import shell_runner.server as srv_mod

    tmp_db = _db(tmp_path)
    original_db = srv_mod.db
    srv_mod.db = tmp_db
    yield srv_mod._compute_suggestions, tmp_db
    srv_mod.db = original_db


def test_compute_suggestions_returns_none_when_no_neighbors(patched_server) -> None:  # type: ignore[type-arg]
    compute, _ = patched_server
    result = compute("curl <url>", "agent-a")
    assert result is None


def test_compute_suggestions_returns_executesuggestion_list(patched_server) -> None:  # type: ignore[type-arg]
    compute, db = patched_server
    _approve(db, "curl <url> --flag", tier=2)

    result = compute("curl <url>", "agent-a")
    assert result is not None
    assert len(result) >= 1
    suggestion = result[0]
    assert isinstance(suggestion, ExecuteSuggestion)
    assert suggestion.template == "curl <url> --flag"
    assert suggestion.tier == 2
