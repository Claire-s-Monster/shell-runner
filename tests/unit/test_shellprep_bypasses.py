"""Reviewer-reported shellprep bypasses: every command that writes into `.git` (or feeds an
interpreter from a heredoc / here-string) must end at T3 or stricter, never auto-execute.

`\\n` below is a real newline. Git-dir writes must come out as the git-dir guard's verdict
(T3, path_guarded, flagged); interpreter-fed-by-heredoc is T4 and non-promotable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shell_runner import executor
from shell_runner.catalog import Tier
from shell_runner.classifier import PERMISSIVENESS, classify
from shell_runner.write_targets import analyze

AGENT = "some-agent"
T3, T4 = Tier.APPROVE_ONCE, Tier.ALWAYS_APPROVE


@pytest.fixture(autouse=True)
def _roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor, "_CWD_ROOTS", [tmp_path.resolve()])
    monkeypatch.setenv("SHELL_RUNNER_CONFIG_DIR", str(tmp_path / "no-rules"))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = (tmp_path / "repo").resolve()
    (root / ".git" / "hooks").mkdir(parents=True)
    (root / ".git" / "config").write_text("[core]\n")
    return root


def _cls(command: str, cwd: Path):  # noqa: ANN202
    return classify(command, str(cwd), AGENT, env={"HOME": "/nonexistent-home"})


def _assert_blocked(command: str, cwd: Path) -> None:
    cls = _cls(command, cwd)
    assert PERMISSIVENESS[cls.command_tier] <= PERMISSIVENESS[T3], (command, cls.command_tier)
    assert cls.path_guarded, command


def _assert_git_guarded(command: str, cwd: Path) -> None:
    cls = _cls(command, cwd)
    assert cls.command_tier == T3, (command, cls.command_tier)
    assert cls.path_guarded, command
    assert cls.flagged, command


A = ["echo \\;\ntouch .git/config"]
B = [
    "echo \\ # ; touch .git/config",
    "echo a\\;#; touch .git/config",
    "echo ${x:- # }; touch .git/config",
]
C = [
    "cat <<EO\\F\nEOF\ntouch .git/config\nEO",
    'cat <<EO""F\nEOF\ntouch .git/config\nEO',
]
D = [
    "cat <<EOF\n$(touch .git/hooks/pre-commit)\nEOF",
    "cat <<EOF\n`touch .git/hooks/pre-commit`\nEOF",
    "cat <<-EOF\n\t$(touch .git/hooks/pre-commit)\n\tEOF",
]
E = [
    'cat <<< "$(touch .git/hooks/pre-commit)"',
    'cat <<<"`touch .git/config`"',
    'cat <<< "a $(echo "b") $(touch .git/config)"',
]
F = ['echo "$(echo "\'")" ; touch .git/config ; echo "$(echo "\'")"']
# found while fixing A-G: same classes (word state, joined lines, opaque backticks)
EXTRA = [
    "cat <<EOF\nEO\\\nF\ntouch .git/config\nEOF",  # joined line is the terminator
    "echo a\\\n# ; touch .git/config",  # continuation: `a#` is a word, not a comment
    "echo $((1))#; touch .git/config",
    "echo $(( 1 # ))\ntouch .git/config",
    "echo `echo # `; touch .git/config",
    "echo $(date)#; touch .git/config",
    'echo "$(echo # ")\n)"; touch .git/config',
]


@pytest.mark.parametrize("command", A + B + C + D + E + F + EXTRA)
def test_candidate_writes_into_git_dir_are_guarded(repo: Path, command: str) -> None:
    _assert_git_guarded(command, repo)


PLAIN_SUBST = [
    'echo "$(touch .git/config)"',
    'echo "`touch .git/config`"',
    'echo "a $(touch .git/config) b"',
    "echo $(touch .git/config)",
    "echo `touch .git/config`",
]


@pytest.mark.parametrize("command", PLAIN_SUBST)
def test_plain_substitutions_into_git_dir_are_guarded(repo: Path, command: str) -> None:
    _assert_git_guarded(command, repo)


def test_a_escaped_semicolon_keeps_newline_separator() -> None:
    assert [cp.verb for cp in analyze(A[0])] == ["echo", "touch"]


def test_f_nested_quotes_in_substitution_split_commands() -> None:
    verbs = [cp.verb for cp in analyze(F[0])]
    assert verbs.count("touch") == 1
    assert verbs[0] == "echo"


@pytest.mark.parametrize(
    "command",
    [
        "cat <<'EOF'\n$(touch .git/hooks/pre-commit)\nEOF",
        'cat <<"EOF"\n$(touch .git/hooks/pre-commit)\nEOF',
        "cat <<\\EOF\n$(touch .git/hooks/pre-commit)\nEOF",
        "cat <<'EOF'\n`touch .git/config`\nEOF",
    ],
)
def test_quoted_heredoc_body_stays_inert(repo: Path, command: str) -> None:
    cls = _cls(command, repo)
    assert cls.command_tier == T3
    assert not cls.path_guarded
    assert not cls.flagged


INTERPRETER_FED = [
    "bash <<EOF\ntouch .git/config\nEOF",
    "sh <<'EOF'\ntouch x\nEOF",
    "python3 <<EOF\nimport os\nEOF",
    'bash <<< "touch x"',
    "dash <<EOF\nx\nEOF",
    "zsh <<< x",
    "ksh <<EOF\nx\nEOF",
    "python <<'EOF'\nx\nEOF",
    "python3.12 <<EOF\nx\nEOF",
    "bash -e <<EOF\nx\nEOF",
    "bash -s -- a b <<< x",
    "/bin/bash <<EOF\nx\nEOF",
    "env bash <<EOF\nx\nEOF",
]


@pytest.mark.parametrize("command", INTERPRETER_FED)
def test_interpreter_fed_by_heredoc_is_t4_non_promotable(repo: Path, command: str) -> None:
    cls = _cls(command, repo)
    assert cls.command_tier == T4, (command, cls.command_tier)
    assert cls.non_promotable, command


@pytest.mark.parametrize(
    "command",
    ["cat <<EOF\nx\nEOF", "cat <<< hello", "echo hi", "python3 script.py", "bash script.sh"],
)
def test_non_interpreter_stdin_is_promotable(repo: Path, command: str) -> None:
    assert not _cls(command, repo).non_promotable, command


# Fail-safe direction: these already come out guarded (parse-correctly flagged, or
# unparseable). They must stay that way.
FAIL_SAFE = [
    "echo $((a+(b))) ; touch .git/config",
    "echo $((1<<2)) && touch .git/config",
    "cat <<EOF-END\nbody\nEOF-END\ntouch .git/config",
    "cat <<EOF\n EOF\ntouch .git/config\nEOF",
    "cat <<A <<B\na\nA\nb\nB\ntouch .git/config",
    "echo $(cat <<EOF\nx\nEOF\n) ; touch .git/config",
    "cat <<< hi && touch .git/config",
    "# it's a comment\ntouch .git/config",
]


@pytest.mark.parametrize("command", FAIL_SAFE)
def test_fail_safe_forms_stay_guarded(repo: Path, command: str) -> None:
    _assert_blocked(command, repo)
