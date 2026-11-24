"""Desktop approval notifier for shell-runner (issue #136).

Polls the read-only ``POST /pending`` endpoint and raises one ``notify-send``
popup per new T3/T4 approval prompt. Standard library only. It never approves
anything: ``/pending`` returns no tokens and popups only inform.
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

CMD_LIMIT = 120
TRUNCATED = "… (truncated)"
_WHITESPACE_CTRL = re.compile(r"[\n\r\t\x85]")
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
_MISSING = "?"


def clean(text: str, limit: int | None = None) -> str:
    """Strip control/format characters, optionally cut to `limit`, escape markup.

    Drops Unicode categories Cc, Cf (e.g. U+202E bidi override), Zl and Zp
    (U+2028/2029). Newlines, tabs and NEL (U+0085) become spaces so multi-line
    commands stay readable. The length limit is applied before markup escaping,
    so an HTML entity is never cut in half; the escaped text may be longer than
    `limit` characters.
    """
    text = _WHITESPACE_CTRL.sub(" ", text)
    text = "".join(c for c in text if unicodedata.category(c) not in _DROPPED_CATEGORIES)
    if limit is not None and len(text) > limit:
        return html.escape(text[:limit], quote=False) + TRUNCATED
    return html.escape(text, quote=False)


def shorten_home(cwd: str, home: str) -> str:
    home = home.rstrip("/")
    if not home:
        return cwd
    if cwd == home or cwd.startswith(home + "/"):
        return "~" + cwd[len(home) :]
    return cwd


def format_remaining(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60}m {total % 60}s"


def build_body(prompt: Mapping[str, Any], remaining_s: float, home: str) -> str:
    lines: list[str] = []
    if prompt.get("scope_warning"):
        lines.append(clean(str(prompt["scope_warning"])))
    agent_id = str(prompt.get("agent_id") or _MISSING)
    cwd = str(prompt.get("cwd") or _MISSING)
    raw_cmd = str(prompt.get("raw_cmd") or _MISSING)
    lines += [
        f"agent: {clean(agent_id)}",
        f"cwd: {clean(shorten_home(cwd, home))}",
        f"cmd: {clean(raw_cmd, CMD_LIMIT)}",
        f"expires in {format_remaining(remaining_s)}",
        "decide on the Pending Prompts page",
    ]
    return "\n".join(lines)
