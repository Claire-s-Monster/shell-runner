"""Pytest configuration and shared fixtures for shell-runner tests."""

from __future__ import annotations

from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "systemd: needs a real systemd user manager (skipped by default; "
        "pixi run -e dev pytest tests/integration/test_unit_systemd_smoke.py -m systemd)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip systemd-marked tests unless explicitly selected with -m."""
    if "systemd" in (config.option.markexpr or ""):
        return
    skip = pytest.mark.skip(reason="systemd tests need -m systemd")
    for item in items:
        if "systemd" in item.keywords:
            item.add_marker(skip)


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
