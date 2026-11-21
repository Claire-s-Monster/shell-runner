"""Reproduction: raw ``__TKSENTINEL_<n>`` placeholders leaking into stored templates.

``classify()`` stores ``normalize(command, cwd, env=env).template`` as
``ClassificationResult.template``, which the server persists as the prompt's
``normalized_template`` and promotes to ``template_approvals.template``. A leaked
sentinel there makes the approval rule unmatchable or overly broad.

These tests are red on purpose until the normalizer is fixed.
"""

from __future__ import annotations

import pytest

from shell_runner.normalizer import normalize

CWD = "/home/test/work"
ENV = {"HOME": "/home/test"}
SENTINEL = "__TKSENTINEL_"


def _template(command: str) -> str:
    return normalize(command, CWD, env=ENV).template


_XFAIL = pytest.mark.xfail(strict=True, reason="#117 step 2: embedded sentinel restore")

LEAK_CASES = [
    pytest.param(
        "export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus",
        id="a_dbus",
        marks=_XFAIL,
    ),
    pytest.param("ls $ENV/bin/", id="a_var_prefix_path"),
    pytest.param("export PATH=$CONDA_PREFIX/bin:$PATH", id="a_path_colon", marks=_XFAIL),
    pytest.param("http_code=$(cat status.txt)", id="a_assign_cmdsubst"),
    pytest.param(
        'declare -A urls=(["source"]="https://example.com/a.tar.gz")',
        id="a_assoc_array_url",
        marks=_XFAIL,
    ),
    pytest.param("echo pre$(date)suffix", id="a_prefix_suffix", marks=_XFAIL),
    pytest.param("x=<(cat f)", id="a_procsubst_assign"),
    pytest.param("curl https://example.com/$V", id="a_url_with_var"),
    pytest.param("tool --opt=$VAR", id="a_flag_eq_var", marks=_XFAIL),
    pytest.param(
        'for d in a b; do TARGETS="$TARGETS $d"; done', id="b_quoted_loop_concat", marks=_XFAIL
    ),
    pytest.param(
        'PATH="$HOME/.local/bin:$PATH" node --test x.test.ts',
        id="b_quoted_path_colon",
        marks=_XFAIL,
    ),
    pytest.param("tee out.md << 'EOF'\nsee ```code``` and `x`\nEOF", id="c_heredoc_quoted_delim"),
    pytest.param("tee out.md << EOF\nsee ```code``` and `x`\nEOF", id="c_heredoc_unquoted_delim"),
    pytest.param("echo $HOME", id="ctrl_whole_token_var"),
]


@pytest.mark.parametrize("command", LEAK_CASES)
def test_template_has_no_raw_sentinel(command: str) -> None:
    template = _template(command)
    assert SENTINEL not in template, f"leaked sentinel: {template!r}"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("tee out.md << 'EOF'\nsee ```code``` and `x`\nEOF", id="quoted_delim"),
        pytest.param("tee out.md << EOF\nsee ```code``` and `x`\nEOF", id="unquoted_delim"),
    ],
)
def test_heredoc_body_not_in_template(command: str) -> None:
    template = _template(command)
    assert "see" not in template, f"heredoc body leaked: {template!r}"
