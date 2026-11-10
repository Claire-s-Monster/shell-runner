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

from shell_runner import executor, rules, worktree_git
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
    home = root  # worktree roots must lie strictly inside $HOME
    repo = root / "proj" / "development"
    wts = root / "proj" / "worktrees"
    reg = repo / ".git" / "worktrees" / "existing"
    for d in (home / ".ssh", reg, repo / "src", wts / "existing", root / "cfg"):
        d.mkdir(parents=True)
    (reg / "gitdir").write_text(f"{wts / 'existing' / '.git'}\n")
    monkeypatch.setattr(executor, "_CWD_ROOTS", [root])
    monkeypatch.setenv(rules.CONFIG_DIR_ENV, str(root / "cfg"))
    # isolate from the host: HOME/XDG in tmp, a nonexistent system gitconfig, no GIT_* env
    for var in [k for k in os.environ if k.startswith("GIT_CONFIG")]:
        monkeypatch.delenv(var)
    for var in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / ".config"))
    monkeypatch.setattr(worktree_git, "SYSTEM_GITCONFIG", root / "no-such-system-gitconfig")
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
    "git worktree add --detach ../worktrees/feat HEAD",
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
    priv = lay.repo / ".git" / "worktrees" / "existing"
    (priv / "commondir").write_text("../..\n")
    (lay.worktrees / "existing" / ".git").write_text(f"gitdir: {priv}\n")
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
    # -B resets an existing branch
    "git worktree add -B feat/x ../worktrees/feat",
    # nested: must be a direct child of the allowed root
    "git worktree add ../worktrees/feat/nested",
    "git worktree add .claude/worktrees/a/b",
    "git worktree remove ../worktrees/existing/sub",
    # hooks / env / global options that could change hook behaviour
    "git -c core.hooksPath=x worktree add ../worktrees/feat",
    "git -c core.hooksPath=/tmp/h worktree add ../worktrees/feat",
    "git --git-dir=/x/.git worktree add ../worktrees/feat",
    "git --git-dir=/x/.git worktree remove ../worktrees/existing",
    "GIT_DIR=/x git worktree add ../worktrees/feat",
    "GIT_COMMON_DIR=/x git worktree add ../worktrees/feat",
    "GIT_CONFIG_COUNT=1 git worktree add ../worktrees/feat",
    "GIT_CONFIG_GLOBAL=/x git worktree add ../worktrees/feat",
    "env GIT_DIR=/x git worktree add ../worktrees/feat",
    "git worktree add ../worktrees/feat $GIT_DIR",
    # remove of an unregistered path / bare suffix names
    "git worktree remove ../worktrees/unregistered",
    "git worktree remove existing",
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
# 2b. hardening: hooks, -B, remove suffix match, direct child, repo root
# ---------------------------------------------------------------------------

ADD = "git worktree add ../worktrees/feat"


def _hook(lay: Layout) -> Path:
    hooks = lay.repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    return hooks / "post-checkout"


def test_worktree_add_without_hook_or_config_is_t2(lay: Layout) -> None:
    assert lay.tier(ADD) == T2
    assert lay.tier("git worktree add -b feat/x ../worktrees/feat") == T2


def test_worktree_add_with_post_checkout_hook_stays_t3(lay: Layout) -> None:
    hook = _hook(lay)
    hook.write_text("#!/bin/sh\necho pwned\n")
    assert lay.tier(ADD) == T3  # non-executable counts as present
    hook.chmod(0o755)
    assert lay.tier(ADD) == T3
    # remove runs no post-checkout hook
    assert lay.tier("git worktree remove ../worktrees/existing") == T2


def test_worktree_add_with_dangling_symlink_hook_stays_t3(lay: Layout) -> None:
    _hook(lay).symlink_to(lay.root / "nonexistent")
    assert lay.tier(ADD) == T3


def test_worktree_add_hook_planted_through_the_runner_is_caught(lay: Layout) -> None:
    # the escalation: write the hook, chmod it, then worktree add
    assert lay.tier(ADD) == T2
    _hook(lay).write_text("x")
    assert lay.tier(ADD) == T3


@pytest.mark.parametrize(
    "where",
    [
        ".git/config",
        ".git/config.worktree",
        ".gitconfig",
        ".config/git/config",
    ],
)
@pytest.mark.parametrize(
    "content",
    [
        "[core]\n\thooksPath = /evil\n",
        "[core]\n\thookspath=/evil\n",
        "[CORE]\n\tHOOKSPATH = x\n",
        "[include]\n\tpath = /x\n",
        '[includeIf "gitdir:~/"]\n\tpath = /x\n',
        "[ include ]\n\tpath = /x\n",
    ],
)
def test_worktree_add_hookspath_or_include_in_any_config_stays_t3(
    lay: Layout, where: str, content: str
) -> None:
    base = lay.repo if where.startswith(".git/") else lay.home
    target = base / where
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    assert lay.tier(ADD) == T3
    assert lay.tier("git worktree remove ../worktrees/existing") == T2


def test_worktree_add_sample_only_hooks_dir_is_t2(lay: Layout) -> None:
    hooks = lay.repo / ".git" / "hooks"
    hooks.mkdir()
    for name in ("pre-commit.sample", "post-checkout.sample", "reference-transaction.sample"):
        (hooks / name).write_text("#!/bin/sh\n")
    assert lay.tier(ADD) == T2
    (hooks / "empty-dir-not-allowed").mkdir()
    assert lay.tier(ADD) == T3


@pytest.mark.parametrize(
    "name",
    ["reference-transaction", "post-index-change", "pre-commit", "x", "sample", "a.sample.sh"],
)
def test_worktree_add_any_non_sample_hook_stays_t3(lay: Layout, name: str) -> None:
    hooks = lay.repo / ".git" / "hooks"
    hooks.mkdir()
    (hooks / "ok.sample").write_text("x")
    (hooks / name).write_text("x")
    assert lay.tier(ADD) == T3


def test_worktree_add_sample_symlink_or_subdir_stays_t3(lay: Layout) -> None:
    hooks = lay.repo / ".git" / "hooks"
    hooks.mkdir()
    (lay.root / "real").write_text("x")
    (hooks / "evil.sample").symlink_to(lay.root / "real")  # symlink named *.sample
    assert lay.tier(ADD) == T3
    (hooks / "evil.sample").unlink()
    (hooks / "dir.sample").mkdir()  # subdirectory named *.sample
    assert lay.tier(ADD) == T3


def test_worktree_add_hooks_dir_listing_error_or_symlink_stays_t3(lay: Layout) -> None:
    (lay.repo / ".git" / "hooks").write_text("not a dir")
    assert lay.tier(ADD) == T3
    (lay.repo / ".git" / "hooks").unlink()
    (lay.root / "elsewhere").mkdir()
    (lay.repo / ".git" / "hooks").symlink_to(lay.root / "elsewhere")
    assert lay.tier(ADD) == T3


def test_worktree_add_private_hooks_dir_is_checked_too(lay: Layout) -> None:
    priv = lay.repo / ".git" / "worktrees" / "existing"
    (priv / "commondir").write_text("../..\n")
    (lay.worktrees / "existing" / ".git").write_text(f"gitdir: {priv}\n")
    wt = lay.worktrees / "existing"
    assert lay.tier("git worktree add ../sibling", cwd=wt) == T2
    (priv / "hooks").mkdir()
    (priv / "hooks" / "post-checkout.sample").write_text("x")
    assert lay.tier("git worktree add ../sibling", cwd=wt) == T2
    (priv / "hooks" / "reference-transaction").write_text("x")
    assert lay.tier("git worktree add ../sibling", cwd=wt) == T3


@pytest.mark.parametrize(
    "content",
    [
        '[filter "lfs"]\n\tsmudge = x\n',
        "[FILTER]\n\tclean = x\n",
        "[core]\n\tfsmonitor = /x\n",
        "[core]\n\tFsMonitor = true\n",
        "[core]\n\tattributesFile = /x\n",
        "[core]\n\tsshCommand = x\n",
        "[core]\n\tpager = x\n",
        "[core]\n\teditor = vim\n",
        "[core]\n\taskpass = x\n",
        '[diff "x"]\n\tcommand = /x\n',
        '[diff "x"]\n\texternal = /x\n',
        '[merge "x"]\n\tdriver = /x\n',
        "[diff]\n\tdriver = x\n",
        "[core] fsmonitor = x\n",
        "[user]\n\tname = x\n\tfilter.lfs.clean = y\n",
    ],
)
def test_worktree_add_risky_config_stays_t3(lay: Layout, content: str) -> None:
    (lay.home / ".gitconfig").write_text(content)
    assert lay.tier(ADD) == T3
    (lay.home / ".gitconfig").unlink()
    (lay.repo / ".git" / "config").write_text(content)
    assert lay.tier(ADD) == T3
    assert lay.tier("git worktree remove ../worktrees/existing") == T2


@pytest.mark.parametrize(
    "content",
    [
        "[core]\n\tbare = false\n\tfilemode = true\n",
        "# comment\n; another\n[user]\n\tname = x\n\temail = a@b\n",
        '[remote "origin"]\n\turl = git@example.com:x/y.git\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n',
        '[diff "x"]\n\tcolormoved = default\n',
        "[merge]\n\tconflictstyle = diff3\n\n",
        "",
    ],
)
def test_worktree_add_inert_config_is_t2(lay: Layout, content: str) -> None:
    (lay.repo / ".git" / "config").write_text(content)
    assert lay.tier(ADD) == T2


@pytest.mark.parametrize(
    "content",
    ["garbage line here\n", "[core\nbare = false\n", "[user]\n\tname = a \\\n\tb\n", "= x\n"],
)
def test_worktree_add_unparseable_config_stays_t3(lay: Layout, content: str) -> None:
    (lay.repo / ".git" / "config").write_text(content)
    assert lay.tier(ADD) == T3


def test_worktree_add_with_harmless_config_is_t2(lay: Layout) -> None:
    (lay.repo / ".git" / "config").write_text("[core]\n\tbare = false\n[user]\n\tname = x\n")
    (lay.home / ".gitconfig").write_text("[user]\n\temail = a@b\n")
    assert lay.tier(ADD) == T2


def test_worktree_add_xdg_config_home_is_honoured(
    lay: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = lay.root / "xdg"
    (xdg / "git").mkdir(parents=True)
    (xdg / "git" / "config").write_text("[core]\nhooksPath = x\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert lay.tier(ADD) == T3


@pytest.mark.parametrize(
    "content", ["[core]\nhooksPath = x\n", "[include]\npath = /x\n", '[includeIf "gitdir:/"]\n']
)
def test_worktree_add_system_gitconfig_stays_t3(lay: Layout, content: str) -> None:
    worktree_git.SYSTEM_GITCONFIG.write_text(content)
    assert lay.tier(ADD) == T3
    assert lay.tier("git worktree remove ../worktrees/existing") == T2


def test_worktree_add_unreadable_config_stays_t3(lay: Layout) -> None:
    (lay.repo / ".git" / "config").mkdir()  # a directory: read error, not "missing"
    assert lay.tier(ADD) == T3


@pytest.mark.parametrize("var", ["GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_CONFIG_GLOBAL"])
def test_worktree_add_git_env_in_server_environment_stays_t3(
    lay: Layout, monkeypatch: pytest.MonkeyPatch, var: str
) -> None:
    monkeypatch.setenv(var, "/x")
    assert lay.tier(ADD) == T3


def test_worktree_add_from_linked_worktree_follows_commondir(lay: Layout) -> None:
    priv = lay.repo / ".git" / "worktrees" / "existing"
    (priv / "commondir").write_text("../..\n")
    (lay.worktrees / "existing" / ".git").write_text(f"gitdir: {priv}\n")
    wt = lay.worktrees / "existing"
    assert lay.tier("git worktree add ../sibling", cwd=wt) == T2
    hooks = lay.repo / ".git" / "hooks"
    hooks.mkdir()
    (hooks / "post-checkout").write_text("x")  # hook lives in the COMMON dir
    assert lay.tier("git worktree add ../sibling", cwd=wt) == T3
    (hooks / "post-checkout").unlink()
    (priv / "config.worktree").write_text("[core]\nhooksPath = x\n")
    assert lay.tier("git worktree add ../sibling", cwd=wt) == T3


def test_worktree_linked_checkout_with_bad_gitfile_stays_t3(lay: Layout) -> None:
    (lay.worktrees / "existing" / ".git").write_text("garbage\n")
    assert lay.tier("git worktree add ../sibling", cwd=lay.worktrees / "existing") == T3
    (lay.worktrees / "existing" / ".git").write_text(f"gitdir: {lay.root}/missing\n")
    assert lay.tier("git worktree add ../sibling", cwd=lay.worktrees / "existing") == T3


def test_worktree_remove_requires_a_registered_worktree(lay: Layout) -> None:
    (lay.worktrees / "stray").mkdir()
    assert lay.tier("git worktree remove ../worktrees/stray") == T3
    assert lay.tier("git worktree remove ../worktrees/existing") == T2


def test_worktree_remove_unreadable_registration_stays_t3(lay: Layout) -> None:
    (lay.repo / ".git" / "worktrees" / "existing" / "gitdir").unlink()
    assert lay.tier("git worktree remove ../worktrees/existing") == T3
    (lay.repo / ".git" / "worktrees" / "existing" / "gitdir").write_text("\n")
    assert lay.tier("git worktree remove ../worktrees/existing") == T3


def test_worktree_remove_with_no_registrations_stays_t3(lay: Layout) -> None:
    shutil.rmtree(lay.repo / ".git" / "worktrees")
    assert lay.tier("git worktree remove ../worktrees/existing") == T3


def test_remove_suffix_ambiguity_helper(lay: Layout) -> None:
    other = lay.root / "work" / "worktrees" / "existing"
    reg = lay.repo / ".git" / "worktrees" / "other"
    reg.mkdir()
    (reg / "gitdir").write_text(f"{other / '.git'}\n")
    target = lay.worktrees / "existing"
    cands = (str(target), str(target))
    common = lay.repo / ".git"
    # git would match `worktrees/existing` as a suffix of BOTH registered worktrees
    assert not worktree_git.remove_target_unambiguous(common, "worktrees/existing", cands)
    assert not worktree_git.remove_target_unambiguous(common, "existing", cands)
    # a fully qualified argument is unambiguous
    assert worktree_git.remove_target_unambiguous(common, str(target), cands)
    assert worktree_git.remove_target_unambiguous(common, "../worktrees/existing", cands)
    # not registered at all
    assert not worktree_git.remove_target_unambiguous(common, "x", (str(lay.root / "nope"),) * 2)


def test_worktree_target_must_be_direct_child_of_root(lay: Layout) -> None:
    assert lay.tier("git worktree add ../worktrees/a") == T2
    assert lay.tier("git worktree add ../worktrees/a/b") == T3
    assert lay.tier("git worktree add .claude/worktrees/a") == T2
    assert lay.tier("git worktree add .claude/worktrees/a/b") == T3


def test_worktree_b_upper_keeps_tier_lower_b_is_t2(lay: Layout) -> None:
    assert lay.tier("git worktree add -b x ../worktrees/a") == T2
    assert lay.tier("git worktree add -B x ../worktrees/a") == T3
    assert lay.tier("git worktree add ../worktrees/a -B x") == T3


def test_worktree_repo_top_is_home_or_ancestor_stays_t3(lay: Layout) -> None:
    home_repo = lay.root / "home_is_repo"
    (home_repo / ".git").mkdir(parents=True)
    env = {"HOME": str(home_repo)}
    for cwd in (home_repo, home_repo / ".git"):
        r = classify("git worktree add ../worktrees/a", str(cwd), AGENT, env=env)
        assert PERMISSIVENESS[r.command_tier] <= PERMISSIVENESS[T3]
    # repo top is an ancestor of $HOME
    env = {"HOME": str(lay.repo / "src")}
    r = classify(ADD, str(lay.repo), AGENT, env=env)
    assert PERMISSIVENESS[r.command_tier] <= PERMISSIVENESS[T3]


def test_worktree_roots_outside_home_stay_t3(lay: Layout) -> None:
    # repo outside $HOME: its worktrees/ root is outside HOME's subtree
    r = classify(ADD, str(lay.repo), AGENT, env={"HOME": str(lay.root / "elsewhere")})
    assert PERMISSIVENESS[r.command_tier] <= PERMISSIVENESS[T3]


def test_worktree_root_equal_to_home_stays_t3(lay: Layout) -> None:
    # root <repo parent>/worktrees == $HOME
    (lay.worktrees / "existing" / ".git").mkdir()
    env = {"HOME": str(lay.worktrees)}
    r = classify("git worktree add ../x", str(lay.worktrees / "existing"), AGENT, env=env)
    assert PERMISSIVENESS[r.command_tier] <= PERMISSIVENESS[T3]


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
