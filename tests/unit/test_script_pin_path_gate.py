"""The single gate script_pin passes every command-named path through (CodeQL path alerts)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from shell_runner import script_pin


def test_gate_matches_os_path_predicates(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("x")
    (tmp_path / "d").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "f")
    (tmp_path / "dead").symlink_to(tmp_path / "nowhere")
    for name in ("f", "d", "link", "dead", "missing", "d/../f", "f/x"):
        p = str(tmp_path / name)
        assert script_pin._is_regular(p) == os.path.isfile(p), name
        assert script_pin._is_directory(p) == os.path.isdir(p), name
        assert script_pin._path_exists(p) == os.path.exists(p), name


def test_gate_resolves_dotdot_and_symlinks(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "f").write_text("x")
    assert script_pin._is_regular(str(tmp_path / "a" / ".." / "f"))
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    assert script_pin._path_stat(str(tmp_path / "loop")) is None


def test_gate_refuses_nul_and_non_regular(tmp_path: Path) -> None:
    assert script_pin._path_stat(str(tmp_path / "a\0b")) is None
    assert not script_pin._is_regular(str(tmp_path))  # a directory
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert not script_pin._is_regular(str(fifo))
    assert script_pin._read_capped(str(fifo)) == b""  # never opened


def test_read_capped_applies_cap(tmp_path: Path) -> None:
    small = tmp_path / "s"
    small.write_bytes(b"abc")
    assert script_pin._read_capped(str(small)) == b"abc"
    big = tmp_path / "b"
    big.write_bytes(b"x" * (script_pin._ENV_FILE_MAX + 1))
    assert script_pin._read_capped(str(big)) is None


@pytest.mark.parametrize(
    ("command", "expected"),
    [("php -c a/../p.ini x.php", "p.ini"), ("gawk -f a/../x.awk", "x.awk")],
)
def test_dotdot_operands_still_pinned(tmp_path: Path, command: str, expected: str) -> None:
    (tmp_path / "a").mkdir()
    for n in ("p.ini", "x.php", "x.awk"):
        (tmp_path / n).write_text("")
    pin = script_pin.compute_pin(command, str(tmp_path))
    assert pin is not None
    assert str((tmp_path / expected).resolve()) in pin.paths
