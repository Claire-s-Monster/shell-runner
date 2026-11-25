"""Static guard: shell-runner must only bind to loopback (issue #136).

POST /pending is unauthenticated, so a non-loopback bind would expose
approval-prompt details. Port 4111 serves both the HTTP API and /mcp.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
LOOPBACK_HOST = re.compile(r"--host 127\.0\.0\.1(\s|$)")
HOST_FLAG = re.compile(r"--host(?:=|\s)")
NON_VARIABLE_HOST = re.compile(r'--host[= ](?!"?\$)(?!127\.0\.0\.1(?:\s|$))\S')


def _execstart_lines(name: str) -> list[str]:
    text = (SCRIPTS / name).read_text()
    return [line for line in text.splitlines() if line.startswith("ExecStart=")]


@pytest.mark.parametrize("name", ["shell-runner.service", "shell-runner.service.example"])
def test_unit_execstart_binds_loopback_only(name: str) -> None:
    lines = _execstart_lines(name)
    assert len(lines) == 1, f"{name}: expected exactly one ExecStart"
    assert LOOPBACK_HOST.search(lines[0]), f"{name}: ExecStart lacks --host 127.0.0.1"
    assert len(HOST_FLAG.findall(lines[0])) == 1, f"{name}: ExecStart needs exactly one --host"
    assert "0.0.0.0" not in lines[0]


@pytest.mark.parametrize("name", ["shell-runner.service", "shell-runner.service.example"])
def test_units_listen_on_4111_only(name: str) -> None:
    lines = _execstart_lines(name)
    assert len(lines) == 1, f"{name}: expected exactly one ExecStart"
    assert "--port 4111" in lines[0], f"{name}: ExecStart lacks --port 4111"
    text = (SCRIPTS / name).read_text()
    assert "4112" not in text, f"{name}: unexpected port 4112"


def test_daemon_script_default_host_is_loopback() -> None:
    text = (SCRIPTS / "shell-runner-daemon.sh").read_text()
    assert re.search(r'^DEFAULT_HOST="127\.0\.0\.1"$', text, re.MULTILINE)


def test_daemon_script_fallback_still_uses_default_host() -> None:
    text = (SCRIPTS / "shell-runner-daemon.sh").read_text()
    assert re.search(
        r'^SHELL_RUNNER_HOST="\$\{SHELL_RUNNER_HOST:-\$DEFAULT_HOST\}"$', text, re.MULTILINE
    )


def test_daemon_script_launch_uses_host_variable() -> None:
    lines = (SCRIPTS / "shell-runner-daemon.sh").read_text().splitlines()
    assert any('--host "$SHELL_RUNNER_HOST"' in line for line in lines), (
        'daemon launch must pass --host "$SHELL_RUNNER_HOST"'
    )
    offenders = [line for line in lines if NON_VARIABLE_HOST.search(line)]
    assert not offenders, f"literal non-loopback --host in daemon script: {offenders}"


def test_loopback_pattern_rejects_other_hosts() -> None:
    assert not LOOPBACK_HOST.search("uvicorn app --host 0.0.0.0 --port 4111")
    assert not LOOPBACK_HOST.search("uvicorn app --host 127.0.0.10 --port 4111")
    assert not LOOPBACK_HOST.search("uvicorn app --port 4111")
