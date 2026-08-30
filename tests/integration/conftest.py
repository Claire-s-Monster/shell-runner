"""Shared fixtures for integration tests."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _allow_all_cwd_roots(monkeypatch: pytest.MonkeyPatch):
    """Allow integration tests to use /tmp/* for cwd by setting jail root to /."""
    monkeypatch.setenv("SHELL_RUNNER_CWD_ROOT", "/")
    import shell_runner.executor

    importlib.reload(shell_runner.executor)
    yield
    importlib.reload(shell_runner.executor)


@pytest.fixture
def fresh_db(tmp_path: Path) -> Path:
    return tmp_path / "test.sqlite3"
