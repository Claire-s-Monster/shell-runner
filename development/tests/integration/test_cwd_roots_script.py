"""Integration tests for development/scripts/cwd-roots.sh.

These tests invoke the script as a subprocess with an isolated XDG_CONFIG_HOME
so they never touch the user's real TOML.  The reload/signal steps are skipped
because no daemon is running in CI — missing-PID-file exits are handled
gracefully by the helper itself.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

# Absolute path to the helper script being tested
_SCRIPT = (
    Path(__file__).parent.parent.parent / "scripts" / "cwd-roots.sh"
).resolve()


def _run(
    args: list[str],
    env: dict[str, str] | None = None,
    *,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run cwd-roots.sh with the given args; return CompletedProcess.

    Invokes the script via ``bash`` explicitly so tests pass even if the
    file mode bit is 644 (e.g. freshly checked out without setup-scripts).
    """
    return subprocess.run(
        ["bash", str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        check=check,
    )


def _make_env(tmp_path: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a minimal env with XDG_CONFIG_HOME pointing at tmp_path."""
    env = {
        **os.environ,
        "XDG_CONFIG_HOME": str(tmp_path),
        # Unset runtime dir so the PID file resolves to /tmp/shell-runner.pid
        # (which doesn't exist in CI — that's intentional)
    }
    env.pop("XDG_RUNTIME_DIR", None)
    if extra:
        env.update(extra)
    return env


def _toml_path(xdg_config_home: Path) -> Path:
    return xdg_config_home / "shell-runner" / "cwd-roots.toml"


# ---------------------------------------------------------------------------
# Prerequisite: script exists and is executable
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_script_exists() -> None:
    assert _SCRIPT.exists(), f"Script not found: {_SCRIPT}"


@pytest.mark.integration
def test_script_is_bash() -> None:
    """Script must have a bash shebang (content check, mode-independent)."""
    first_line = _SCRIPT.read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith("#!/"), f"Missing shebang in {_SCRIPT}"
    assert "bash" in first_line, f"Expected bash shebang, got: {first_line}"


# ---------------------------------------------------------------------------
# help / bad args
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_help_exits_zero() -> None:
    result = _run(["--help"])
    assert result.returncode == 0
    assert "Usage" in result.stdout


@pytest.mark.integration
def test_no_args_exits_two() -> None:
    result = _run([])
    assert result.returncode == 2
    assert "Usage" in result.stdout


@pytest.mark.integration
def test_unknown_subcommand_exits_two() -> None:
    result = _run(["frobnicate"])
    assert result.returncode == 2


# ---------------------------------------------------------------------------
# list / add / remove (no daemon required)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_list_creates_empty_toml_if_missing(tmp_path: Path) -> None:
    env = _make_env(tmp_path)
    toml = _toml_path(tmp_path)
    assert not toml.exists()

    result = _run(["list"], env=env)
    assert result.returncode == 0
    assert toml.exists()
    assert "roots = []" in toml.read_text()


@pytest.mark.integration
def test_add_writes_path_to_toml(tmp_path: Path) -> None:
    env = _make_env(tmp_path)

    # Use /tmp because it always exists
    result = _run(["add", "/tmp"], env=env)
    # reload will fail (no PID file) with exit 2; that's acceptable here
    # — we only care that the TOML mutation happened
    toml = _toml_path(tmp_path)
    assert toml.exists()
    content = toml.read_text()
    assert "/tmp" in content


@pytest.mark.integration
def test_add_is_idempotent(tmp_path: Path) -> None:
    env = _make_env(tmp_path)

    _run(["add", "/tmp"], env=env)
    _run(["add", "/tmp"], env=env)

    toml = _toml_path(tmp_path)
    content = toml.read_text()
    # /tmp should appear exactly once
    assert content.count('"/tmp"') == 1


@pytest.mark.integration
def test_remove_deletes_path_from_toml(tmp_path: Path) -> None:
    env = _make_env(tmp_path)

    _run(["add", "/tmp"], env=env)
    toml = _toml_path(tmp_path)
    assert '"/tmp"' in toml.read_text()

    _run(["remove", "/tmp"], env=env)
    assert '"/tmp"' not in toml.read_text()


@pytest.mark.integration
def test_remove_nonexistent_path_is_silent(tmp_path: Path) -> None:
    env = _make_env(tmp_path)

    # Ensure TOML exists with no entries
    _run(["list"], env=env)

    result = _run(["remove", "/tmp"], env=env)
    # Should not error (just silently succeed)
    # Exit code may be 2 from the reload-without-pid step, but TOML stays intact
    toml = _toml_path(tmp_path)
    assert '"/tmp"' not in toml.read_text()


@pytest.mark.integration
def test_add_nonexistent_directory_fails(tmp_path: Path) -> None:
    env = _make_env(tmp_path)
    result = _run(["add", "/this/path/does/not/exist/ever"], env=env)
    assert result.returncode == 1
    assert "does not exist" in result.stderr


@pytest.mark.integration
def test_reload_fails_gracefully_without_pid_file(tmp_path: Path) -> None:
    """reload exits 2 when no PID file is present — not a crash."""
    # Use a XDG_RUNTIME_DIR that doesn't contain a PID file
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    env = _make_env(tmp_path, extra={"XDG_RUNTIME_DIR": str(runtime_dir)})

    result = _run(["reload"], env=env)
    assert result.returncode == 2
    assert "PID file not found" in result.stderr


@pytest.mark.integration
def test_status_shows_toml_when_no_daemon(tmp_path: Path) -> None:
    env = _make_env(tmp_path)
    # Pre-populate TOML
    _run(["add", "/tmp"], env=env)

    result = _run(["status"], env=env)
    assert result.returncode == 0
    assert "/tmp" in result.stdout


@pytest.mark.integration
def test_multiple_paths_preserved_in_order(tmp_path: Path) -> None:
    env = _make_env(tmp_path)

    _run(["add", "/tmp"], env=env)
    _run(["add", "/var/tmp"], env=env)

    toml = _toml_path(tmp_path)
    content = toml.read_text()
    pos_tmp = content.index('"/tmp"')
    pos_var = content.index('"/var/tmp"')
    assert pos_tmp < pos_var, "Insertion order should be preserved"
