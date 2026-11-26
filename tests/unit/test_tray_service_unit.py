"""Static checks of the tray's systemd user unit example (issue #138)."""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

UNIT_FILE = Path(__file__).resolve().parents[2] / "scripts" / "shell-runner-notify.service.example"


def _unit() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]  # keep CamelCase keys
    parser.read_string(UNIT_FILE.read_text())
    return parser


def test_execstart_runs_the_tray_with_system_python() -> None:
    assert _unit()["Service"]["ExecStart"] == "/usr/bin/python3 -m shell_runner.tray"


@pytest.mark.parametrize("word", ["notify-send", "popup", "pixi"])
def test_no_popup_or_pixi_wording_anywhere(word: str) -> None:
    assert word not in UNIT_FILE.read_text().lower()


def test_description() -> None:
    assert (
        _unit()["Unit"]["Description"]
        == "shell-runner silent tray indicator for pending approval prompts"
    )


def test_start_limits_are_in_the_unit_section() -> None:
    unit = _unit()
    assert unit["Unit"]["StartLimitIntervalSec"] == "300"
    assert unit["Unit"]["StartLimitBurst"] == "10"
    assert "StartLimitIntervalSec" not in unit["Service"]
    assert "StartLimitBurst" not in unit["Service"]


def test_restart_policy_is_in_the_service_section() -> None:
    service = _unit()["Service"]
    assert service["Restart"] == "on-failure"
    assert service["RestartSec"] == "10"
    assert service["RestartPreventExitStatus"] == "2"


def test_bound_to_the_graphical_session() -> None:
    unit = _unit()
    assert unit["Unit"]["PartOf"] == "graphical-session.target"
    assert unit["Unit"]["After"] == "graphical-session.target"
    assert unit["Install"]["WantedBy"] == "graphical-session.target"


def test_paths_are_absolute_with_percent_h() -> None:
    service = _unit()["Service"]
    assert service["WorkingDirectory"] == "%h/ClaudeCode/Servers/shell-runner/development"
    assert service["Environment"] == "PYTHONPATH=%h/ClaudeCode/Servers/shell-runner/development/src"


def test_install_note_and_troubleshooting_are_documented() -> None:
    text = UNIT_FILE.read_text()
    assert "sudo apt install python3-gi gir1.2-xapp-1.0" in text
    assert "xdg-open" in text
    assert "# If the tray icon does not appear" in text
    assert "systemctl --user is-active graphical-session.target" in text
    assert "WantedBy=default.target" in text
    assert "import-environment DISPLAY WAYLAND_DISPLAY DBUS_SESSION_BUS_ADDRESS" in text
    assert "StartLimitIntervalSec=0" in text
