"""Config/session tier changes: git worktree, mkdir, redirect labelling, out-of-scope pins.

Worktree layout under test: <project>/development (repo, holds .git) with worktrees at
<project>/worktrees/<name>, plus <repo>/.claude/worktrees/<name>.
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
    worktrees: Path  # <root>/proj/worktrees

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
    home = root / "home"
    repo = root / "proj" / "development"
    wts = root / "proj" / "worktrees"
    for d in (home / ".ssh", repo / ".git", repo / "src", wts / "existing", root / "cfg"):
        d.mkdir(parents=True)
    monkeypatch.setattr(executor, "_CWD_ROOTS", [root])
    monkeypatch.setenv(rules.CONFIG_DIR_ENV, str(root / "cfg"))
    try:
        yield Layout(root, home, repo, wts)
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1. git worktree list / prune
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git worktree list",
        "git worktree list --porcelain",
        "git worktree list -v",
        "git worktree prune",
        "git worktree prune -n",
        "git worktree prune --dry-run",
        "git worktree prune -n -v",
        "git worktree prune --dry-run --verbose",
    ],
)
def test_worktree_list_and_prune_are_t2(lay: Layout, command: str) -> None:
    assert lay.tier(command) == T2


@pytest.mark.parametrize(
    "command",
    [
        "git worktree prune --expire now",
        "git worktree prune --expire=1.day.ago",
        "git worktree prune -n --expire now",
        "git worktree list --expire now",
        "git -C /etc worktree list",
        "git worktree list ; rm -rf x",
        "git worktree prune | tee out.txt",
    ],
)
def test_worktree_list_prune_variants_stay_t3_or_higher(lay: Layout, command: str) -> None:
    assert PERMISSIVENESS[lay.tier(command)] <= PERMISSIVENESS[T3]


# ---------------------------------------------------------------------------
# 2. git worktree add / remove
# ---------------------------------------------------------------------------

ADD_OK = [
    "git worktree add ../worktrees/feat",
    "git worktree add ../worktrees/feat -b feat/x",
    "git worktree add -b feat/x ../worktrees/feat origin/development",
    "git worktree add -B feat/x ../worktrees/feat",
    "git worktree add --detach ../worktrees/feat HEAD",
    "git worktree add ../worktrees/feat/nested",
    "git worktree add ./../worktrees/./feat",
    "git worktree add .claude/worktrees/agent-1",
    "git worktree add -q --no-checkout ../worktrees/feat",
    "git worktree remove ../worktrees/existing",
]


@pytest.mark.parametrize("command", ADD_OK)
def test_worktree_add_remove_inside_allowed_root_is_t2(lay: Layout, command: str) -> None:
    result = lay.cls(command)
    assert result.command_tier == T2
    assert result.tier == T2
    assert result.matched_rule is not None
    assert result.matched_rule.pattern == "path_guard:worktree_path"
    assert result.path_guarded  # persisted template/verb approvals never apply to it
    assert any("worktree" in p for p in result.decision_path)


def test_worktree_absolute_target_inside_root_is_t2(lay: Layout) -> None:
    assert lay.tier(f"git worktree add {lay.worktrees}/abs") == T2
    assert lay.tier(f"git worktree add {lay.repo}/.claude/worktrees/abs") == T2
    assert lay.tier(f"git worktree remove {lay.worktrees}/existing") == T2


def test_worktree_from_repo_subdir_and_from_a_worktree(lay: Layout) -> None:
    assert lay.tier("git worktree add ../../worktrees/x", cwd=lay.repo / "src") == T2
    (lay.worktrees / "existing" / ".git").write_text("gitdir: x\n")
    assert lay.tier("git worktree add ../sibling", cwd=lay.worktrees / "existing") == T2
    assert lay.tier("git worktree add ../../elsewhere", cwd=lay.worktrees / "existing") == T3


def test_worktree_t2_respects_agent_cap(lay: Layout) -> None:
    capped = lay.cls("git worktree add ../worktrees/feat", agent="focused-shell-runner")
    assert capped.command_tier == T2
    assert capped.tier == T3  # agent cap APPROVE_ONCE still applies
    assert lay.cls("git worktree add ../worktrees/feat", agent="primary").tier == Tier.DENY


ADD_BAD = [
    # force / lock / other options
    "git worktree add --force ../worktrees/feat",
    "git worktree add -f ../worktrees/feat",
    "git worktree add -ff ../worktrees/feat",
    "git worktree add --for ../worktrees/feat",
    "git worktree add ../worktrees/feat --force",
    "git worktree add --lock ../worktrees/feat",
    "git worktree add --lock --reason x ../worktrees/feat",
    "git worktree add -b -f ../worktrees/feat",
    "git worktree add -- ../worktrees/feat",
    "git worktree remove --force ../worktrees/existing",
    "git worktree remove -f ../worktrees/existing",
    "git worktree remove -f -f ../worktrees/existing",
    # -C and other global options
    "git -C ../worktrees worktree add feat",
    "git -C /tmp worktree add ../worktrees/feat",
    "git --git-dir=/etc worktree add ../worktrees/feat",
    # outside allowed roots / escapes
    "git worktree add ../feat",
    "git worktree add ../worktrees",
    "git worktree add ../worktrees/",
    "git worktree add ..",
    "git worktree add .",
    "git worktree add feat",
    "git worktree add ../worktrees/../feat",
    "git worktree add ../worktrees/../../etc/x",
    "git worktree add ../worktrees/feat/../../x",
    "git worktree add ../../../../../../etc/x",
    "git worktree add .claude/worktrees/../../x",
    "git worktree add ../worktreesX/feat",
    "git worktree add /tmp/worktrees/feat",
    "git worktree add /opt/worktrees/feat",
    "git worktree remove ../feat",
    "git worktree remove ../worktrees",
    "git worktree remove ../worktrees/../development",
    "git worktree remove .",
    # system / credential / HOME
    "git worktree add /etc/worktrees/x",
    "git worktree add /etc/x",
    "git worktree add /usr/local/worktrees/x",
    "git worktree add /var/worktrees/x",
    "git worktree add /boot/x",
    "git worktree add /root/worktrees/x",
    "git worktree add /",
    "git worktree remove /etc/x",
    "git worktree add ~",
    "git worktree add ~/",
    "git worktree add ~/worktrees/x",
    "git worktree add ~/.ssh/worktrees/x",
    "git worktree add ~/.gnupg/x",
    "git worktree add ~/.aws/x",
    "git worktree add ~/.config/gh/x",
    "git worktree add $HOME",
    "git worktree add $HOME/worktrees/x",
    "git worktree add ${HOME}/x",
    # unparseable / dynamic / compound
    "git worktree add",
    "git worktree remove",
    "git worktree add 'unterminated",
    "git worktree add ../worktrees/$X",
    "git worktree add ../worktrees/`id`",
    "git worktree add ../worktrees/$(id)",
    "git worktree add ../worktrees/*",
    "git worktree add ../worktrees/{a,b}",
    "git worktree add a b c",
    "git worktree remove a b",
    "git worktree add -b",
    "git worktree add ../worktrees/feat; rm -rf x",
    "git worktree add ../worktrees/feat && ls",
    "git worktree add ../worktrees/feat | cat",
    "git worktree add ../worktrees/feat > out.txt",
    "cd .. && git worktree add worktrees/feat",
    "FOO=1 git worktree add ../worktrees/feat",
    "git worktree add ../worktrees/feat\nrm -rf x",
    "git worktree move ../worktrees/a ../worktrees/b",
    "git worktree lock ../worktrees/existing",
    "git worktree repair",
]


@pytest.mark.parametrize("command", ADD_BAD)
def test_worktree_add_remove_fail_closed(lay: Layout, command: str) -> None:
    result = lay.cls(command)
    assert PERMISSIVENESS[result.command_tier] <= PERMISSIVENESS[T3], command
    assert PERMISSIVENESS[result.tier] <= PERMISSIVENESS[T3], command


def test_worktree_add_outside_any_repo_stays_t3(lay: Layout) -> None:
    other = lay.root / "norepo"
    other.mkdir()
    assert lay.tier("git worktree add ../worktrees/x", cwd=other) == T3


def test_worktree_add_symlink_escape_stays_t3(lay: Layout) -> None:
    (lay.worktrees / "link").symlink_to(lay.home / ".ssh")
    assert lay.tier("git worktree add ../worktrees/link/x") == T3
    assert lay.tier("git worktree add ../worktrees/link") == T3
    assert lay.tier("git worktree remove ../worktrees/link") == T3


def test_worktree_add_under_credential_dir_inside_root_stays_t3(lay: Layout) -> None:
    assert lay.tier("git worktree add ../worktrees/.ssh/x") == T3
    assert lay.tier("git worktree add ../worktrees/.gnupg") == T3


def test_worktree_add_home_itself_and_cred_dirs_with_home_layout(
    lay: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    # repo sits directly in $HOME: its parent `worktrees` is below $HOME, but $HOME
    # itself and credential dirs must still fail
    home_repo = lay.home / "development"
    (home_repo / ".git").mkdir(parents=True)
    assert lay.tier("git worktree add ../worktrees/ok", cwd=home_repo) == T2
    assert lay.tier("git worktree add ..", cwd=home_repo) == T3
    assert lay.tier("git worktree add ../.ssh/x", cwd=home_repo) == T3
    assert lay.tier("git worktree add ../.aws", cwd=home_repo) == T3


def test_worktree_add_protected_path_stays_t3(lay: Layout) -> None:
    (lay.root / "cfg" / "rules.toml").write_text(
        f'[[protected]]\nglob = "{lay.worktrees}/locked/**"\nmode = "flag"\nreason = "frozen"\n'
    )
    assert lay.tier("git worktree add ../worktrees/ok") == T2
    result = lay.cls("git worktree add ../worktrees/locked/x")
    assert PERMISSIVENESS[result.command_tier] <= PERMISSIVENESS[T3]
    assert lay.tier("git worktree remove ../worktrees/locked") == T3


def test_worktree_rules_config_error_fails_closed(lay: Layout) -> None:
    (lay.root / "cfg" / "rules.toml").write_text("this is not toml = = =")
    assert lay.cls("git worktree add ../worktrees/ok").tier == Tier.DENY


def test_worktree_t2_does_not_touch_approval_templates(lay: Layout) -> None:
    # approval trap: the T2 comes from the path check, never from a template, and the
    # result is path_guarded so persisted `<cwd_path>`/`<abs_path>` approvals cannot
    # lower a failing sibling (same normalized template as an allowed one)
    ok = lay.cls("git worktree add ../worktrees/feat")
    bad = lay.cls("git worktree add ../feat")
    assert ok.template == bad.template
    assert ok.path_guarded
    assert bad.path_guarded
    assert bad.command_tier == T3


# ---------------------------------------------------------------------------
# 3. mkdir
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


# ---------------------------------------------------------------------------
# 4. redirect write labelling
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
# 5. roth_planner: not present anywhere in the repo (no rules file, fixture, README or
# seed), so no protected-path test is added -- see the task report.
# ---------------------------------------------------------------------------


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
