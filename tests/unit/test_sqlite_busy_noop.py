"""Unit tests for _is_sqlite_busy_noop (issue #42).

Identifies a command that failed WITHOUT taking effect because SQLite's busy
handler rejected the statement before applying it (sqlite3 CLI defaults
busy_timeout to 0), so the failure is worth reporting distinctly from an
ordinary non-zero exit.
"""

from __future__ import annotations

import pytest

from shell_runner.server import _is_sqlite_busy_noop


@pytest.mark.parametrize(
    "stderr",
    [
        "Error: database is locked",
        "Error: DATABASE IS LOCKED",
        "sqlite3: database table is locked",
        "SQLITE3: DATABASE TABLE IS LOCKED",
    ],
)
def test_nonzero_exit_with_busy_marker_is_noop(stderr: str) -> None:
    assert _is_sqlite_busy_noop(exit_code=1, stderr=stderr) is True


def test_zero_exit_with_busy_marker_is_not_noop() -> None:
    assert _is_sqlite_busy_noop(exit_code=0, stderr="database is locked") is False


def test_nonzero_exit_with_unrelated_stderr_is_not_noop() -> None:
    assert _is_sqlite_busy_noop(exit_code=1, stderr="no such table: foo") is False
