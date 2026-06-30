"""Pytest configuration and shared fixtures for shell-runner tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def fresh_db(tmp_path: Path) -> Path:
    """Return a path to a fresh (empty) SQLite database file."""
    return tmp_path / "test.sqlite3"
