"""shell_audit(flagged_only) returns a flag-mode write into a roth_planner-style protected path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shell_runner import rules
from shell_runner.persistence import Persistence
from shell_runner.server import app

AGENT = "trial-agent"


@pytest.fixture
def audit_db(fresh_db: Path, monkeypatch: pytest.MonkeyPatch) -> Persistence:
    fresh = Persistence(db_path=fresh_db)
    monkeypatch.setattr("shell_runner.server.db", fresh)
    return fresh


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "cfg"
    (cfg / "projects").mkdir(parents=True)
    monkeypatch.setenv(rules.CONFIG_DIR_ENV, str(cfg))
    proj = tmp_path / "roth_planner"
    (proj / "tests" / "acceptance").mkdir(parents=True)
    (cfg / "projects" / "roth_planner.toml").write_text(
        f"""
roots = ["{proj}"]

[[protected]]
glob = "tests/acceptance/**"
mode = "flag"

[[protected]]
glob = "tests/acceptance.sha256"
mode = "flag"
"""
    )
    return proj


def _execute(command: str, cwd: Path) -> dict:
    resp = TestClient(app).post(
        "/execute", json={"command": command, "cwd": str(cwd), "agent_id": AGENT}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_shell_audit_flagged_only_returns_acceptance_write(
    project: Path, audit_db: Persistence
) -> None:
    # harmless: the write target is inside the temp project root
    body = _execute("echo x >> tests/acceptance/foo.feature", project)
    assert body["decision"] == "executed"
    _execute("echo x >> tests/other.txt", project)

    resp = TestClient(app).post("/audit", json={"flagged_only": True})
    assert resp.status_code == 200, resp.text
    (row,) = resp.json()["rows"]
    assert row["raw_cmd"] == "echo x >> tests/acceptance/foo.feature"
    assert row["flagged"] == 1
    (match,) = json.loads(row["protected_path_match"])
    assert match["kind"] == "write"
    assert match["mode"] == "flag"
