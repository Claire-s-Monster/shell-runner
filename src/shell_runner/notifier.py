"""Desktop approval notifier for shell-runner (issue #136).

Polls the read-only ``POST /pending`` endpoint and raises one ``notify-send``
popup per new T3/T4 approval prompt. Standard library only. It never approves
anything: ``/pending`` returns no tokens and popups only inform.
"""

from __future__ import annotations

import html
import http.client
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

CMD_LIMIT = 120
REMIND_BEFORE_S = 60
MAX_BACKOFF_S = 60.0
DEFAULT_INTERVAL_S = 3.0
MAX_RESPONSE_BYTES = 1_048_576
READ_CHUNK_BYTES = 8192
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

    fetch: Callable[[], list[dict[str, Any]]]
    notify: Callable[[str, str, str], None]
    clock: Callable[[], datetime] = _utcnow
    home: str = field(default_factory=lambda: str(Path.home()))
    seen: set[str] = field(default_factory=set)
    reminded: set[str] = field(default_factory=set)
    interval_s: float = DEFAULT_INTERVAL_S
    _failures: int = field(default=0, init=False)
    _next_poll: datetime | None = field(default=None, init=False)

    def tick(self) -> None:
        now = self.clock()
        if self._next_poll is not None and now < self._next_poll:
            return
        try:
            prompts = self.fetch()
        except (OSError, ValueError) as exc:
            # A failed fetch deliberately leaves seen/reminded untouched: an
            # outage must neither prune state nor cause re-popups on recovery.
            self._on_unreachable(now, exc)
            return
        self._failures = 0
        self._next_poll = None
        self._handle(prompts, now)

    def _on_unreachable(self, now: datetime, exc: Exception) -> None:
        first = self._failures == 0
        # Record back-off state BEFORE the popup so a failing notify is never
        # retried on every tick.
        # Capped counter: 2**n must stay float-convertible on very long outages.
        self._failures = min(self._failures + 1, 30)
        delay = min(self.interval_s * 2**self._failures, MAX_BACKOFF_S)
        self._next_poll = now + timedelta(seconds=delay)
        if first:
            self.notify(
                "shell-runner-notify: server unreachable",
                f"POST /pending failed: {clean(str(exc), CMD_LIMIT)}",
                "normal",
            )

    def _handle(self, prompts: list[Any], now: datetime) -> None:
        live: set[str] = set()
        for prompt in prompts:
            if not isinstance(prompt, dict):
                _log(f"skipping non-object prompt entry: {repr(prompt)[:80]}")
                continue
            # notify() failures (notify-send/xdg-open OSError) are handled inside
            # DesktopNotifier (Task 6), so they are deliberately not caught here.
            try:
                self._handle_one(prompt, now, live)
            except (KeyError, TypeError, ValueError) as exc:
                _log(f"skipping malformed prompt {prompt.get('id')}: {exc!r}")
        self.seen &= live
        self.reminded &= live

    def _handle_one(self, prompt: dict[str, Any], now: datetime, live: set[str]) -> None:
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
            if remaining <= REMIND_BEFORE_S:
                self.reminded.add(prompt["id"])
            self._popup(prompt, remaining, "approval needed")
        elif prompt["id"] not in self.reminded and remaining <= REMIND_BEFORE_S:
            self.reminded.add(prompt["id"])
            self._popup(prompt, remaining, "approval expiring")

    def _popup(self, prompt: dict[str, Any], remaining: float, what: str) -> None:
        tier = int(prompt["command_tier"])
        urgency = "critical" if tier == 4 else "normal"
        title = f"shell-runner: T{tier} {what}"
        self.notify(title, build_body(prompt, remaining, self.home), urgency)


def _read_bounded(response: http.client.HTTPResponse, deadline: float) -> bytes:
    """Read up to MAX_RESPONSE_BYTES + 1 bytes in chunks before `deadline`."""
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_RESPONSE_BYTES:
        chunk = response.read1(min(READ_CHUNK_BYTES, MAX_RESPONSE_BYTES + 1 - total))
        if not chunk:
            if response.length:  # EOF before Content-Length bytes arrived
                raise http.client.IncompleteRead(b"".join(chunks), response.length)
            break
        chunks.append(chunk)
        total += len(chunk)
        if time.monotonic() > deadline:
            raise OSError("/pending read deadline exceeded")
    return b"".join(chunks)


def http_fetch(url: str, timeout: float = 5.0) -> list[dict[str, Any]]:
    """POST `{}` to /pending and return the prompt list.

    Raises OSError (incl. URLError/HTTPError/timeout, or a non-200 status) when
    unreachable and ValueError on a non-http(s) URL, an oversized or malformed
    response; `Notifier.tick` treats both as "server unreachable".
    """
    scheme = urllib.parse.urlsplit(url).scheme
    if scheme not in ("http", "https"):
        # Scheme only: the URL may embed credentials that would reach the popup.
        raise ValueError(f"unsupported URL scheme: {scheme!r}")
    request = urllib.request.Request(
        url,
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # urlopen's timeout applies per socket operation, so bound the whole read too.
    deadline = time.monotonic() + timeout
    try:
        # B310 suppressed below: the scheme was restricted to http/https above.
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            if response.status != 200:
                raise OSError(f"unexpected HTTP status {response.status}")
            raw = _read_bounded(response, deadline)
    except http.client.HTTPException as exc:
        raise OSError(f"HTTP protocol error: {exc!r}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("/pending response too large")
    data = json.loads(raw)
    prompts = data.get("prompts") if isinstance(data, dict) else None
    if not isinstance(prompts, list):
        raise ValueError("unexpected /pending response")
    return prompts
