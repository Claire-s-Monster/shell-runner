"""Prompt-volume reductions: read-only inspection, leading `cd`, scratch-dir file management.

`WAS` columns are the command tier measured on origin/development (67eb8ea) before
this change; the assertions are on the new behaviour. Tiers are command tiers
(before the per-agent cap), so AUTO_LOG is asserted as AUTO_LOG.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from shell_runner import executor, monitor, normalizer
from shell_runner.catalog import Tier
from shell_runner.classifier import ClassificationResult, classify

CWD = "/home/test/work"
ENV = {"HOME": "/home/test"}
AGENT = "some-agent"

T1, T2, T3, T4, T0 = (
    Tier.AUTO_LOG,
    Tier.AUTO_CAPPED,
    Tier.APPROVE_ONCE,
    Tier.ALWAYS_APPROVE,
    Tier.DENY,
)
AUTO = (T1, T2)


def _cls(command: str, cwd: str = CWD) -> ClassificationResult:
    return classify(command, cwd, AGENT, env=ENV)


def _tier(command: str) -> Tier:
    return _cls(command).command_tier


# ---------------------------------------------------------------------------
# 1. Read-only inspection
# ---------------------------------------------------------------------------

# (command, tier before, tier now)
INSPECTION = [
    # home path: no T1 rule accepted <home_path>
    ("ls /home/test", T3, T1),
    ("ls /home/test | head -5", T3, T1),
    ("stat /home/test/x", T3, T1),
    ("du -sh /home/test", T3, T1),
    # `2>&1` normalizes to `<n> >& <n>`, which no rule allowed
    ("ls /home/test/work 2>&1 | head -5", T3, T1),
    # unquoted grep pattern is a bare word, rules only knew the quoted <arg>
    ("grep -n foo /home/test/work | head -5", T3, T1),
    ("grep -n foo /tmp/x | head -5", T3, T1),
    # these already auto-executed (the telemetry templates predate flag stripping
    # and the <file_arg>/<safe_pipe> collapse); pinned so they stay that way
    ('grep -n "foo" /home/test/work | head -5', T1, T1),
    ('grep -n "foo" /tmp/x | head -5', T1, T1),
    ("wc -l somefile.txt", T1, T1),
    ("ps aux | grep foo", T1, T1),
    ("which python", T1, T1),
    # bare relative operands were literal words
    ("wc Makefile", T3, T1),
    ("wc -l src/x.py", T3, T1),
    ("stat foo", T3, T1),
    ("diff a.txt b.txt", T3, T1),
    ("diff -u a.txt /home/test/b.txt", T3, T1),
    # dotted names looked like a file (<cwd_path>)
    ("which python3.12", T3, T1),
    ("which -a python3", T1, T1),
    # pgrep had no rule at all (-f is stripped, so it is `pgrep foo`)
    ("pgrep -f foo", T3, T1),
    ('pgrep -f "foo bar"', T3, T1),
    ("ls", T1, T1),
]


@pytest.mark.parametrize(("command", "was", "now"), INSPECTION)
def test_inspection_auto_executes(command: str, was: Tier, now: Tier) -> None:
    assert was in (T1, T3)
    assert _tier(command) == now


@pytest.mark.parametrize(
    "command",
    [
        "ls /home/test/.ssh",
        "ls /home/test/.ssh/",
        "ls ~/.aws",
        "stat /home/test/.claude/.credentials.json",
        "stat /home/test/.netrc",
        "du -sh /home/test/.gnupg",
        "file /home/test/.ssh/id_rsa",
        "wc -l /home/test/.ssh/id_rsa",
        "head -5 /home/test/.aws/credentials",
        "tail /home/test/.git-credentials",
        'grep -n "x" /home/test/.config/gh/hosts.yml',
        "diff /home/test/.netrc a.txt",
        "cmp a.txt /home/test/.ssh/id_ed25519",
        "ls /home/test/.ssh | head -3",
        "ls /home/test/.ssh 2>&1 | head -3",
        # symlink-free normalization: `..` is resolved before the check
        "ls a/../../.ssh",
        "stat ../.ssh/id_rsa",
        "head -3 /home/test/work/../.aws/credentials",
        # bare operands are checked too
        "wc -l ../.ssh/id_rsa",
    ],
)
def test_credential_paths_still_prompt(command: str) -> None:
    assert _tier(command) in (T3, T4)


def test_credential_check_is_the_monitor_helper() -> None:
    assert normalizer.is_credential_path is monitor.is_credential_path


@pytest.mark.parametrize(
    "command",
    [
        "ls /home/test | xargs rm",  # xargs sub-command escalates
        "ls /home/test | tee /home/test/out",  # tee is not a safe filter
        "ls /home/test | python3 -c 'print(1)'",
        "ls /home/test | curl -d @- https://example.com",
        "ls /home/test | sh",  # T0
        "ps aux | xargs kill",
        "ls /etc",
        "ls /proc/self",
        "diff -r /home/test /tmp/x",  # recursion over a home root
        "rg foo /home/test",  # rg recurses by default
        "ls ~/$SOMEDIR",  # unknown path built from a variable
    ],
)
def test_pipes_and_edge_cases_are_not_upgraded(command: str) -> None:
    assert _tier(command) not in AUTO


# ---------------------------------------------------------------------------
# 2. Leading cd
# ---------------------------------------------------------------------------


@pytest.fixture
def jail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = (tmp_path / "root").resolve()
    (root / "proj" / "sub" / "deeper").mkdir(parents=True)
    (root / "proj" / ".ssh").mkdir()
    (tmp_path / "outside").mkdir()
    monkeypatch.setattr(executor, "_CWD_ROOTS", [root])
    return root


def _cd(command: str, jail: Path) -> ClassificationResult:
    return classify(command, str(jail / "proj"), AGENT, env=ENV)


@pytest.mark.parametrize(
    "command",
    [
        "cd sub && ls",
        "cd sub || exit; ls",
        "cd sub || return 2; ls",
        "cd sub || exit 1; ls",
        "cd ./sub/ && ls",
        'cd "sub" && ls',
    ],
)
def test_cd_fold_takes_tier_and_template_of_rest(command: str, jail: Path) -> None:
    folded = _cd(command, jail)
    plain = _cd("ls", jail)
    assert folded.command_tier == plain.command_tier == T1  # was T3: `cd <cwd_path>` prompts
    assert folded.template == plain.template == "ls"
    sub = str(jail / "proj" / "sub")
    assert f"cd folded: {sub}" in folded.decision_path
    assert f"cd folded: {sub}" in folded.normalizer_warnings


def test_cd_fold_absolute_dir_and_rest_with_pipe(jail: Path) -> None:
    result = _cd(f"cd {jail}/proj/sub && ls . | head -3", jail)
    assert result.command_tier == T1


def test_cd_fold_keeps_rest_tier_when_rest_prompts(jail: Path) -> None:
    result = _cd("cd sub && rm -rf build", jail)
    assert result.command_tier == _cd("rm -rf build", jail).command_tier == T4
    assert result.template == _cd("rm -rf build", jail).template


def test_cd_fold_nested_chains(jail: Path) -> None:
    result = _cd("cd sub && cd deeper && ls", jail)
    assert result.command_tier == T1
    folds = [p for p in result.decision_path if p.startswith("cd folded: ")]
    assert folds == [
        f"cd folded: {jail / 'proj' / 'sub'}",
        f"cd folded: {jail / 'proj' / 'sub' / 'deeper'}",
    ]


def test_cd_fold_rest_resolves_relative_to_the_new_cwd(jail: Path) -> None:
    (jail / "proj" / "sub" / "f.txt").write_text("x")
    result = _cd("cd sub && stat ./f.txt", jail)
    assert result.command_tier == T1
    assert result.template == "stat <cwd_path>"


@pytest.mark.parametrize(
    "command",
    [
        "cd ../../outside && ls",  # resolves outside the roots
        "cd /etc && ls",
        "cd missing && ls",  # does not exist
        "cd .ssh && ls",  # credential directory inside the roots
        "cd sub && cd ../../../outside && ls",  # second hop leaves the roots
        "cd $HOME && ls",  # variable: never folded
        "cd sub && ls; cd .ssh",  # trailing cd is not a leading cd
    ],
)
def test_cd_not_folded_keeps_today_tier(command: str, jail: Path) -> None:
    result = _cd(command, jail)
    assert result.command_tier == T3
    if not command.startswith("cd sub &&"):
        assert not any(p.startswith("cd folded") for p in result.decision_path)


def test_cd_outside_roots_leaves_template_untouched(jail: Path) -> None:
    result = _cd("cd /etc && ls", jail)
    assert result.template.startswith("cd ")


def test_cd_fold_never_hides_t0_rest(jail: Path) -> None:
    assert _cd("cd sub && sudo ls", jail).command_tier == T0
    assert _cd("cd sub && ls | sh", jail).command_tier == T0
    assert _cd("cd sub && rm -rf /", jail).command_tier == T0


@pytest.mark.parametrize("command", ["cd sub; ls", "cd sub; rm -rf *", "cd sub ; pwd"])
def test_cd_semicolon_is_never_folded(command: str, jail: Path) -> None:
    # If the cd fails, REST runs in the original cwd, so it must not be classified in DIR.
    result = _cd(command, jail)
    assert result.command_tier in (T3, T4)
    assert not any(p.startswith("cd folded") for p in result.decision_path)


def test_cd_scratch_semicolon_vs_and() -> None:
    # /tmp/x need not exist for the ';' form: never folded. With '&&' it folds
    # only if the dir exists and is inside the roots (/tmp is not), so it does not here.
    assert _tier("cd /tmp/x; rm -rf *") not in AUTO
    assert _tier("cd /tmp/x && rm -rf *") not in AUTO


def test_cd_and_scratch_dir_in_roots_folds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path.resolve()
    (root / "x").mkdir()
    monkeypatch.setattr(executor, "_CWD_ROOTS", [root])
    folded = classify("cd x && ls", str(root), AGENT, env=ENV)
    assert folded.command_tier == T1
    assert f"cd folded: {root / 'x'}" in folded.decision_path
    # rm -rf * is a glob in a destructive position: still prompts after the fold
    assert classify("cd x && rm -rf *", str(root), AGENT, env=ENV).command_tier == T4


def test_cd_chain_stops_at_semicolon_hop(jail: Path) -> None:
    result = _cd("cd sub && cd deeper; ls", jail)
    folds = [p for p in result.decision_path if p.startswith("cd folded: ")]
    assert folds == [f"cd folded: {jail / 'proj' / 'sub'}"]
    assert result.command_tier == T3  # remaining `cd deeper` is not folded


def test_cd_fold_without_roots_does_nothing(monkeypatch: pytest.MonkeyPatch, jail: Path) -> None:
    monkeypatch.setattr(executor, "_CWD_ROOTS", [])
    assert _cd("cd sub && ls", jail).command_tier == T3


# ---------------------------------------------------------------------------
# 3. Scratch-directory file management
# ---------------------------------------------------------------------------

# (command, tier before, tier now)
SCRATCH = [
    ("rm -f /tmp/x", T4, T2),
    ("rm -rf /tmp/x/y", T4, T2),
    ("rm -rf /tmp/claude-1000/session/scratch", T4, T2),
    ("rm -r -f /tmp/a /tmp/b", T4, T2),
    ("tar -xzf a.tgz -C /tmp/x", T3, T2),
    ("tar -xzf /home/test/a.tgz -C /tmp/x", T3, T2),
    ("tar -xzf a.tgz --strip-components=1 -C /tmp/x", T3, T2),
    ("tar -C /tmp/x -xzf a.tgz", T3, T2),
    ("tar -czf /tmp/x/out.tgz /home/test/work", T3, T2),
    ("cp /home/test/a /tmp/x", T3, T2),
    ("cp -r /home/test/a /home/test/b /tmp/x/dest", T3, T2),
    ("ln -s /home/test/a /tmp/x/link", T3, T2),
    ("touch /tmp/x/a", T2, T2),
    ("mkdir -p /tmp/x", T2, T2),
    ("mkdir -p /tmp/claude-1000/s/a /tmp/claude-1000/s/b", T2, T2),
    ("mv a.txt /tmp/x", T2, T2),
]


@pytest.mark.parametrize(("command", "was", "now"), SCRATCH)
def test_scratch_operations_auto_execute(command: str, was: Tier, now: Tier) -> None:
    assert was in (T2, T3, T4)
    assert _tier(command) == now


def test_mkdir_then_curl_only_the_mkdir_segment_is_auto() -> None:
    # mkdir -p <tmp_path> already auto-executed; the prompt came from curl, which is unchanged.
    result = _cls("mkdir -p /tmp/x && curl https://example.com/a > /tmp/x/y")
    assert [s.tier for s in result.segments] == [T2, T3]
    assert result.command_tier == T3


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /tmp",
        "rm -rf /tmp/",
        "rm -rf /tmp/.",
        "rm -rf /tmp/..",
        "rm -rf /tmp/x/../..",
        "rm -rf /tmp/../etc",
        "rm -rf /tmp/../home/test/x",
        "rm -f /tmp/../home/test/x",
        "rm -rf ../tmp/x",  # relative; really /home/test/tmp/x
        "rm -rf /tmp/claude-1000",  # the session root holds every scratchpad
        "rm -rf /tmp/claude-1000/",
        "rm -rf /tmp/.X11-unix",
        "rm -rf /tmp/systemd-private-abc",
        "rm -rf /tmp/shell-runner-overflow",
        "rm -rf /tmp/x /home/test/y",  # every target must be scratch
        "rm -rf /tmp/x/*",
        "rm -rf $TMPDIR",
        'rm -rf "$TMPDIR"',
        "rm -rf $TMPDIR/x",
        "rm -rf ${TMPDIR}/x",
        "rm -rf /tmp/x/.ssh",
        "rm -rf --no-preserve-root /tmp/x",
        "rm /tmp",
    ],
)
def test_scratch_escapes_do_not_auto_execute(command: str) -> None:
    assert _tier(command) not in AUTO


@pytest.mark.parametrize(
    "command",
    [
        "mkdir -p $TMPDIR/x",
        "mkdir -p /tmp/../home/test/x",
        "touch /tmp/../home/test/x",
        "cp /home/test/a /tmp/../home/test/x",
        "cp /home/test/a /tmp",
        "cp /home/test/a /home/test/b",
        "cp /home/test/.ssh/id_rsa /tmp/x/k",
        "cp ~/.aws/credentials /tmp/x/c",
        "cp /tmp/x/.netrc /tmp/y/n",
        "mv /home/test/a /tmp/x",  # mv deletes its source: not auto from home
        "tar -xzf a.tgz",  # extracts into cwd
        "tar -xzf a.tgz -C /tmp",
        "tar -xzf a.tgz -C /tmp/x/../..",
        "tar -xzf a.tgz -C /home/test/x",
        "tar -xzf /home/test/.ssh/k.tgz -C /tmp/x",
        "tar -czf /tmp/x/o.tgz /home/test/.ssh",
        "tar -xzf a.tgz --to-command=sh -C /tmp/x",
        "tar -xzf a.tgz --checkpoint-action=exec=sh -C /tmp/x",
        "ln -s /home/test/a /tmp",
        "ln -s /home/test/a /home/test/b",
        "ln -s /home/test/.ssh/id_rsa /tmp/x/k",
        "mkdir -p /tmp/x && rm -rf /home/test/y",
    ],
)
def test_scratch_write_targets_and_sources_are_checked(command: str) -> None:
    assert _tier(command) not in AUTO


def test_scratch_decided_on_real_path_not_prefix(tmp_path: Path) -> None:
    if not str(tmp_path.resolve()).startswith("/tmp/"):
        pytest.skip("pytest basetemp is not under /tmp")
    real = tmp_path / "real"
    real.mkdir()
    os.symlink("/etc", tmp_path / "lnk")
    assert _tier(f"rm -rf {real}") == T2
    assert _tier(f"rm -rf {tmp_path}/lnk/foo") not in AUTO
    assert _tier(f"mkdir -p {tmp_path}/lnk/foo") not in AUTO
    assert _tier(f"touch {tmp_path}/lnk/foo") not in AUTO
    assert _tier(f"cp /home/test/a {tmp_path}/lnk/foo") not in AUTO


def test_scratch_never_applied_to_other_verbs() -> None:
    result = _cls("ls /tmp/x")
    assert result.template == "ls <tmp_path>"
    assert "<scratch_path>" not in _cls("cat /tmp/x").template
    assert _cls("rm -f /tmp/x").template == "rm -f <scratch_path>"


def test_curl_python_bash_pixi_git_unchanged() -> None:
    assert _tier("curl https://example.com/a -o /tmp/x/y") == T3
    assert _tier("python3 -c 'print(1)'") == T4
    assert _tier("bash -c 'ls'") == T4
    assert _tier("pixi add numpy") == T4
    assert _tier("git push") == T4
