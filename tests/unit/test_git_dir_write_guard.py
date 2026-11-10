"""Issue #84: writes into a git directory must not auto-execute.

Planting `core.fsmonitor`, `core.pager`, hooks, `include.path`, ... in `.git/` makes
the next (auto-executed) `git status` run attacker code. `WAS` is the catalog tier
before the guard, asserted via `_classify_catalog`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from shell_runner import executor, path_guard
from shell_runner.catalog import Tier
from shell_runner.classifier import _classify_catalog, classify
from shell_runner.write_targets import analyze

AGENT = "some-agent"
T2, T3 = Tier.AUTO_CAPPED, Tier.APPROVE_ONCE
RULE = "path_guard:git_dir_write"


@pytest.fixture(autouse=True)
def _roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor, "_CWD_ROOTS", [tmp_path.resolve()])
    monkeypatch.setenv("SHELL_RUNNER_CONFIG_DIR", str(tmp_path / "no-rules"))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = (tmp_path / "repo").resolve()
    (root / ".git" / "hooks").mkdir(parents=True)
    (root / ".git" / "config").write_text("[core]\n")
    (root / "sub" / "deep").mkdir(parents=True)
    return root


@pytest.fixture
def linked(tmp_path: Path) -> Path:
    """A linked worktree: `.git` FILE -> main/.git/worktrees/wt, with a commondir."""
    main = (tmp_path / "main").resolve()
    admin = main / ".git" / "worktrees" / "wt"
    admin.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n")
    wt = (tmp_path / "wt").resolve()
    (wt / "src").mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {admin}\n")
    return wt


def _cls(command: str, cwd: Path):  # noqa: ANN202
    return classify(command, str(cwd), AGENT, env={"HOME": "/nonexistent-home"})


def _assert_guarded(command: str, cwd: Path) -> None:
    cls = _cls(command, cwd)
    assert cls.command_tier == T3, command
    assert cls.path_guarded, command
    assert cls.flagged, command
    assert cls.matched_rule is not None
    assert cls.matched_rule.pattern == RULE
    assert cls.matched_rule.category == "git_dir_write"
    assert cls.protected_path_json is not None
    assert "builtin:.git" in cls.protected_path_json


# (command, WAS)
VECTORS = [
    ("echo '[core]' >> .git/config", T2),
    ("echo '        fsmonitor = touch /tmp/pwn' >> .git/config", T2),
    ("echo '[core]' > .git/config", T2),
    ("echo 'pager = x' >> .git/config", T2),
    ("echo '[diff]' >> .git/config", T2),
    ("echo '[filter \"x\"]' >> .git/config", T2),
    ("echo '[core] hooksPath = /tmp/h' >> .git/config", T2),
    ("echo '[include] path = /tmp/x' >> .git/config", T2),
    ("echo x > .git/hooks/pre-commit", T2),
    ("printf x >> .git/config", T3),
    ("cp x .git/hooks/pre-commit", T3),
    ("chmod +x .git/hooks/pre-commit", T3),
]


@pytest.mark.parametrize(("command", "was"), VECTORS)
def test_vectors_are_guarded(repo: Path, command: str, was: Tier) -> None:
    assert _classify_catalog(command, str(repo), AGENT).tier == was  # effective tier, agent cap
    _assert_guarded(command, repo)


WRITE_SHAPES = [
    "echo x > .git/config",
    "echo x >> .git/config",
    "echo x &> .git/config",
    "echo x &>> .git/config",
    "echo x 2> .git/config",
    "echo x 2>.git/config",
    "echo x >| .git/config",
    "echo x >.git/config",
    "echo x > .git/config 2>&1",
    "echo x | tee .git/config",
    "echo x | tee -a .git/config",
    "cat x | tee a.txt .git/config",
    "cp x .git/config",
    "cp -t .git/hooks x",
    "cp -f x .git/hooks/pre-commit",
    "mv x .git/config",
    "mv .git/config x",
    "install -m755 x .git/hooks/pre-commit",
    "ln -s /tmp/x .git/hooks/pre-commit",
    "touch .git/hooks/pre-commit",
    "mkdir .git/hooks/new",
    "sed -i s/a/b/ .git/config",
    "sed -i -e s/a/b/ .git/config",
    "truncate -s 0 .git/config",
    "dd if=x of=.git/config",
    "echo x > .git",
    "echo x > ./.git/config",
    "echo x > sub/../.git/config",
    "ls && echo x >> .git/config",
    "echo ok; touch .git/hooks/post-commit",
    "cd sub && echo x >> ../.git/config",
    "cd .git && echo x >> config",
]


@pytest.mark.parametrize("command", WRITE_SHAPES)
def test_write_shapes_are_guarded(repo: Path, command: str) -> None:
    _assert_guarded(command, repo)


def test_relative_dotdot_from_subdir(repo: Path) -> None:
    _assert_guarded("echo x >> ../.git/config", repo / "sub")
    _assert_guarded("echo x >> ../../.git/config", repo / "sub" / "deep")


def test_absolute_path(repo: Path) -> None:
    _assert_guarded(f"echo x >> {repo}/.git/config", repo)
    _assert_guarded(f"echo x >> {repo}/.git/config", repo / "sub")


def test_home_relative_path(repo: Path, tmp_path: Path) -> None:
    cls = classify(
        "echo x >> ~/repo/.git/config", str(repo), AGENT, env={"HOME": str(tmp_path.resolve())}
    )
    assert cls.matched_rule is not None and cls.matched_rule.pattern == RULE


def test_symlink_into_git_dir(repo: Path) -> None:
    (repo / "hk").symlink_to(repo / ".git" / "hooks")
    (repo / "cfg").symlink_to(repo / ".git" / "config")
    _assert_guarded("echo x > hk/pre-commit", repo)
    _assert_guarded("echo x >> cfg", repo)
    _assert_guarded("cp x hk/pre-commit", repo)
    _assert_guarded("tee -a cfg", repo)


def test_symlink_to_git_dir_itself(repo: Path) -> None:
    (repo / "g").symlink_to(repo / ".git")
    _assert_guarded("echo x >> g/config", repo)


def test_linked_worktree_dot_git_file_and_gitdir(linked: Path, tmp_path: Path) -> None:
    admin = tmp_path / "main" / ".git" / "worktrees" / "wt"
    _assert_guarded("echo x > .git", linked)
    _assert_guarded(f"echo x >> {admin}/config", linked)
    _assert_guarded(f"echo x >> {admin}/hooks/pre-commit", linked / "src")
    _assert_guarded(f"echo x >> {tmp_path}/main/.git/config", linked)  # commondir
    _assert_guarded("echo x >> ../main/.git/config", linked)


def test_gitdir_without_dot_git_name_and_relative_pointer(tmp_path: Path) -> None:
    """`gitdir:` (relative to the .git file) and its commondir are guarded even
    when no path component is literally `.git`."""
    store = (tmp_path / "store").resolve()
    (store / "wt-admin").mkdir(parents=True)
    (store / "common").mkdir()
    (store / "wt-admin" / "commondir").write_text("../common\n")
    wt = (tmp_path / "proj").resolve()
    (wt / "src").mkdir(parents=True)
    (wt / ".git").write_text("gitdir: ../store/wt-admin\n")
    for target in ("../store/wt-admin/config", "../store/common/config"):
        _assert_guarded(f"echo x >> {target}", wt)
    _assert_guarded(f"echo x >> {store}/common/hooks/h", wt / "src")


# --- negatives -------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "cat .git/config",
        "git status",
        "ls .git",
        "ls .git/hooks",
        "git log --oneline",
        "cat .git/config > out.txt",
        "cp .git/config backup.txt",
        "grep fsmonitor .git/config",
        "git diff",
    ],
)
def test_reads_keep_their_tier(repo: Path, command: str) -> None:
    cls = _cls(command, repo)
    assert cls.matched_rule is None or cls.matched_rule.pattern != RULE
    assert not cls.flagged
    assert not cls.path_guarded
    assert cls.command_tier == _classify_catalog(command, str(repo), AGENT).command_tier


@pytest.mark.parametrize(
    "command",
    [
        "echo x > notes.txt",
        "echo x >> notes.txt",
        "echo x > sub/notes.txt",
        "echo x > .gitignore",
        "echo x >> .gitignore",
        "echo x > .gitattributes",
        "echo x > .github/workflow.yml",
        "echo x > mygit/.gitkeep",
        "echo x > /dev/null",
        "echo x >/dev/null 2>&1",
        "echo x 2>&1",
        "ls .git > /dev/null",
    ],
)
def test_non_git_writes_unchanged(repo: Path, command: str) -> None:
    cls = _cls(command, repo)
    assert not cls.flagged
    assert not cls.path_guarded
    assert cls.matched_rule is None or cls.matched_rule.pattern != RULE


def test_notes_txt_stays_t2(repo: Path) -> None:
    assert _cls("echo x > notes.txt", repo).tier == T2


def test_gitattributes_is_not_guarded(repo: Path) -> None:
    # known limitation (#84 report): `.gitattributes` lives outside .git
    assert _cls("echo '* filter=x' > .gitattributes", repo).tier == T2


def test_dynamic_targets_fall_back_not_lower(repo: Path) -> None:
    for command in (
        "echo x > $D/config",
        "echo x > .git/c*",
        "echo x > $(echo .git)/config",
        "echo x > `echo .git`/config",
    ):
        cls = _cls(command, repo)
        was = _classify_catalog(command, str(repo), AGENT).command_tier
        assert cls.command_tier == was, command


def test_resolution_failure_is_skipped_not_raised(repo: Path) -> None:
    (cp,) = analyze("echo x > a\x00b")
    assert path_guard._git_dir_matches(cp, str(repo), "/h") == []


def test_rm_of_git_dir_is_flagged_and_never_lowered(repo: Path) -> None:
    for command in ("rm .git/hooks/pre-commit", "rm -rf .git"):
        cls = _cls(command, repo)
        assert cls.path_guarded
        assert cls.flagged
        assert cls.command_tier in (T3, Tier.ALWAYS_APPROVE, Tier.DENY)


def test_no_git_dir_anywhere_still_guards_dot_git_component(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    _assert_guarded("echo x > other/.git/config", plain)


def test_flag_not_lost_when_other_guard_also_fires(repo: Path) -> None:
    cls = _cls("echo x > /var/tmp/sr84 && echo y >> .git/config", repo)
    assert cls.command_tier == T3
    assert cls.flagged
    assert cls.matched_rule is not None and cls.matched_rule.pattern == RULE


def test_credential_deny_still_wins(repo: Path) -> None:
    cls = classify(
        "echo x > /home/t/.ssh/authorized_keys; echo y >> .git/config",
        str(repo),
        AGENT,
        env={"HOME": "/home/t"},
    )
    assert cls.command_tier == Tier.DENY


def test_realpath_vs_normpath_both_checked(repo: Path) -> None:
    # normpath alone would put this outside .git; realpath (via symlink) is inside
    (repo / "sub" / "lnk").symlink_to(repo / ".git" / "hooks")
    _assert_guarded("echo x > sub/lnk/../hooks/h", repo)
    assert os.path.normpath("sub/lnk/../hooks/h") == "sub/hooks/h"
