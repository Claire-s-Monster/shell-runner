"""Pytest configuration and shared fixtures for shell-runner tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_rules_config(tmp_path_factory: pytest.TempPathFactory, monkeypatch):
    """Point the rules config dir at an empty tmp dir so a developer's real
    ~/.config/shell-runner rules never leak into tests."""
    monkeypatch.setenv("SHELL_RUNNER_CONFIG_DIR", str(tmp_path_factory.mktemp("rules-config")))


@pytest.fixture(autouse=True)
def _close_write_conns():
    """Close shared Persistence write connections so tmp dbs don't leak handles."""
    yield
    from shell_runner.persistence import _close_all_write_conns

    _close_all_write_conns()


@pytest.fixture
def fresh_db(tmp_path: Path) -> Path:
    """Return a path to a fresh (empty) SQLite database file."""
    return tmp_path / "test.sqlite3"
