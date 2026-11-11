"""Config/session tier changes: mkdir, redirect labelling, out-of-scope pins.

Layout under test: <root>/proj/development is the project cwd, with $HOME = <root>.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from shell_runner import executor, rules
from shell_runner.catalog import Tier
from shell_runner.classifier import PERMISSIVENESS, ClassificationResult, classify

AGENT = "some-agent"
T1, T2, T3 = Tier.AUTO_LOG, Tier.AUTO_CAPPED, Tier.APPROVE_ONCE


@dataclass
class Layout:
    root: Path
    home: Path
    repo: Path  # <root>/proj/development

    @property
    def env(self) -> dict[str, str]:
        return {"HOME": str(self.home)}

    def cls(
        self, command: str, cwd: Path | None = None, agent: str = AGENT
    ) -> ClassificationResult:
        return classify(command, str(cwd or self.repo), agent, env=self.env)

    def tier(self, command: str, cwd: Path | None = None) -> Tier:
        return self.cls(command, cwd).command_tier


@pytest.fixture
def lay(monkeypatch: pytest.MonkeyPatch) -> Iterator[Layout]:
    # Not under /tmp: the classifier treats /tmp specially (scratch / safe area).
    cache = Path.home() / ".cache"
    cache.mkdir(exist_ok=True)
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="sr-config-tiers-", dir=cache)))
    home = root
    repo = root / "proj" / "development"
    for d in (home / ".ssh", repo / "src", root / "cfg"):
        d.mkdir(parents=True)
    monkeypatch.setattr(executor, "_CWD_ROOTS", [root])
    monkeypatch.setenv(rules.CONFIG_DIR_ENV, str(root / "cfg"))
    monkeypatch.setenv("HOME", str(home))
    try:
        yield Layout(root, home, repo)
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# git worktree is a git-MCP concern: never auto-approved, plain T3 fall-through
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git worktree list",
        "git worktree prune",
        "git worktree add ../worktrees/feat",
        "git worktree remove ../worktrees/existing",
    ],
)
def test_git_worktree_is_t3(lay: Layout, command: str) -> None:
    assert lay.tier(command) == T3


# ---------------------------------------------------------------------------
# mkdir
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "mkdir newdir",
        "mkdir -p newdir",
        "mkdir -p a/b/c",
        "mkdir -p ./a ./b",
        "mkdir -p src/new",
    ],
)
def test_mkdir_inside_cwd_is_t2(lay: Layout, command: str) -> None:
    # already T2 on origin/development (catalog fs-write rule); pinned here
    result = lay.cls(command)
    assert result.command_tier == T2
    assert result.matched_rule is not None
    assert result.matched_rule.category == "fs-write"


@pytest.mark.parametrize(
    "command",
    [
        "mkdir -p ~/.ssh/x",
        "mkdir -p ~/.gnupg",
        "mkdir -p ~/.aws/x",
        "mkdir -p ~",
        "mkdir -p $HOME/x",
        "mkdir -p /etc/x",
        "mkdir -p /usr/local/x",
        "mkdir -p /var/x",
        "mkdir -p /boot/x",
        "mkdir -p /root/x",
        "mkdir -p /home/other/x",
        "mkdir -p newdir /etc/x",
        "mkdir -p newdir $X",
        "mkdir -p newdir; rm -rf x",
        # `..` escapes: the normalizer calls these <cwd_path>; the guard resolves them
        "mkdir -p ../x",
        "mkdir ../x",
        "mkdir -p ../../x",
        "mkdir -p a/../../x",
        "mkdir -p src/../../x",
        "mkdir -p newdir ../x",
        "mkdir -p ${HOME}/x",
        "mkdir -p x$Y",
        "mkdir -p $(pwd)/x",
        "mkdir -p ./$X/y",
        "mkdir -p $X/y",
        "mkdir -p {a,b}",
        f"mkdir -p {Path('/home/memento')}/x",
    ],
)
def test_mkdir_outside_cwd_stays_t3_or_higher(lay: Layout, command: str) -> None:
    result = lay.cls(command)
    assert PERMISSIVENESS[result.command_tier] <= PERMISSIVENESS[T3], result.decision_path


def test_mkdir_symlink_escape_is_not_t2(lay: Layout) -> None:
    (lay.repo / "link").symlink_to(lay.home / ".ssh")
    assert lay.tier("mkdir -p link/x") == T3
    assert lay.tier("mkdir -p ./link/x") == T3


def test_mkdir_tmp_scratch_still_t2(lay: Layout) -> None:
    assert lay.tier("mkdir -p /tmp/x") == T2
    assert lay.tier("mkdir -p /tmp/claude-1000/s/a") == T2


def test_mkdir_protected_path_is_not_t2(lay: Layout) -> None:
    (lay.root / "cfg" / "rules.toml").write_text(
        f'[[protected]]\nglob = "{lay.repo}/frozen/**"\nmode = "flag"\nreason = "r"\n'
    )
    assert lay.tier("mkdir -p fine") == T2
    assert lay.tier("mkdir -p frozen/sub") == T3
    assert lay.tier("mkdir frozen") == T3
    assert lay.tier("mkdir -p ./frozen/sub") == T3


@pytest.mark.parametrize("command", ["mkdir x", "mkdir -p x/y", "mkdir -p ./x"])
def test_mkdir_in_home_itself_is_not_t2(lay: Layout, command: str) -> None:
    assert lay.tier(command, cwd=lay.home) == T3


@pytest.mark.parametrize("cwd", ["/etc", "/usr/local", "/var", "/root"])
def test_mkdir_in_system_cwd_is_not_t2(lay: Layout, cwd: str) -> None:
    assert PERMISSIVENESS[lay.cls("mkdir -p x", cwd=Path(cwd)).command_tier] <= PERMISSIVENESS[T3]


def test_mkdir_in_protected_cwd_is_not_t2(lay: Layout) -> None:
    (lay.root / "cfg" / "rules.toml").write_text(
        f'[[protected]]\nglob = "{lay.repo}/src/**"\nmode = "flag"\nreason = "r"\n'
    )
    assert lay.tier("mkdir -p x", cwd=lay.repo / "src") == T3
    assert lay.tier("mkdir -p x") == T2


def test_mkdir_in_project_subdir_of_home_stays_t2(lay: Layout) -> None:
    assert lay.tier("mkdir -p x/y", cwd=lay.repo / "src") == T2


# ---------------------------------------------------------------------------
# redirect write labelling
# ---------------------------------------------------------------------------


def _labels(result: ClassificationResult) -> list[str]:
    return [p for p in result.decision_path if p.startswith("fs-write")]


@pytest.mark.parametrize(
    ("command", "target"),
    [
        ("echo x > notes.txt", "notes.txt"),
        ("echo x >> notes.txt", "notes.txt"),
        ("echo x 2> err.txt", "err.txt"),
        ("echo x &> all.txt", "all.txt"),
        ("echo x &>> all.txt", "all.txt"),
        ("echo x >notes.txt", "notes.txt"),
        ("echo x | tee out.txt", "out.txt"),
        ("echo x | tee -a out.txt", "out.txt"),
    ],
)
def test_redirect_and_tee_writes_are_labelled_fs_write(
    lay: Layout, command: str, target: str
) -> None:
    result = lay.cls(command)
    assert _labels(result) == [f"fs-write: {'tee' if 'tee' in command else 'redirect'} {target}"]


@pytest.mark.parametrize(
    "command",
    [
        "echo x > /dev/null",
        "echo x >/dev/null 2>&1",
        "echo x 2>&1",
        "echo x >&2",
        "echo x 2>/dev/null",
        "echo x | tee /dev/null",
        "ls",
        "cat notes.txt",
    ],
)
def test_non_writes_are_not_labelled(lay: Layout, command: str) -> None:
    assert _labels(lay.cls(command)) == []


def test_labelling_does_not_change_in_cwd_redirect_tier(lay: Layout) -> None:
    result = lay.cls("echo x > notes.txt")
    assert result.command_tier == T1
    assert lay.tier("echo x >> notes.txt") == T1
    assert lay.tier("echo x > ../outside.txt") == T3  # unchanged: outside cwd
    assert lay.tier(f"echo x > {lay.home}/notes.txt") == T3


def test_multiple_redirects_each_labelled(lay: Layout) -> None:
    result = lay.cls("echo x > a.txt 2> b.txt")
    assert _labels(result) == ["fs-write: redirect a.txt", "fs-write: redirect b.txt"]


def _protect(lay: Layout, glob: str, mode: str) -> None:
    (lay.root / "cfg" / "rules.toml").write_text(
        f'[[protected]]\nglob = "{glob}"\nmode = "{mode}"\nreason = "r"\n'
    )


@pytest.mark.parametrize(
    "redirect", ["echo x > {p}", "echo x >> {p}", "echo x 2> {p}", "echo x &> {p}"]
)
def test_redirect_to_flag_mode_protected_path_is_flagged(lay: Layout, redirect: str) -> None:
    _protect(lay, f"{lay.repo}/protected/**", "flag")
    result = lay.cls(redirect.format(p="protected/f.txt"))
    assert result.flagged
    assert result.protected_path_json is not None
    assert result.protected_matches[0].kind == "write"
    assert _labels(result)  # still labelled as a write


def test_redirect_to_deny_mode_protected_path_is_denied_and_flagged(lay: Layout) -> None:
    _protect(lay, f"{lay.repo}/protected/**", "deny")
    result = lay.cls("echo x > protected/f.txt")
    assert result.tier == Tier.DENY
    assert result.flagged
    assert result.protected_path_json is not None


def test_redirect_outside_protected_path_not_flagged(lay: Layout) -> None:
    _protect(lay, f"{lay.repo}/protected/**", "flag")
    result = lay.cls("echo x > other.txt")
    assert not result.flagged
    assert result.protected_path_json is None


# ---------------------------------------------------------------------------
# Out of scope: stay at T3
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "python3 ~/.claude/hooks/pre_tool_use.py",
        "python3 /home/test/.claude/hooks/shell_runner_intercept.py",
        "python ~/.claude/skills/foo/scripts/run.py",
        "python3 ~/.claude/skills/foo/scripts/run.py --arg x",
        "rattler-build build --recipe recipe.yaml",
        "rattler-build build",
        "make",
        "make build",
        "make -C src install",
    ],
)
def test_out_of_scope_commands_stay_t3_or_higher(lay: Layout, command: str) -> None:
    result = lay.cls(command)
    assert PERMISSIVENESS[result.command_tier] <= PERMISSIVENESS[T3]
    assert not result.flagged


def test_no_protected_rules_added_for_claude_hooks(lay: Layout) -> None:
    # No protected-path rule ships for ~/.claude/hooks: an empty config protects nothing.
    result = lay.cls(f"echo x > {lay.home}/.claude/hooks/h.py")
    assert result.protected_matches == ()
    assert not result.flagged
