"""Base-name verbs for trusted absolute paths, and the generic help/version T2 rule."""

from __future__ import annotations

from pathlib import Path

import pytest
from shell_runner.catalog import Tier
from shell_runner.classifier import classify
from shell_runner.executor import trusted_bin_dirs
from shell_runner.normalizer import normalize

CWD = "/home/test/work"
ENV = {"HOME": "/home/test"}
AGENT = "some-agent"


def _tier(command: str) -> Tier:
    return classify(command, CWD, AGENT, env=ENV).tier


@pytest.fixture
def pixi_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".pixi" / "bin").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SHELL_RUNNER_EXTRA_PATH", raising=False)
    return tmp_path


# --- trusted_bin_dirs -------------------------------------------------------


def test_trusted_bin_dirs_keeps_dirs_already_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "tools").mkdir()
    monkeypatch.setenv("PATH", f"{tmp_path}/tools:/usr/bin")
    monkeypatch.setenv("SHELL_RUNNER_EXTRA_PATH", f"{tmp_path}/tools:{tmp_path}/missing")
    dirs = trusted_bin_dirs()
    assert f"{tmp_path}/tools" in dirs
    assert f"{tmp_path}/missing" not in dirs
    assert "/usr/bin" in dirs


# --- Part A: base-name verbs ------------------------------------------------


def test_usr_bin_ls_classifies_like_ls() -> None:
    plain = classify("ls -la", CWD, AGENT, env=ENV)
    absolute = classify("/usr/bin/ls -la", CWD, AGENT, env=ENV)
    assert absolute.template == plain.template
    assert absolute.tier == plain.tier
    assert absolute.matched_rule == plain.matched_rule is not None
    assert absolute.segments[0].segment.verb == "ls"


@pytest.mark.parametrize(
    "command",
    ["/tmp/ls", "/usr/bin/../../tmp/ls", "/usr//bin/ls", "/usr/./bin/ls", "./ls", "bin/ls"],
)
def test_untrusted_or_non_canonical_paths_keep_raw_token(command: str) -> None:
    result = classify(command, CWD, AGENT, env=ENV)
    assert result.segments[0].segment.verb == command
    assert result.template.startswith(command)
    assert result.tier == Tier.APPROVE_ONCE


def test_home_pixi_path_behaves_like_bare_pixi(pixi_home: Path) -> None:
    bare = classify("pixi --version", CWD, AGENT, env=ENV)
    absolute = classify(f"{pixi_home}/.pixi/bin/pixi --version", CWD, AGENT, env=ENV)
    assert absolute.template == bare.template == "pixi --version"
    assert absolute.tier == bare.tier


def test_home_pixi_path_untrusted_when_dir_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))  # no .pixi/bin created
    monkeypatch.delenv("SHELL_RUNNER_EXTRA_PATH", raising=False)
    result = normalize(f"{tmp_path}/.pixi/bin/pixi --version", CWD, env=ENV)
    assert result.segments[0].verb == f"{tmp_path}/.pixi/bin/pixi"


def test_trusted_absolute_sudo_is_still_denied() -> None:
    assert _tier("/usr/bin/sudo ls") == Tier.DENY


# --- Part B: generic help/version -------------------------------------------


def test_version_gives_t2_with_category() -> None:
    result = classify("pixi --version", CWD, AGENT, env=ENV)
    assert result.tier == Tier.AUTO_CAPPED
    assert result.matched_rule is not None
    assert result.matched_rule.category == "generic-help-version"
    assert result.segments[0].decision_step == "T2"


def test_generic_a_applies_to_unlisted_verb() -> None:
    assert _tier("mytool --help") == Tier.AUTO_CAPPED


def test_absolute_pixi_exec_help_is_t2(pixi_home: Path) -> None:
    assert _tier(f"{pixi_home}/.pixi/bin/pixi exec --help") == Tier.AUTO_CAPPED


@pytest.mark.parametrize(
    "command",
    [
        "sh evil --help",
        "python x --help",
        "npx foo --help",
        "foo --version extra",
        "foo --help --version",
        "git bogus -h",
        "git -V",
        "git Status --help",
        "/tmp/evil --version",
        "./evil --help",
        "pixi exec pkg --help",
        "pixi run --help x",
    ],
)
def test_help_version_not_accepted(command: str) -> None:
    assert _tier(command) == Tier.APPROVE_ONCE


@pytest.mark.parametrize("flag", ["--version", "--help"])
@pytest.mark.parametrize(
    "verb", ["source", "eval", "exec", "command", "builtin", "trap", "sudo", "doas", "su", "pkexec"]
)
def test_exec_and_privilege_verbs_excluded_from_generic_rule(verb: str, flag: str) -> None:
    result = classify(f"{verb} {flag}", CWD, AGENT, env=ENV)
    assert not result.matched_rule or result.matched_rule.category != "generic-help-version"


def test_help_version_does_not_override_specific_rules() -> None:
    # A specific rule (here the rm -rf T4/DENY rule) still decides; the generic
    # rule is only the T3 fallthrough.
    result = classify("rm -rf / --help", CWD, AGENT, env=ENV)
    assert result.command_tier in (Tier.DENY, Tier.ALWAYS_APPROVE)
    assert not result.matched_rule or result.matched_rule.category != "generic-help-version"


def test_help_version_in_chain_classified_per_segment() -> None:
    assert _tier("pixi --version && npx foo --help") == Tier.APPROVE_ONCE
    assert _tier("pixi --version && git bogus --help") == Tier.AUTO_CAPPED
