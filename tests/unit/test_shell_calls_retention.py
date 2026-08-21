"""Unit tests for decision-aware shell_calls telemetry retention.

Covers Persistence.cleanup_old_shell_calls (audit rows are never deleted,
only stale passive 'observed_externally' telemetry is pruned, and batching
is bounded), the SHELL_CALLS_RETENTION_S >= RETENTION_S startup invariant,
and the shell_health denominator fix that excludes passive telemetry from
denied_rate_24h / prompt_rate_24h.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from shell_runner.persistence import Persistence
from shell_runner.server import _validate_shell_calls_retention

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Persistence:
    return Persistence(db_path=tmp_path / "test.sqlite3")


def _record_call(db: Persistence, **overrides: object) -> str:
    defaults: dict[str, object] = dict(
        agent_id="test-agent",
        cwd="/tmp",
        raw_cmd="echo hello",
        normalized_template="echo <word>",
        command_tier=1,
        final_tier=1,
        decision="observed_externally",
        matched_rule_pattern=None,
        matched_rule_category=None,
        exit_code=0,
        stdout_bytes=10,
        stderr_bytes=0,
        duration_ms=5,
        decision_path=["observed"],
        normalizer_warnings=[],
    )
    defaults.update(overrides)
    return db.record_call(**defaults)  # type: ignore[arg-type]


def _age_row(db: Persistence, call_id: str, days_ago: int) -> None:
    old_ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    with db._conn() as conn:
        conn.execute("UPDATE shell_calls SET ts = ? WHERE id = ?", (old_ts, call_id))


def _row_exists(db: Persistence, call_id: str) -> bool:
    with db._conn() as conn:
        row = conn.execute("SELECT 1 FROM shell_calls WHERE id = ?", (call_id,)).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# cleanup_old_shell_calls
# ---------------------------------------------------------------------------


def test_old_observed_externally_rows_are_deleted(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    call_id = _record_call(db, decision="observed_externally")
    _age_row(db, call_id, days_ago=30)

    deleted = db.cleanup_old_shell_calls(retention_seconds=86400)  # 1 day

    assert deleted == 1
    assert not _row_exists(db, call_id)


@pytest.mark.parametrize("decision", ["executed", "denied", "prompt_required", "running"])
def test_audit_rows_are_never_deleted_regardless_of_age(tmp_path: Path, decision: str) -> None:
    db = _make_db(tmp_path)
    call_id = _record_call(db, decision=decision)
    _age_row(db, call_id, days_ago=400)

    deleted = db.cleanup_old_shell_calls(retention_seconds=1)  # near-zero window

    assert deleted == 0
    assert _row_exists(db, call_id)


def test_recent_observed_externally_rows_are_not_deleted(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    call_id = _record_call(db, decision="observed_externally")  # ts = now

    deleted = db.cleanup_old_shell_calls(retention_seconds=86400)  # 1 day

    assert deleted == 0
    assert _row_exists(db, call_id)


def test_batching_is_bounded_by_max_batches(tmp_path: Path) -> None:
    """Seeding more rows than batch_size*max_batches must cap the return value."""
    db = _make_db(tmp_path)
    batch_size = 5
    max_batches = 3
    cap = batch_size * max_batches
    total_rows = cap + 7  # more than the cap can process in one invocation

    for _ in range(total_rows):
        call_id = _record_call(db, decision="observed_externally")
        _age_row(db, call_id, days_ago=30)

    deleted = db.cleanup_old_shell_calls(
        retention_seconds=86400, batch_size=batch_size, max_batches=max_batches
    )

    assert deleted == cap
    with db._conn() as conn:
        remaining = conn.execute("SELECT COUNT(*) as cnt FROM shell_calls").fetchone()["cnt"]
    assert remaining == total_rows - cap


# ---------------------------------------------------------------------------
# SHELL_CALLS_RETENTION_S >= RETENTION_S invariant
# ---------------------------------------------------------------------------


def test_shell_calls_retention_invariant_raises_when_violated() -> None:
    with pytest.raises(RuntimeError, match="SHELL_CALLS_RETENTION_S"):
        _validate_shell_calls_retention(shell_calls_retention_s=100, job_retention_s=200)


def test_shell_calls_retention_invariant_allows_equal_or_greater() -> None:
    _validate_shell_calls_retention(shell_calls_retention_s=200, job_retention_s=200)
    _validate_shell_calls_retention(shell_calls_retention_s=300, job_retention_s=200)


# ---------------------------------------------------------------------------
# shell_health denominator fix
# ---------------------------------------------------------------------------


def test_health_stats_excludes_observed_externally_from_rate_denominator(
    tmp_path: Path,
) -> None:
    db = _make_db(tmp_path)
    for _ in range(20):
        _record_call(db, decision="observed_externally")
    for _ in range(2):
        _record_call(db, decision="denied")
    for _ in range(3):
        _record_call(db, decision="prompt_required")
    for _ in range(5):
        _record_call(db, decision="executed")

    stats = db.health_stats()

    assert stats["total_calls_24h"] == 30
    assert stats["observed_24h"] == 20
    # gated denominator = 30 - 20 = 10
    assert abs(stats["denied_rate_24h"] - 2 / 10) < 0.001
    assert abs(stats["prompt_rate_24h"] - 3 / 10) < 0.001
