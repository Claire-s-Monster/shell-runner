"""End-to-end: rules-file verdicts are enforced by /execute and recorded in shell_calls."""

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
    proj = tmp_path / "proj"
    (proj / "tests" / "acceptance").mkdir(parents=True)
    (cfg / "projects" / "p.toml").write_text(
        f"""
roots = ["{proj}"]

[[protected]]
glob = "tests/acceptance/**"
reason = "frozen"

[[protected]]
glob = "NOTES.md"
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


def test_denied_protected_write_is_recorded_and_not_executed(
    project: Path, audit_db: Persistence
) -> None:
    target = project / "tests" / "acceptance" / "a.py"
    body = _execute("touch tests/acceptance/a.py", project)
    assert body["decision"] == "denied"
    assert "frozen" in body["stderr"]
    assert not target.exists()

    (row,) = audit_db.telemetry_query()
    assert row["flagged"] == 1
    assert row["matched_rule_category"] == "protected_path"
    (match,) = json.loads(row["protected_path_match"])
    assert match["kind"] == "write"
    assert match["mode"] == "deny"
    assert match["path"] == str(target)


def test_flag_mode_executes_and_records_flag(project: Path, audit_db: Persistence) -> None:
    body = _execute("touch NOTES.md", project)
    assert body["decision"] == "executed"
    assert (project / "NOTES.md").exists()

    (row,) = audit_db.telemetry_query()
    assert row["flagged"] == 1
    assert json.loads(row["protected_path_match"])[0]["mode"] == "flag"


def test_shell_audit_flagged_only_returns_flag_mode_write(
    project: Path, audit_db: Persistence
) -> None:
    (project / "NOTES.md").write_text("hi")
    _execute("cat NOTES.md", project)
    _execute("ls", project)
    _execute("touch NOTES.md", project)

    resp = TestClient(app).post("/audit", json={"flagged_only": True})
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["notes"] == []
    (row,) = data["rows"]
    assert row["raw_cmd"] == "touch NOTES.md"
    assert row["flagged"] == 1
    assert json.loads(row["protected_path_match"])[0]["mode"] == "flag"


def test_touch_is_recorded_without_flag(project: Path, audit_db: Persistence) -> None:
    (project / "NOTES.md").write_text("hi")
    body = _execute("cat NOTES.md", project)
    assert body["decision"] == "executed"

    (row,) = audit_db.telemetry_query()
    assert row["flagged"] == 0
    assert json.loads(row["protected_path_match"])[0]["kind"] == "touch"


def test_unrelated_call_has_empty_audit_fields(project: Path, audit_db: Persistence) -> None:
    _execute("ls", project)
    (row,) = audit_db.telemetry_query()
    assert row["flagged"] == 0
    assert row["protected_path_match"] is None


def test_rules_config_error_denies_everything(
    project: Path, tmp_path: Path, audit_db: Persistence
) -> None:
    (tmp_path / "cfg" / "rules.toml").write_text("[[deny")
    body = _execute("ls", project)
    assert body["decision"] == "denied"
    assert "rules.toml" in body["stderr"]
    (row,) = audit_db.telemetry_query()
    assert row["matched_rule_category"] == "rules_config_error"
