"""Shared fixtures for integration tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def fresh_db(tmp_path: Path) -> Path:
    return tmp_path / "test.sqlite3"
