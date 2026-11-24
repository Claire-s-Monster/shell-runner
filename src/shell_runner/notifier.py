"""Desktop approval notifier for shell-runner (issue #136).

Polls the read-only ``POST /pending`` endpoint and raises one ``notify-send``
popup per new T3/T4 approval prompt. Standard library only. It never approves
anything: ``/pending`` returns no tokens and popups only inform.
"""

from __future__ import annotations

import html
import re
import sys
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
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


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _log(message: str) -> None:
    print(f"shell-runner-notify: {message}", file=sys.stderr)


def parse_expiry(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class Notifier:
    """Polling core. `fetch()` returns prompt dicts or raises OSError/ValueError;
    `notify(title, body, urgency)` raises one popup; `clock()` returns aware UTC."""

    fetch: Callable[[], list[dict]]
    notify: Callable[[str, str, str], None]
    clock: Callable[[], datetime] = _utcnow
    home: str = field(default_factory=lambda: str(Path.home()))
    seen: set[str] = field(default_factory=set)

    def tick(self) -> None:
        now = self.clock()
        self._handle(self.fetch(), now)

    def _handle(self, prompts: list[dict], now: datetime) -> None:
        live: set[str] = set()
        for prompt in prompts:
            try:
                self._handle_one(prompt, now, live)
            except (KeyError, TypeError, ValueError) as exc:
                _log(f"skipping malformed prompt {prompt.get('id')}: {exc!r}")
        self.seen &= live

    def _handle_one(self, prompt: dict, now: datetime, live: set[str]) -> None:
        expiry = parse_expiry(prompt.get("expires_at"))
        if expiry is None:
            _log(f"skipping prompt {prompt.get('id')}: bad expires_at")
            return
        remaining = (expiry - now).total_seconds()
        if remaining <= 0 or prompt.get("approve_decision") is not None:
            return
        live.add(prompt["id"])
        if prompt["id"] not in self.seen:
            self.seen.add(prompt["id"])
            self._popup(prompt, remaining, "approval needed")

    def _popup(self, prompt: dict, remaining: float, what: str) -> None:
        tier = int(prompt["command_tier"])
        urgency = "critical" if tier == 4 else "normal"
        title = f"shell-runner: T{tier} {what}"
        self.notify(title, build_body(prompt, remaining, self.home), urgency)
