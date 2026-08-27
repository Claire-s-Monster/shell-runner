"""Unit tests for shell-runner models."""

import importlib
import os
import sys

import pytest
from pydantic import ValidationError


def test_max_timeout_default_is_3600(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that MAX_TIMEOUT_S defaults to 3600 seconds."""
    monkeypatch.delenv("SHELL_RUNNER_MAX_TIMEOUT_S", raising=False)
    # Remove cached module to force re-import with fresh env
    if "shell_runner.models" in sys.modules:
        del sys.modules["shell_runner.models"]
    import shell_runner.models

    assert shell_runner.models.MAX_TIMEOUT_S == 3600
    assert shell_runner.models.DEFAULT_MAX_TIMEOUT_S == 3600


def test_max_timeout_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that MAX_TIMEOUT_S respects SHELL_RUNNER_MAX_TIMEOUT_S env var."""
    monkeypatch.setenv("SHELL_RUNNER_MAX_TIMEOUT_S", "7200")
    # Remove cached module to force re-import with fresh env
    if "shell_runner.models" in sys.modules:
        del sys.modules["shell_runner.models"]
    import shell_runner.models

    assert shell_runner.models.MAX_TIMEOUT_S == 7200


def test_execute_request_default_timeout() -> None:
    """Test that ExecuteRequest defaults to 30 second timeout."""
    from shell_runner.models import ExecuteRequest

    req = ExecuteRequest(command="echo hello", cwd="/tmp", agent_id="test-agent")
    assert req.timeout_s == 30


def test_execute_request_custom_timeout() -> None:
    """Test that ExecuteRequest accepts custom timeout values."""
    from shell_runner.models import ExecuteRequest

    req = ExecuteRequest(command="echo hello", cwd="/tmp", agent_id="test-agent", timeout_s=120)
    assert req.timeout_s == 120


def test_execute_request_rejects_timeout_below_minimum() -> None:
    """Test that ExecuteRequest rejects timeout < 1."""
    from shell_runner.models import ExecuteRequest

    with pytest.raises(ValidationError):
        ExecuteRequest(command="echo hello", cwd="/tmp", agent_id="test-agent", timeout_s=0)


def test_execute_request_rejects_timeout_above_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that ExecuteRequest rejects timeout > MAX_TIMEOUT_S."""
    monkeypatch.setenv("SHELL_RUNNER_MAX_TIMEOUT_S", "60")
    # Remove cached module to force re-import with fresh env
    if "shell_runner.models" in sys.modules:
        del sys.modules["shell_runner.models"]
    import shell_runner.models

    importlib.reload(shell_runner.models)
    # Re-import ExecuteRequest after reload
    from shell_runner.models import ExecuteRequest

    with pytest.raises(ValidationError):
        ExecuteRequest(command="echo hello", cwd="/tmp", agent_id="test-agent", timeout_s=120)


def test_execute_request_accepts_max_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that ExecuteRequest accepts timeout == MAX_TIMEOUT_S."""
    monkeypatch.setenv("SHELL_RUNNER_MAX_TIMEOUT_S", "120")
    # Remove cached module to force re-import with fresh env
    if "shell_runner.models" in sys.modules:
        del sys.modules["shell_runner.models"]
    import shell_runner.models

    importlib.reload(shell_runner.models)
    # Re-import ExecuteRequest after reload
    from shell_runner.models import ExecuteRequest

    req = ExecuteRequest(command="echo hello", cwd="/tmp", agent_id="test-agent", timeout_s=120)
    assert req.timeout_s == 120
