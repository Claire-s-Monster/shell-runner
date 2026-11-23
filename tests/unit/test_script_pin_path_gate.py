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
    assert script_pin._read_capped(str(fifo)) == b""  # opened non-blocking, fstat refuses it


def test_open_regular_returns_fd_of_regular_file(tmp_path: Path) -> None:
    (tmp_path / "f").write_bytes(b"abc")
    fd, st = script_pin._open_regular(str(tmp_path / "a" / ".." / "f"))
    try:
        assert st.st_size == 3
        assert os.read(fd, 10) == b"abc"
    finally:
        os.close(fd)


def test_open_regular_refuses_non_regular_and_bad_paths(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(script_pin._NotRegularError):
        script_pin._open_regular(str(fifo))
    with pytest.raises(script_pin._NotRegularError):
        script_pin._open_regular(str(tmp_path))
    with pytest.raises(FileNotFoundError):
        script_pin._open_regular(str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="null"):
        script_pin._open_regular(str(tmp_path / "a\0b"))


def test_open_regular_does_not_follow_a_final_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink swapped in after realpath is refused by O_NOFOLLOW, not followed."""
    (tmp_path / "target").write_bytes(b"secret")
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "target")
    monkeypatch.setattr(os.path, "realpath", lambda p: str(link))
    with pytest.raises(OSError, match="symbolic links|Too many"):
        script_pin._open_regular(str(link))
    assert script_pin._read_capped(str(link)) == b""
    assert script_pin._file_sha256(str(link), [1 << 20]) == script_pin.UNREADABLE


def _swap_after_fstat(monkeypatch: pytest.MonkeyPatch, victim: Path) -> None:
    """Replace `victim` with a FIFO right after the descriptor has been checked."""
    real_fstat = os.fstat

    def swapping_fstat(fd: int) -> os.stat_result:
        result = real_fstat(fd)
        monkeypatch.setattr(os, "fstat", real_fstat)
        victim.unlink()
        os.mkfifo(victim)
        return result

    monkeypatch.setattr(os, "fstat", swapping_fstat)


def test_read_capped_reads_the_object_that_was_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    victim = tmp_path / "v"
    victim.write_bytes(b"original")
    _swap_after_fstat(monkeypatch, victim)
    assert script_pin._read_capped(str(victim)) == b"original"  # not the FIFO that replaced it


def test_file_sha256_hashes_the_object_that_was_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    victim = tmp_path / "v"
    victim.write_bytes(b"original")
    _swap_after_fstat(monkeypatch, victim)
    digest = script_pin._file_sha256(str(victim), [1 << 20])
    assert digest == hashlib.sha256(b"original").hexdigest()


def test_file_sha256_states(tmp_path: Path) -> None:
    (tmp_path / "f").write_bytes(b"x")
    os.mkfifo(tmp_path / "fifo")
    budget = [1 << 20]
    assert script_pin._file_sha256(str(tmp_path / "missing"), budget) == script_pin.MISSING
    assert script_pin._file_sha256(str(tmp_path / "f" / "x"), budget) == script_pin.MISSING
    assert script_pin._file_sha256(str(tmp_path), budget) == script_pin.UNREADABLE
    assert script_pin._file_sha256(str(tmp_path / "fifo"), budget) == script_pin.UNREADABLE
    assert script_pin._file_sha256(str(tmp_path / "a\0b"), budget) == script_pin.UNREADABLE
    assert script_pin._file_sha256(str(tmp_path / "f"), [0]) == script_pin._OVER_BUDGET


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
