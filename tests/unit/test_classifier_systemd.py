"""Classifier tests for systemd user-scope rules (issue #56 part 3)."""

import pytest
from shell_runner.catalog import Tier
from shell_runner.classifier import classify

CWD = "/home/test/work"
ENV = {"HOME": "/home/test"}
AGENT = "focused-ghc-ci-analyzer"


def _tier(raw: str) -> Tier:
    return classify(raw, CWD, AGENT, env=ENV).command_tier


@pytest.mark.parametrize(
    "raw",
    [
        "systemctl --user status foo",
        "systemctl --user status foo.service",
        "systemctl --user --no-pager status foo",
        "systemctl --user is-active foo",
        "systemctl --user is-enabled foo",
        "systemctl --user is-failed foo",
        "systemctl --user show foo -p ActiveState",
        "systemctl --user cat foo",
        "systemctl --user list-units --type=service",
        "systemctl --user list-unit-files",
        "systemctl --user list-timers --all",
        "journalctl --user -u foo",
        "journalctl --user -u foo.service -n 50 --no-pager",
        "journalctl --user --unit=foo --since today",
        "journalctl --user -u foo --since '1 hour ago'",
        "journalctl --user",
    ],
)
def test_systemd_user_reads_are_t1(raw):
    assert _tier(raw) == Tier.AUTO_LOG


@pytest.mark.parametrize(
    "raw",
    [
        "systemctl --user start foo",
        "systemctl --user stop foo",
        "systemctl --user restart foo",
        "systemctl --user reload foo",
        "systemctl --user enable foo",
        "systemctl --user disable foo",
        "systemctl --user daemon-reload",
    ],
)
def test_systemd_user_mutations_are_t3(raw):
    assert _tier(raw) == Tier.APPROVE_ONCE


@pytest.mark.parametrize(
    "raw",
    [
        "journalctl --user -f",
        "journalctl --user -fu foo",
        "journalctl --user -u foo --follow",
        "journalctl --user --vacuum-size=1M",
        "journalctl --user --vacuum-time=1d",
        "journalctl --user --rotate",
        "journalctl --user --flush",
        "journalctl -u foo",  # system scope: not auto
        "systemctl status foo",  # system scope: not auto
        "systemctl --user status foo -H root@host",
    ],
)
def test_systemd_unsafe_reads_not_auto(raw):
    assert _tier(raw) >= Tier.APPROVE_ONCE


@pytest.mark.parametrize(
    "raw",
    [
        "systemctl --user status x; rm -rf ~",
        "systemctl --user status x && touch ./a",
        "systemctl --user status x | sh",
        "systemctl --user status $(rm ./a)",
        "systemctl --user status `rm ./a`",
        "systemctl --user status x > ./out",
        "systemctl --user status x >./out",
        "journalctl --user -u x; rm -rf ~",
        "journalctl --user -u $(id)",
        "journalctl --user -u x > ./out",
    ],
)
def test_systemd_chaining_not_t1(raw):
    r = classify(raw, CWD, AGENT, env=ENV)
    # The chained part is classified on its own (e.g. `touch ./a` is T2), but the
    # combined command must never be auto-logged T1.
    assert r.command_tier != Tier.AUTO_LOG


@pytest.mark.parametrize(
    "raw",
    [
        "systemctl start foo",
        "systemctl stop foo",
        "systemctl restart foo",
        "systemctl enable foo",
        "systemctl disable foo",
        "systemctl mask foo",
        "systemctl --now enable foo",
    ],
)
def test_systemd_system_scope_mutations_are_t4(raw):
    assert _tier(raw) == Tier.ALWAYS_APPROVE


@pytest.mark.parametrize(
    "raw",
    [
        "systemctl poweroff",
        "systemctl reboot",
        "systemctl halt",
        "systemctl kexec",
        "systemctl suspend",
        "systemctl hibernate",
        "systemctl --user poweroff",
        "systemctl --user reboot",
    ],
)
def test_systemd_power_state_is_denied(raw):
    assert _tier(raw) == Tier.DENY


def test_sudo_systemctl_still_denied():
    assert _tier("sudo systemctl restart foo") == Tier.DENY
