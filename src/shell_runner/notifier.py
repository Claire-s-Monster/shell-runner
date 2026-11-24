"""Desktop approval notifier for shell-runner (issue #136).

Polls the read-only ``POST /pending`` endpoint and raises one ``notify-send``
popup per new T3/T4 approval prompt. Standard library only. It never approves
anything: ``/pending`` returns no tokens and popups only inform.
"""

from __future__ import annotations

import html
import re
import unicodedata

CMD_LIMIT = 120
TRUNCATED = "… (truncated)"
_WHITESPACE_CTRL = re.compile(r"[\n\r\t]")
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def clean(text: str, limit: int | None = None) -> str:
    """Strip control/format characters, optionally cut to `limit`, escape markup.

    Drops Unicode categories Cc, Cf (e.g. U+202E bidi override), Zl and Zp
    (U+2028/2029). Newlines and tabs become spaces so multi-line commands stay
    readable. Truncation happens before escaping so an entity is never cut in half.
    """
    text = _WHITESPACE_CTRL.sub(" ", text)
    text = "".join(c for c in text if unicodedata.category(c) not in _DROPPED_CATEGORIES)
    if limit is not None and len(text) > limit:
        return html.escape(text[:limit], quote=False) + TRUNCATED
    return html.escape(text, quote=False)


def shorten_home(cwd: str, home: str) -> str:
    home = home.rstrip("/")
    if cwd == home or cwd.startswith(home + "/"):
        return "~" + cwd[len(home) :]
    return cwd


def format_remaining(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60}m {total % 60}s"


def build_body(prompt: dict, remaining_s: float, home: str) -> str:
    lines: list[str] = []
    if prompt.get("scope_warning"):
        lines.append(clean(prompt["scope_warning"]))
    lines += [
        f"agent: {clean(prompt['agent_id'])}",
        f"cwd: {clean(shorten_home(prompt['cwd'], home))}",
        f"cmd: {clean(prompt['raw_cmd'], CMD_LIMIT)}",
        f"expires in {format_remaining(remaining_s)}",
        "decide on the Pending Prompts page",
    ]
    return "\n".join(lines)
