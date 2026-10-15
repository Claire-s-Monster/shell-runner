"""Unit tests for executor extra PATH dirs (config-session feedback 2026-10-15)."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import shell_runner.executor as executor


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SHELL_RUNNER_CWD_ROOT", "/")
    monkeypatch.delenv("SHELL_RUNNER_CWD_ROOTS", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    importlib.reload(executor)
    yield
    importlib.reload(executor)


def _mkdirs(tmp_path: Path, *names: str) -> list[str]:
    out = []
    for name in names:
        d = tmp_path / name
        d.mkdir(parents=True, exist_ok=True)
        out.append(str(d))
    return out


def test_unset_uses_default_dirs_under_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SHELL_RUNNER_EXTRA_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    _mkdirs(tmp_path, ".pixi/bin", ".cargo/bin")  # .local/bin intentionally absent
    assert executor.extra_path_dirs() == [
        str(tmp_path / ".pixi/bin"),
        str(tmp_path / ".cargo/bin"),
    ]


def test_empty_value_disables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _mkdirs(tmp_path, ".pixi/bin")
    monkeypatch.setenv("SHELL_RUNNER_EXTRA_PATH", "")
    assert executor.extra_path_dirs() == []
    assert executor._build_env(None)["PATH"] == "/usr/bin:/bin"


def test_nonexistent_dirs_skipped_and_order_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = _mkdirs(tmp_path, "a", "b")
    monkeypatch.setenv("SHELL_RUNNER_EXTRA_PATH", f"{b}:{tmp_path}/missing:{a}")
    assert executor.extra_path_dirs() == [b, a]


def test_dirs_already_on_path_not_duplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = _mkdirs(tmp_path, "a", "b")
    monkeypatch.setenv("PATH", f"{a}:/usr/bin")
    monkeypatch.setenv("SHELL_RUNNER_EXTRA_PATH", f"{a}:{b}")
    assert executor._build_env(None)["PATH"] == f"{a}:/usr/bin:{b}"


def test_path_absent_uses_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (a,) = _mkdirs(tmp_path, "a")
    monkeypatch.delenv("PATH")
    monkeypatch.setenv("SHELL_RUNNER_EXTRA_PATH", a)
    assert executor._build_env(None)["PATH"] == f"/usr/local/bin:/usr/bin:/bin:{a}"


def test_path_not_in_passthrough_is_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (a,) = _mkdirs(tmp_path, "a")
    monkeypatch.setenv("SHELL_RUNNER_EXTRA_PATH", a)
    assert "PATH" not in executor._build_env(["HOME"])


def test_execute_shows_extra_dir_last(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (a,) = _mkdirs(tmp_path, "a")
    monkeypatch.setenv("SHELL_RUNNER_EXTRA_PATH", a)
    result = executor.execute(command="printenv PATH", cwd=str(tmp_path))
    assert result.exit_code == 0
    assert result.stdout.strip() == f"/usr/bin:/bin:{a}"
