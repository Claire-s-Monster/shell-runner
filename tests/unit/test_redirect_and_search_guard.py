"""Issue #75: output redirects and recursive grep must not ride a T1 rule.

`WAS` is the command tier on origin/development before this change.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shell_runner import executor, path_guard
from shell_runner.catalog import Tier
from shell_runner.classifier import classify

CWD = "/home/test/work"
ENV = {"HOME": "/home/test"}
AGENT = "some-agent"
T1, T3, T0 = Tier.AUTO_LOG, Tier.APPROVE_ONCE, Tier.DENY
RESTRICTED = (T3, Tier.ALWAYS_APPROVE, T0)


@pytest.fixture(autouse=True)
def _no_extra_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor, "_CWD_ROOTS", [Path("/srv/jail-root")])


def _tier(command: str) -> Tier:
    return classify(command, CWD, AGENT, env=ENV).command_tier


# (command, WAS, now)
REDIRECT_DENY = [
    ("echo x > /home/test/.ssh/authorized_keys", T1, T0),
    ("echo x >> /home/test/.ssh/authorized_keys", T1, T0),
    ("echo x >| /home/test/.ssh/authorized_keys", T1, T0),
    ("echo x &> /home/test/.aws/credentials", T1, T0),
    ("echo x 2> /home/test/.ssh/id_rsa", T1, T0),
    ("echo x > ~/.ssh/authorized_keys", T1, T0),
    ("echo x > ../.ssh/authorized_keys", T1, T0),
    ("printf x > /home/test/.ssh/authorized_keys", T3, T0),
    ("ls > /home/test/.netrc", T1, T0),
    ("cd /home/test && echo x > .ssh/authorized_keys", T3, T0),
]


@pytest.mark.parametrize(("command", "was", "now"), REDIRECT_DENY)
def test_redirect_to_credential_path_is_denied(command: str, was: Tier, now: Tier) -> None:
    assert was in (T1, T3)
    assert _tier(command) == now


REDIRECT_OUTSIDE = [
    "echo x >> /home/test/.bashrc",
    "echo x > /home/test/notes.txt",
    "echo x > ../other.txt",
    "echo x > /var/tmp/foo",
    "echo x &>> /home/test/out.log",
    "ls > /home/test/out.txt",
    "echo x > /home/test/work/../out.txt",
]


@pytest.mark.parametrize("command", REDIRECT_OUTSIDE)
def test_redirect_outside_cwd_needs_approval(command: str) -> None:
    assert _tier(command) in RESTRICTED


@pytest.mark.parametrize(
    "command",
    [
        "echo x > notes.txt",
        "echo x >> notes.txt",
        "echo x > ./sub/notes.txt",
        "echo x > /home/test/work/notes.txt",
        "echo x > /tmp/foo.txt",
        "echo x > /tmp/claude-1000/scratch/a",
        "echo x > /dev/null",
        "echo x >/dev/null 2>&1",
        "echo x 2>&1",
        "echo x >&2",
        "echo x > $OUT",
    ],
)
def test_redirect_into_cwd_tmp_devnull_keeps_tier(command: str) -> None:
    assert _tier(command) == T1


def test_guard_never_loosens() -> None:
    assert _tier("sudo echo x > notes.txt") == T0


# ---------------------------------------------------------------------------
# grep / rg / ag
# ---------------------------------------------------------------------------

GREP_BROAD = [
    ("grep -r password /home/test", T1, T3),
    ("grep -rn password /home/test", T1, T3),
    ("grep -R password /home/test", T1, T3),
    ("grep --recursive password /home/test", T1, T3),
    ("grep -r password ~", T1, T3),
    ("grep -r password /", T1, T3),
    ("grep -r password /home", T1, T3),
    ("grep -r password ..", T1, T3),
    ("cd /home/test && grep -r password .", T3, T3),
    ("grep -r password /home/test/.ssh", T3, T3),
    ("grep password /home/test/.ssh/id_rsa", T3, T3),
    ("grep -n password ~/.aws/credentials | head -3", T3, T3),
]


@pytest.mark.parametrize(("command", "was", "now"), GREP_BROAD)
def test_broad_or_credential_grep_needs_approval(command: str, was: Tier, now: Tier) -> None:
    assert was in (T1, T3)
    assert _tier(command) == now


@pytest.mark.parametrize(
    "command",
    [
        "rg password /home/test/.ssh",
        "ag password /home/test/.ssh",
        "rg password /home/test",
        "rg password /",
    ],
)
def test_rg_ag_credential_or_broad_needs_approval(command: str) -> None:
    assert _tier(command) in RESTRICTED


@pytest.mark.parametrize(
    "command",
    [
        "grep -rn foo src",
        "grep -r foo ./src",
        "grep -rn foo /home/test/work",
        "grep -rn foo /home/test/work/src",
        "grep -n foo file.txt",
        "grep foo file.txt",
        "grep -n foo /home/test/work | head -5",
        "grep -n foo /tmp/x | head -5",
        "grep -r foo /tmp/x",
        "ps aux | grep foo",
    ],
)
def test_grep_inside_cwd_or_tmp_unchanged(command: str) -> None:
    assert path_guard.evaluate(command, CWD, ENV) is None


@pytest.mark.parametrize(
    ("command", "tier"),
    [
        ("grep -n foo /home/test/work | head -5", T1),
        ("grep -n foo /tmp/x | head -5", T1),
        ("ps aux | grep foo", T1),
        ("grep -rn foo src", T3),  # pre-existing: bare relative dir is not auto-executed
    ],
)
def test_grep_prompt_volume_pinned(command: str, tier: Tier) -> None:
    assert _tier(command) == tier


def test_guard_decision_is_recorded() -> None:
    res = classify("echo x > /home/test/.ssh/authorized_keys", CWD, AGENT, env=ENV)
    assert res.matched_rule is not None
    assert res.matched_rule.category == "redirect_credential"
    assert any("path_guard" in d for d in res.decision_path)


def test_directory_holding_credentials_is_broad(tmp_path: Path) -> None:
    (tmp_path / ".ssh").mkdir()
    assert path_guard._holds_credentials(tmp_path)
    (tmp_path / "plain").mkdir()
    assert not path_guard._holds_credentials(tmp_path / "plain")
