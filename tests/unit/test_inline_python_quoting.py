"""Inline python (`-c`, heredoc) must respect shell quoting when deciding whether an import
can be resolved statically.

USABILITY (test_usability_*): real, successfully-run commands whose `$` is literal to the
shell (single quotes, `\\$`, quoted heredoc delimiter) or whose escaped quotes are valid
Python after shell unquoting must stay pinnable. SECURITY: anything the shell would expand
or that genuinely cannot be parsed must stay unpinnable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shell_runner import script_pin

# Byte-identical `cmd` values from the telemetry replay (JSON string literals are valid
# Python string literals).
# fmt: off
# line119/line121: literal backslash-n between statements, so the body is not valid Python
# after shell unquoting (they can never have run successfully).
UNPARSEABLE_REPLAY_CMDS = {
    "line119":"pixi run python3 -c \"import asyncio, asyncpg, json\\nasync def main():\\n    conn = await asyncpg.connect('postgresql://localhost/session_intelligence')\\n    row = await conn.fetchrow(\\\"UPDATE sessions SET status='completed' WHERE id='session-20260515-214106-f9da79' RETURNING id, status, project_name, started_at\\\")\\n    print(json.dumps(dict(row) if row else None, default=str))\\n    await conn.close()\\nasyncio.run(main())\"",  # noqa: E501
    "line121": "pixi run python3 -c \"import asyncio, asyncpg, json\\nasync def main():\\n    conn = await asyncpg.connect('postgresql://localhost/session_intelligence')\\n    row = await conn.fetchrow(\\\"SELECT id, status, project_name FROM sessions WHERE id='session-20260515-214106-f9da79'\\\")\\n    print(json.dumps(dict(row) if row else None, default=str))\\n    await conn.close()\\nasyncio.run(main())\"",  # noqa: E501
}
REPLAY_CMDS = {
    "line123": "/opt/conda/bin/pixi run python3 -c 'import asyncio, asyncpg, json\nasync def main():\n    conn = await asyncpg.connect(\"postgresql://localhost/session_intelligence\")\n    row = await conn.fetchrow(\"UPDATE sessions SET status=$$completed$$ WHERE id=$$session-20260515-214106-f9da79$$ RETURNING id, status, project_name, started_at\")\n    print(json.dumps(dict(row) if row else None, default=str))\n    await conn.close()\nasyncio.run(main())'",  # noqa: E501
    "line163": ".pixi/envs/default/bin/python -c \"\nimport asyncio, asyncpg\nasync def main():\n    conn = await asyncpg.connect('postgresql://localhost/session_intelligence')\n    ids = ('session-20260521-175713-94fff4', 'session-20260521-175730-640b6d')\n    d = await conn.execute('DELETE FROM decisions WHERE session_id = ANY(\\$1)', list(ids))\n    s = await conn.execute('DELETE FROM sessions WHERE id = ANY(\\$1)', list(ids))\n    print('decisions:', d, '| sessions:', s)\n    await conn.close()\nasyncio.run(main())\n\"",  # noqa: E501
    "line4262": ".pixi/envs/ci/bin/python -c \"\nimport datetime, time, asyncio, asyncpg\nprint('host tzname:', time.tzname, 'utcoffset_s:', -time.timezone, 'dst_now:', time.localtime().tm_isdst)\nprint('naive now :', datetime.datetime.now().isoformat())\nprint('utc now   :', datetime.datetime.now(datetime.UTC).isoformat())\nasync def m():\n    c = await asyncpg.connect('postgresql://localhost/session_intelligence_test')\n    print('pg TimeZone setting:', await c.fetchval('show timezone'))\n    for t, col in [('session_summaries','key_changes'),('session_summaries','tags'),('mcp_sessions','last_activity'),('mcp_sessions','created_at')]:\n        print(t, col, '->', await c.fetchval('select data_type from information_schema.columns where table_name=\\$1 and column_name=\\$2', t, col))\n    await c.close()\nasyncio.run(m())\n\"",  # noqa: E501
}
# fmt: on


def _plan(command: str, tmp_path: Path) -> script_pin.Plan:
    return script_pin.analyze(command, str(tmp_path))


def _short_reasons(plan: script_pin.Plan) -> str:
    return "; ".join(str(r)[:120] for r in plan.reasons)


# --- U1. real replay commands --------------------------------------------------------


@pytest.mark.parametrize("label", list(REPLAY_CMDS))
def test_usability_replay_cmds(label: str, tmp_path: Path) -> None:
    plan = _plan(REPLAY_CMDS[label], tmp_path)
    assert plan.unpinnable is False, (label, _short_reasons(plan))


# --- U2. synthetic usability cases ---------------------------------------------------

USABILITY = {
    "single_quoted_literal_dollar": r"""python -c 'import json; print("$$ and $1 and `x`")'""",
    "escaped_dollar_in_dq": r'''python -c "import json; print(\"\$1\")"''',
    "escaped_quotes_no_dollar": r'''python3 -c "import json; print(\"a\")"''',
    "real_newlines_escaped_quotes": (
        'python3 -c "import asyncio, json\n'
        "async def main():\n"
        '    print(json.dumps(\\"UPDATE t SET a=1\\"))\n'
        'asyncio.run(main())"'
    ),
    "quoted_heredoc_delimiter": "python - <<'EOF'\nimport json\nprint(\"$HOME\")\nEOF",
    "single_quoted_backtick_only": """python -c 'import json; print("`")'""",
}


@pytest.mark.parametrize("label", list(USABILITY))
def test_usability_synthetic(label: str, tmp_path: Path) -> None:
    plan = _plan(USABILITY[label], tmp_path)
    assert plan.unpinnable is False, (label, _short_reasons(plan))


# --- S. security cases that must stay strict -----------------------------------------

UNPINNABLE = {
    "dollar_var_import": 'python -c "import $MOD"',
    "dollar_var_statement": 'python -c "import json; $X"',
    "expanded_dollar_in_dq": r'''python -c "import json; print(\"$HOME\")"''',
    "backtick_in_dq": 'python -c "import json; `cmd`"',
    "command_substitution": 'python -c "import json; $(cat x)"',
    "unquoted_heredoc": "python - <<EOF\nimport json\n$X\nEOF",
    "concatenated_word": """python -c 'import json'"$X\"""",
    "genuine_syntax_error": 'python -c "import json; def ("',
    "sq_plus_expanded_substitution": """python -c 'import json; print(1)'"$(true)\"""",
}


@pytest.mark.parametrize("label", list(UNPARSEABLE_REPLAY_CMDS))
def test_security_unparseable_replay_cmds(label: str, tmp_path: Path) -> None:
    plan = _plan(UNPARSEABLE_REPLAY_CMDS[label], tmp_path)
    assert plan.unpinnable is True, label


@pytest.mark.parametrize("label", list(UNPINNABLE))
def test_security_stays_unpinnable(label: str, tmp_path: Path) -> None:
    plan = _plan(UNPINNABLE[label], tmp_path)
    assert plan.unpinnable is True, (label, UNPINNABLE[label])


def test_security_ansi_c_quoting(tmp_path: Path) -> None:
    command = r"""python -c 'import json; '$'\x69mport os'"""
    plan = _plan(command, tmp_path)
    assert plan.unpinnable is True, command


# --- R. review round 2: fail-open attempts against the quote-aware scanner ------------

R_UNPINNABLE = {
    "R1_bash_c_escaped_dollar": r'''bash -c "python3 -c \"import os; x='\$A'\""''',
    "R1_sh_c_escaped_backtick": r'''sh -c "python3 -c \"import json; print(\`id\`)\""''',
    "R2_arith_shift_decoy_lines": (
        "echo $((1<<2)); echo 'import os; x='\"'\"'$A'\"'\"''\npython3 -c \"import os; x='$A'\""
    ),
    "R2_arith_shift_one_line": 'x=$((1<<2)); python3 -c "import os; $A"',
    "R3_decoy_backslash_newline": (
        "echo 'import os; x='\"'\"'$A'\"'\"''; python3 -c \"import os;\\\n x='$A'\""
    ),
    "R9_ansi_c_heredoc_delimiter": r"""python3 - <<$'E\x4fF'""" + "\nimport os\n$A\nEOF\n",
}


@pytest.mark.parametrize("label", list(R_UNPINNABLE))
def test_security_review_round2(label: str, tmp_path: Path) -> None:
    plan = _plan(R_UNPINNABLE[label], tmp_path)
    assert plan.unpinnable is True, (label, R_UNPINNABLE[label])


# --- D. literal `$` inside double quotes (bash expands only before name/digit/special) -----

DQ_LITERAL_DOLLAR = {
    "regex_end_anchor": '''python3 -c "import re; print(re.search(r'.*$', 'a'))"''',
    "regex_alternation": '''python3 -c "import re; print(re.split(r'\\s*$|x', 'a'))"''',
    "dollar_space": 'python3 -c "import os  # CRT$ merge"',
    "dollar_quote": '''python3 -c "x = 'cost: $'; import json"''',
    "dollar_after_escaped_backslash": (
        '''python3 -c "import re; print(re.sub(r'\\\\$\\\\{', '', 'a'))"'''
    ),
    "dollar_in_print": '''python3 -c "import json; print('$')"''',
}


@pytest.mark.parametrize("label", list(DQ_LITERAL_DOLLAR))
def test_usability_dq_literal_dollar(label: str, tmp_path: Path) -> None:
    plan = _plan(DQ_LITERAL_DOLLAR[label], tmp_path)
    assert plan.unpinnable is False, (label, _short_reasons(plan))


def test_usability_dq_literal_dollar_local_import_pinned(tmp_path: Path) -> None:
    (tmp_path / "localmod.py").write_text("")
    plan = _plan('''python3 -c "import re, localmod; re.match(r'a$', 'a')"''', tmp_path)
    assert plan.unpinnable is False, _short_reasons(plan)
    assert any("localmod" in r.operand for r in plan.refs), plan.refs


DQ_EXPANDING = {
    "name": "$a",
    "underscore": "$_x",
    "digit": "$1",
    "brace": "${x}",
    "command_subst": "$(id)",
    "arith": "$((1+1))",
    "at": "$@",
    "star": "$*",
    "hash": "$#",
    "question": "$?",
    "dash": "$-",
    "pid": "$$",
    "bang": "$!",
    "zero": "$0",
    "backtick": "`id`",
}


@pytest.mark.parametrize("label", list(DQ_EXPANDING))
def test_security_dq_expanding_dollar(label: str, tmp_path: Path) -> None:
    (tmp_path / "localmod.py").write_text("")
    command = f'python3 -c "import localmod; {DQ_EXPANDING[label]}"'
    plan = _plan(command, tmp_path)
    assert plan.unpinnable is True, (label, command)


_BS_NL = "\\" + "\n"
DQ_EXPANDING_ODD = {
    "continuation_name": f"x='${_BS_NL}HOME'",
    "two_continuations_name": f"x='${_BS_NL}{_BS_NL}HOME'",
    "continuation_brace": f"x='${_BS_NL}{{HOME}}'",
    "continuation_digit": f"x='${_BS_NL}1'",
    "legacy_arith": "x='$[x]'",
    "legacy_arith_expr": "x='$[1+1]'",
}


@pytest.mark.parametrize("label", list(DQ_EXPANDING_ODD))
def test_security_dq_expanding_dollar_odd_forms(label: str, tmp_path: Path) -> None:
    (tmp_path / "localmod.py").write_text("")
    command = f'python3 -c "import localmod; {DQ_EXPANDING_ODD[label]}"'
    plan = _plan(command, tmp_path)
    assert plan.unpinnable is True, (label, command)


def test_usability_dq_dollar_continuation_then_quote_is_literal(tmp_path: Path) -> None:
    (tmp_path / "localmod.py").write_text("")
    command = f"python3 -c \"import localmod; x='${_BS_NL}'\""
    plan = _plan(command, tmp_path)
    assert plan.unpinnable is False, _short_reasons(plan)
    assert any("localmod" in r.operand for r in plan.refs), plan.refs


def test_security_sq_dollar_joined_to_name_is_literal_but_dq_after_is_not(
    tmp_path: Path,
) -> None:
    (tmp_path / "localmod.py").write_text("")
    # `$` in single quotes is literal; the adjacent "$x" in double quotes expands.
    command = """python3 -c "import localmod"'$'"$x\""""
    assert _plan(command, tmp_path).unpinnable is True, command


R_USABILITY = {
    "R1_bash_c_single_quoted": """bash -c 'python3 -c "import json; print(1)"'""",
    "R2_arith_shift_then_literal": "echo $((1<<2)); python3 -c 'import json'",
}


@pytest.mark.parametrize("label", list(R_USABILITY))
def test_usability_review_round2(label: str, tmp_path: Path) -> None:
    plan = _plan(R_USABILITY[label], tmp_path)
    assert plan.unpinnable is False, (label, _short_reasons(plan))
