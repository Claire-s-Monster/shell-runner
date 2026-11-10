"""Single shared command preprocessing (review of #84/#85).

write_targets (path/exec guards) and the normalizer must see the SAME commands: heredoc
bodies, comments, ANSI-C quotes and backslash-newline are resolved once, before any
detector runs. A stray apostrophe in a heredoc body or comment must never desync them, and
a command that cannot be parsed is a guarded T3 (`path_guard:unparseable`), never a pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shell_runner import executor
from shell_runner.catalog import Tier
from shell_runner.classifier import PERMISSIVENESS, _classify_catalog, classify
from shell_runner.normalizer import normalize
from shell_runner.write_targets import analyze

AGENT = "some-agent"
T3, T4 = Tier.APPROVE_ONCE, Tier.ALWAYS_APPROVE
SYSTEM = "awk 'BEGIN{system(\"id\")}'"


@pytest.fixture(autouse=True)
def _roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor, "_CWD_ROOTS", [tmp_path.resolve()])
    monkeypatch.setenv("SHELL_RUNNER_CONFIG_DIR", str(tmp_path / "no-rules"))


def _cls(command: str, cwd: Path):  # noqa: ANN202
    return classify(command, str(cwd), AGENT, env={"HOME": "/nonexistent-home"})


def _assert_guarded_t3(command: str, cwd: Path) -> None:
    cls = _cls(command, cwd)
    assert PERMISSIVENESS[cls.command_tier] <= PERMISSIVENESS[T3], command
    assert cls.path_guarded, command


# --- 1. tokenizer desync: the exploit strings -----------------------------

DESYNC = [
    f"cat <<E\nit's\nE\n{SYSTEM}",
    f"cat <<'E'\nit's\nE\n{SYSTEM}",
    f'cat <<"E"\nit\'s\nE\n{SYSTEM}',
    f"cat <<-E\n\tit's\n\tE\n{SYSTEM}",
    f"echo hi # it's\n{SYSTEM}",
    f"# it's a comment\n{SYSTEM}",
    f"echo $'a\\'b'\n{SYSTEM}",
    f"echo $'it\\'s' && {SYSTEM}",
    f"echo '<<X'\n{SYSTEM}\nX",
    f'echo "<<X"\n{SYSTEM}\nX',
    f"echo hi # note \\\n{SYSTEM}",  # a comment ends at the newline, the backslash is data
    f"cat <<A <<B\nit's\nA\nB\n{SYSTEM}",
]


@pytest.mark.parametrize("command", DESYNC)
def test_desync_exploits_still_see_the_later_command(tmp_path: Path, command: str) -> None:
    cls = _cls(command, tmp_path)
    assert cls.command_tier == T4, command
    assert cls.path_guarded, command


def test_every_command_after_a_heredoc_is_a_separate_segment(tmp_path: Path) -> None:
    command = "cat <<E\nit's\nE\nawk 'BEGIN{print 1}'\ngit status"
    assert [cp.verb for cp in analyze(command)] == ["cat", "awk", "git"]
    assert len(normalize(command, str(tmp_path)).segments) == 3


def test_unparseable_command_is_guarded_t3_not_a_silent_fallback(tmp_path: Path) -> None:
    (cp,) = analyze("echo 'abc")
    assert cp.unparseable
    _assert_guarded_t3("echo 'abc", tmp_path)
    _assert_guarded_t3(f"echo 'abc\n{SYSTEM}", tmp_path)


def test_unparseable_finding_has_its_own_rule_when_catalog_is_lenient(tmp_path: Path) -> None:
    from shell_runner import path_guard

    finding = path_guard.evaluate("echo 'abc", str(tmp_path), {})
    assert finding is not None
    assert finding.category == "unparseable"
    assert finding.guarded


# --- 2. backslash-newline -------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        'awk "BEGIN{sys\\\ntem(\\"id\\")}"',
        'awk "BEGIN{system\\\n(\\"id\\")}"',
        "git fetch --upload-\\\npack=sh origin",
        "git fetch --upload-pack=\\\nsh origin",
        "git \\\n  -c core.pager=x log",
        "git fet\\\nch --upload-pack=sh origin",
    ],
)
def test_backslash_newline_is_joined_before_detectors_run(tmp_path: Path, command: str) -> None:
    _assert_guarded_t3(command, tmp_path)


def test_awk_system_split_by_backslash_newline_is_t4(tmp_path: Path) -> None:
    assert _cls('awk "BEGIN{sys\\\ntem(\\"id\\")}"', tmp_path).command_tier == T4


def test_single_quoted_backslash_newline_stays_literal(tmp_path: Path) -> None:
    (cp,) = analyze("awk 'BEGIN{print \"a\\\nb\"}'")
    assert cp.words == ['BEGIN{print "a\\\nb"}']
    cls = _cls("awk 'BEGIN{print \"a\\\nb\"}'", tmp_path)
    assert not cls.path_guarded


def test_bash_joins_without_inserting_a_space() -> None:
    assert [cp.verb for cp in analyze("ec\\\nho x")] == ["echo"]
    assert normalize("ec\\\nho x", "/tmp").segments[0].verb == "echo"
    # the usual `cmd \` + indented continuation is unchanged
    assert normalize("echo a \\\n    b", "/tmp").template == "echo a b"


def test_escaped_backslash_before_newline_is_not_a_continuation() -> None:
    assert [cp.verb for cp in analyze("echo a\\\\\nwhoami")] == ["echo", "whoami"]


def test_double_quoted_continuation_joins_and_single_quoted_does_not() -> None:
    (cp,) = analyze("echo \"a\\\nb\" 'c\\\nd'")
    assert cp.words == ["ab", "c\\\nd"]


# --- 3. normalizer and write_targets agree --------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "ls\nwhoami",
        "git status\ngit log",
        "echo a &&\necho b",
        "echo a ||\n  echo b",
        "echo a |\n  cat",
        "echo a\n\n\necho b",
        "echo a ;\necho b",
        "cat <<EOF\nhello\nEOF\nls",
        "cd sub && ls",
    ],
)
def test_segment_counts_agree_and_no_false_unparseable(tmp_path: Path, command: str) -> None:
    (tmp_path / "sub").mkdir()
    cps = analyze(command)
    assert not any(cp.unparseable for cp in cps)
    cls = _cls(command, tmp_path)
    assert not cls.path_guarded, command
    folded = command.startswith("cd ")
    assert len(cps) == len(cls.segments) + (1 if folded else 0)


def test_newline_is_a_command_separator_in_the_normalizer(tmp_path: Path) -> None:
    result = normalize("ls\nwhoami", str(tmp_path))
    assert [s.verb for s in result.segments] == ["ls", "whoami"]


def test_newline_inside_quotes_stays_data() -> None:
    (cp,) = analyze("sed -e 'a x\nw y' f")
    assert cp.words == ["-e", "a x\nw y", "f"]


# --- heredoc / multi-line positives keep working --------------------------


def test_plain_heredoc_keeps_its_tier(tmp_path: Path) -> None:
    for command in (
        "cat <<'EOF' > notes.txt\nhello\nEOF",
        "cat <<EOF > notes.txt\nhello\nEOF",
        "cat <<EOF\nhello\nEOF",
    ):
        cls = _cls(command, tmp_path)
        assert cls.command_tier == T3, command
        assert not cls.path_guarded, command
        assert not cls.flagged, command


def test_heredoc_redirect_target_is_still_a_write_target(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    cls = _cls("cat <<'EOF' > .git/config\n[core]\nEOF", tmp_path)
    assert cls.flagged
    assert cls.matched_rule is not None
    assert cls.matched_rule.pattern == "path_guard:git_dir_write"


@pytest.mark.parametrize(
    "command",
    [
        "sed '/x/{\np\nd\n}' f",
        "awk 'BEGIN{\nprint 1\n}' f",
        "sed -e 'a x\np' f",
    ],
)
def test_multiline_quoted_scripts_stay_as_classified(tmp_path: Path, command: str) -> None:
    cls = _cls(command, tmp_path)
    assert cls.command_tier == _classify_catalog(command, str(tmp_path), AGENT).command_tier
    assert not cls.path_guarded


def test_multiline_quoted_awk_system_is_still_t4(tmp_path: Path) -> None:
    assert _cls("awk 'BEGIN{\nsystem(\"id\")\n}'", tmp_path).command_tier == T4


# --- 3b. ANSI-C quotes decode so escapes cannot hide a keyword -------------


def test_ansi_c_escapes_are_decoded(tmp_path: Path) -> None:
    assert _cls("awk $'BEGIN{sys\\x74em(\"id\")}'", tmp_path).command_tier == T4
    assert _cls("awk $'BEGIN{sys\\164em(\"id\")}'", tmp_path).command_tier == T4
    (cp,) = analyze("echo $'a\\'b'")
    assert cp.words == ["a'b"]


# --- 4. --exec-path and friends -------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git --exec-path=/x status",
        "git --exec-path status",
        "git --super-prefix=x status",
        "git --list-cmds=main",
        "git -p log",
        "git --paginate log",
        "git -C sub --exec-path=/x log",
    ],
)
def test_more_git_global_hazards_are_guarded(tmp_path: Path, command: str) -> None:
    _assert_guarded_t3(command, tmp_path)


@pytest.mark.parametrize(
    "command",
    ["git log -p", "git diff -p", "git branch --list", "git tag --list", "git log --parents"],
)
def test_p_and_list_are_only_hazards_in_the_global_position(tmp_path: Path, command: str) -> None:
    cls = _cls(command, tmp_path)
    assert cls.command_tier == _classify_catalog(command, str(tmp_path), AGENT).command_tier
    assert not cls.path_guarded


# --- 5. awk -W ------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "awk -W exec prog f",
        "awk -Wexec prog f",
        "awk -W file=prog f",
        "awk -Wfile=prog f",
        "awk -W source='BEGIN{print 1}' f",
        "awk -W include=x '{print}' f",
        "awk -W load=x '{print}' f",
        "awk -W dump=out '{print}' f",
        "awk -W profile=out '{print}' f",
        "awk -W bogus '{print}' f",
        "awk -W version",
        "mawk -W exec prog f",
        "awk -F: -W exec prog f",
    ],
)
def test_awk_w_options_are_guarded_t3(tmp_path: Path, command: str) -> None:
    _assert_guarded_t3(command, tmp_path)


def test_awk_w_value_is_never_the_program_text(tmp_path: Path) -> None:
    # `-W source=...` with a benign-looking value must not be read as a safe program
    _assert_guarded_t3("awk -W source='{print $1}' f", tmp_path)


# --- 6. bare / structural repos at execution time ---------------------------


@pytest.fixture
def bare(tmp_path: Path) -> Path:
    root = (tmp_path / "bare").resolve()
    (root / "objects").mkdir(parents=True)
    (root / "refs").mkdir()
    (root / "hooks").mkdir()
    (root / "HEAD").write_text("ref: refs/heads/main\n")
    return root


@pytest.mark.parametrize(
    "command",
    ["git status", "git log --oneline", "git diff", "git fetch origin", "git branch -a"],
)
def test_git_in_a_structural_repo_is_guarded_t3(bare: Path, command: str) -> None:
    _assert_guarded_t3(command, bare)
    _assert_guarded_t3(command, bare / "hooks")


def test_git_with_c_or_cd_into_a_structural_repo_is_guarded(bare: Path) -> None:
    _assert_guarded_t3("git -C bare status", bare.parent)
    _assert_guarded_t3("cd bare && git status", bare.parent)
    _assert_guarded_t3("cd bare; git log", bare.parent)


def test_non_git_commands_and_normal_repos_are_unaffected(bare: Path, tmp_path: Path) -> None:
    assert not _cls("ls", bare).path_guarded
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    cls = _cls("git status", repo)
    assert not cls.path_guarded
    assert cls.command_tier == _classify_catalog("git status", str(repo), AGENT).command_tier


# --- 7. git stash parsing ---------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git stash -m list",
        "git stash -- show",
        "git stash -p",
        "git stash -k",
        "git stash -u",
        "git stash -q list",
        "git stash --include-untracked show",
        "git stash push list",
        "git stash pop",
        "git stash",
    ],
)
def test_stash_without_list_or_show_as_first_word_is_ordinary_t3(
    tmp_path: Path, command: str
) -> None:
    cls = _cls(command, tmp_path)
    assert PERMISSIVENESS[cls.command_tier] <= PERMISSIVENESS[T3], command
    assert not cls.path_guarded, command


@pytest.mark.parametrize(
    "command",
    ["git stash list", "git stash list --oneline", "git stash show", "git stash show -p stash@{1}"],
)
def test_stash_list_and_show_keep_their_tier(tmp_path: Path, command: str) -> None:
    cls = _cls(command, tmp_path)
    assert cls.command_tier == _classify_catalog(command, str(tmp_path), AGENT).command_tier
    assert PERMISSIVENESS[cls.command_tier] > PERMISSIVENESS[T3]
