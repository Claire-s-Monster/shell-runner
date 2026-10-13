"""Integration tests for scripts/shell-runner-self-restart.sh using stub binaries on PATH."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).parent.parent.parent / "scripts" / "shell-runner-self-restart.sh"
).resolve()


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def _run(tmp_path: Path, *, restart_rc: int, http_code: str) -> tuple[int, dict[str, object]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir, "systemctl", f"exit {restart_rc}")
    _stub(bin_dir, "curl", f"printf '{http_code}'")
    _stub(bin_dir, "journalctl", "printf 'line \"quoted\"\\nsecond\\n'")
    _stub(bin_dir, "sleep", "exit 0")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "SHELL_RUNNER_RESTART_MAX_ATTEMPTS": "2",
    }
    proc = subprocess.run(
        ["bash", str(_SCRIPT)], env=env, capture_output=True, text=True, check=False
    )
    state = tmp_path / "state" / "shell-runner" / "last-restart.json"
    return proc.returncode, json.loads(state.read_text())


@pytest.mark.integration
def test_restart_failure_is_not_ok(tmp_path: Path) -> None:
    rc, record = _run(tmp_path, restart_rc=5, http_code="200")
    assert rc != 0
    assert record["ok"] is False
    assert record["restart_rc"] == 5
    assert 'line "quoted"' in str(record["journal_tail"])


@pytest.mark.integration
def test_restart_ok_and_healthy(tmp_path: Path) -> None:
    rc, record = _run(tmp_path, restart_rc=0, http_code="200")
    assert rc == 0
    assert record["ok"] is True
    assert record["restart_rc"] == 0
    assert record["attempts"] == 1


@pytest.mark.integration
def test_restart_ok_but_unhealthy(tmp_path: Path) -> None:
    rc, record = _run(tmp_path, restart_rc=0, http_code="503")
    assert rc != 0
    assert record["ok"] is False
    assert record["attempts"] == 2
