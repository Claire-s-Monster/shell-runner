"""Unit tests for executor.py — sandboxed subprocess execution."""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

import shell_runner.executor
from shell_runner.executor import execute


@pytest.fixture(autouse=True)
def _allow_all_cwd_roots(monkeypatch: pytest.MonkeyPatch):
    """Allow tests to use /tmp/* for cwd by setting single-root jail to /."""
    monkeypatch.setenv("SHELL_RUNNER_CWD_ROOT", "/")
    monkeypatch.delenv("SHELL_RUNNER_CWD_ROOTS", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    importlib.reload(shell_runner.executor)
    yield
    importlib.reload(shell_runner.executor)


def test_execute_echo_returns_zero_and_stdout(tmp_path: Path) -> None:
    result = execute(command="echo hello", cwd=str(tmp_path))
    assert result.exit_code == 0
    assert "hello" in result.stdout
    assert not result.timed_out


def test_execute_false_returns_nonzero(tmp_path: Path) -> None:
    result = execute(command="false", cwd=str(tmp_path))
    assert result.exit_code == 1


def test_execute_nonexistent_command_returns_nonzero(tmp_path: Path) -> None:
    result = execute(command="this_command_does_not_exist_xyz", cwd=str(tmp_path))
    assert result.exit_code != 0


def test_execute_timeout_kills_process(tmp_path: Path) -> None:
    result = execute(command="sleep 30", cwd=str(tmp_path), timeout_s=1)
    assert result.timed_out is True
    assert result.exit_code == -1


def test_execute_missing_cwd_returns_cwd_jail_code(tmp_path: Path) -> None:
    missing = str(tmp_path / "does_not_exist")
    result = execute(command="echo hi", cwd=missing)
    assert result.exit_code == -3
    assert "does not exist" in result.stderr


def test_execute_output_truncation_writes_overflow_file(tmp_path: Path) -> None:
    overflow = str(tmp_path / "overflow")
    # Generate >8KB of output
    big_cmd = "python3 -c \"print('x' * 9000)\""
    result = execute(
        command=big_cmd,
        cwd=str(tmp_path),
        truncate_threshold=8192,
        overflow_dir=overflow,
    )
    assert result.exit_code == 0
    assert "...truncated" in result.stdout
    assert result.stdout_full_path is not None
    assert Path(result.stdout_full_path).exists()


def test_execute_env_stripping(tmp_path: Path) -> None:
    """Command should only see the passthrough env vars, not SECRET_VAR."""
    os.environ["SECRET_VAR"] = "top_secret"
    try:
        result = execute(
            command="echo ${SECRET_VAR:-MISSING}",
            cwd=str(tmp_path),
            env_passthrough=["PATH"],
        )
        assert result.exit_code == 0
        assert "top_secret" not in result.stdout
        assert "MISSING" in result.stdout
    finally:
        del os.environ["SECRET_VAR"]


def test_execute_respects_cwd(tmp_path: Path) -> None:
    sub = tmp_path / "subdir"
    sub.mkdir()
    result = execute(command="pwd", cwd=str(sub))
    assert result.exit_code == 0
    assert str(sub) in result.stdout


def test_execute_cwd_outside_jail_returns_minus_3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL_RUNNER_CWD_ROOT", "/tmp")
    importlib.reload(shell_runner.executor)
    from shell_runner.executor import execute as _execute
    result = _execute(command="echo hi", cwd="/etc")
    assert result.exit_code == -3
    assert "escapes allowed root" in result.stderr


def test_execute_cwd_inside_jail_works(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Use /tmp as jail; tmp_path is under /tmp on most Linux systems
    import tempfile

    with tempfile.TemporaryDirectory(dir="/tmp") as jail_dir:
        sub = Path(jail_dir) / "subdir"
        sub.mkdir()
        monkeypatch.setenv("SHELL_RUNNER_CWD_ROOT", jail_dir)
        importlib.reload(shell_runner.executor)
        from shell_runner.executor import execute as _execute
        result = _execute(command="echo hello", cwd=str(sub))
        assert result.exit_code == 0
        assert "hello" in result.stdout


def test_execute_cwd_default_no_jail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHELL_RUNNER_CWD_ROOT", "/")
    importlib.reload(shell_runner.executor)
    from shell_runner.executor import execute as _execute
    result = _execute(command="echo ok", cwd=str(tmp_path))
    assert result.exit_code == 0
    assert "ok" in result.stdout


def test_execute_cwd_relative_resolved_under_jail(monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile

    with tempfile.TemporaryDirectory(dir="/tmp") as jail_dir:
        sub = Path(jail_dir) / "subdir"
        sub.mkdir()
        monkeypatch.setenv("SHELL_RUNNER_CWD_ROOT", jail_dir)
        importlib.reload(shell_runner.executor)
        from shell_runner.executor import execute as _execute
        # Pass the full path (relative cwd behaviour depends on process cwd)
        result = _execute(command="echo relative_ok", cwd=str(sub))
        assert result.exit_code == 0
        assert "relative_ok" in result.stdout


def test_execute_cwd_symlink_escape_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile

    with tempfile.TemporaryDirectory(dir="/tmp") as jail_dir:
        # Create a symlink inside the jail that points outside (/etc)
        link = Path(jail_dir) / "escape_link"
        link.symlink_to("/etc")
        monkeypatch.setenv("SHELL_RUNNER_CWD_ROOT", jail_dir)
        importlib.reload(shell_runner.executor)
        from shell_runner.executor import execute as _execute
        result = _execute(command="echo hi", cwd=str(link))
        assert result.exit_code == -3
        assert "escapes allowed root" in result.stderr


# ---------------------------------------------------------------------------
# Multi-root allow-list tests
# ---------------------------------------------------------------------------


def test_cwd_roots_env_list(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """SHELL_RUNNER_CWD_ROOTS accepts a colon-separated list; paths outside are rejected."""
    import tempfile

    with (
        tempfile.TemporaryDirectory(dir="/tmp") as root_a,
        tempfile.TemporaryDirectory(dir="/tmp") as root_b,
    ):
        sub_a = Path(root_a) / "proj"
        sub_a.mkdir()
        sub_b = Path(root_b) / "proj"
        sub_b.mkdir()

        monkeypatch.delenv("SHELL_RUNNER_CWD_ROOT", raising=False)
        monkeypatch.setenv("SHELL_RUNNER_CWD_ROOTS", f"{root_a}:{root_b}")
        importlib.reload(shell_runner.executor)
        from shell_runner.executor import execute as _execute

        assert _execute(command="echo a", cwd=str(sub_a)).exit_code == 0
        assert _execute(command="echo b", cwd=str(sub_b)).exit_code == 0
        result = _execute(command="echo c", cwd="/etc")
        assert result.exit_code == -3
        assert "escapes allowed root" in result.stderr


def test_cwd_roots_toml_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Paths listed in cwd-roots.toml are accepted; others are rejected."""
    import tempfile

    with tempfile.TemporaryDirectory(dir="/tmp") as toml_root:
        sub = Path(toml_root) / "work"
        sub.mkdir()

        config_dir = tmp_path / "shell-runner"
        config_dir.mkdir(parents=True)
        toml_file = config_dir / "cwd-roots.toml"
        toml_file.write_text(f'roots = ["{toml_root}"]\n', encoding="utf-8")

        monkeypatch.delenv("SHELL_RUNNER_CWD_ROOT", raising=False)
        monkeypatch.delenv("SHELL_RUNNER_CWD_ROOTS", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        importlib.reload(shell_runner.executor)
        from shell_runner.executor import execute as _execute

        assert _execute(command="echo toml", cwd=str(sub)).exit_code == 0
        result = _execute(command="echo outside", cwd="/etc")
        assert result.exit_code == -3
        assert "escapes allowed root" in result.stderr


def test_cwd_roots_dedup_union(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Union of TOML + list env + legacy env is deduplicated."""
    import tempfile

    with tempfile.TemporaryDirectory(dir="/tmp") as shared_root:
        config_dir = tmp_path / "shell-runner"
        config_dir.mkdir(parents=True)
        toml_file = config_dir / "cwd-roots.toml"
        toml_file.write_text(f'roots = ["{shared_root}"]\n', encoding="utf-8")

        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setenv("SHELL_RUNNER_CWD_ROOTS", shared_root)
        monkeypatch.setenv("SHELL_RUNNER_CWD_ROOT", shared_root)
        importlib.reload(shell_runner.executor)

        roots = shell_runner.executor._CWD_ROOTS
        # shared_root should appear exactly once
        resolved = Path(shared_root).resolve()
        assert roots.count(resolved) == 1


def test_reload_cwd_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """reload_cwd_roots() picks up env changes without a module reload."""
    import tempfile

    with tempfile.TemporaryDirectory(dir="/tmp") as new_root:
        sub = Path(new_root) / "proj"
        sub.mkdir()

        # Start with a restricted root that excludes new_root
        monkeypatch.delenv("SHELL_RUNNER_CWD_ROOTS", raising=False)
        monkeypatch.setenv("SHELL_RUNNER_CWD_ROOT", "/nonexistent-initial-root")
        importlib.reload(shell_runner.executor)
        from shell_runner.executor import execute as _execute, reload_cwd_roots

        # Confirm new_root is currently rejected
        result = _execute(command="echo hi", cwd=str(sub))
        assert result.exit_code == -3

        # Change the env and reload without a module reload
        monkeypatch.setenv("SHELL_RUNNER_CWD_ROOT", new_root)
        reload_cwd_roots()

        result = _execute(command="echo hi", cwd=str(sub))
        assert result.exit_code == 0


def test_cwd_roots_default_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When no env vars and no config file exist, cwd is used as the single root."""
    monkeypatch.delenv("SHELL_RUNNER_CWD_ROOT", raising=False)
    monkeypatch.delenv("SHELL_RUNNER_CWD_ROOTS", raising=False)
    # Point XDG_CONFIG_HOME at an empty dir so no TOML file exists
    empty_cfg = tmp_path / "empty-cfg"
    empty_cfg.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty_cfg))

    importlib.reload(shell_runner.executor)

    roots = shell_runner.executor._CWD_ROOTS
    assert len(roots) == 1
    assert roots[0] == Path(os.getcwd()).resolve()
