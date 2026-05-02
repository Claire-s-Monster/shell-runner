"""Unit tests for executor.py — sandboxed subprocess execution."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from shell_runner.executor import execute


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
