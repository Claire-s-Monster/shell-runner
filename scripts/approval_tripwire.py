"""Tripwire for uncontrolled growth of the approval tables.

The `template_approvals` table in the shell-runner catalog DB records
approvals that promote command templates to executable tiers. It has been
manually cleaned up twice (559 -> 492 rows) because nothing currently stops
an agent from directly requesting `approve_template` and quietly growing the
table between cleanups.

This script is a cheap, read-only health check: it compares the current
`template_approvals` row count against a known-good baseline and exits
non-zero if the table has grown past it. It also reports `verb_approvals`
row count (informational only, not gated) and a "breadth signal": the
number of `template_approvals` rows whose `template` column contains a path
placeholder (see `PATH_PLACEHOLDERS` in `src/shell_runner/normalizer.py`).
Path-placeholder templates are the dangerous ones — a single approval on
one of these applies far more broadly than the specific command that earned
it, so a rising breadth-signal count deserves scrutiny even if the raw
total is under baseline.

Raising TEMPLATE_APPROVALS_BASELINE is a deliberate, reviewable act (it
should accompany a commit that explains why the new rows are legitimate),
not something to bump casually just to make this script stop complaining.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# Mirrors DEFAULT_DB_PATH in src/shell_runner/persistence.py:22
DB_PATH = Path.home() / ".local/share/shell-runner/telemetry.sqlite3"

# Baselines measured 2026-08-22, after two manual cleanup passes (559 -> 492).
TEMPLATE_APPROVALS_BASELINE = 492
VERB_APPROVALS_BASELINE = None  # not yet baselined; report count only, never gate on it

# Literal placeholder tokens, mirrored from PATH_PLACEHOLDERS in
# src/shell_runner/normalizer.py. Do not invent new ones here; if the
# normalizer's tuple changes, update this list to match.
PATH_PLACEHOLDERS: tuple[str, ...] = (
    "<cwd_path>",
    "<tmp_path>",
    "<etc_path>",
    "<system_path>",
    "<home_path>",
    "<abs_path>",
)


def _table_count(conn: sqlite3.Connection, table: str) -> int | None:
    """Return row count for `table`, or None if the table does not exist."""
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    if cursor.fetchone() is None:
        return None
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608


def _breadth_signal_count(conn: sqlite3.Connection) -> int | None:
    """Count template_approvals rows whose template contains a path placeholder."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='template_approvals'"
    )
    if cursor.fetchone() is None:
        return None
    like_clauses = " OR ".join("template LIKE ?" for _ in PATH_PLACEHOLDERS)
    params = [f"%{token}%" for token in PATH_PLACEHOLDERS]
    query = f"SELECT COUNT(*) FROM template_approvals WHERE {like_clauses}"  # noqa: S608
    return conn.execute(query, params).fetchone()[0]


def collect_metrics(db_path: Path, baseline: int) -> dict[str, object]:
    """Collect all tripwire metrics from the read-only DB connection."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        template_count = _table_count(conn, "template_approvals")
        verb_count = _table_count(conn, "verb_approvals")
        breadth_count = _breadth_signal_count(conn)
    finally:
        conn.close()

    template_count = 0 if template_count is None else template_count
    verb_count = 0 if verb_count is None else verb_count
    breadth_count = 0 if breadth_count is None else breadth_count

    return {
        "db_path": str(db_path),
        "template_approvals_count": template_count,
        "template_approvals_baseline": baseline,
        "template_approvals_delta": template_count - baseline,
        "verb_approvals_count": verb_count,
        "verb_approvals_baseline": VERB_APPROVALS_BASELINE,
        "breadth_signal_count": breadth_count,
        "tripwire_tripped": template_count > baseline,
    }


def _print_human(metrics: dict[str, object]) -> None:
    status = "TRIPPED" if metrics["tripwire_tripped"] else "ok"
    print(
        f"template_approvals: {metrics['template_approvals_count']} "
        f"(baseline {metrics['template_approvals_baseline']}, "
        f"delta {metrics['template_approvals_delta']:+d}) -> {status}"
    )
    print(f"verb_approvals: {metrics['verb_approvals_count']} (no baseline; report only)")
    print(f"breadth signal (path-placeholder templates): {metrics['breadth_signal_count']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report and gate on approval-table growth in the shell-runner catalog DB."
    )
    parser.add_argument("--json", action="store_true", help="emit metrics as a single JSON object")
    parser.add_argument(
        "--baseline",
        type=int,
        default=TEMPLATE_APPROVALS_BASELINE,
        help=f"override TEMPLATE_APPROVALS_BASELINE for this run (default: {TEMPLATE_APPROVALS_BASELINE})",
    )
    args = parser.parse_args(argv)

    if not DB_PATH.exists():
        print(f"approval_tripwire: no DB at {DB_PATH}; nothing to check (not a violation)")
        return 0

    metrics = collect_metrics(DB_PATH, args.baseline)

    if args.json:
        print(json.dumps(metrics))
    else:
        _print_human(metrics)

    return 1 if metrics["tripwire_tripped"] else 0


if __name__ == "__main__":
    sys.exit(main())
