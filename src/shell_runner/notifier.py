"""Polling core of the shell-runner approval tray (issues #136, #138).

Polls the read-only ``POST /pending`` endpoint and hands every fetch result to
an ``on_update(Snapshot)`` callback; ``shell_runner.tray`` turns that into a
silent panel icon. Standard library only. It never approves anything:
``/pending`` returns no tokens and the tray only opens the UI page.
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import re
import subprocess  # nosec B404
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

MAX_BACKOFF_S = 60.0
DEFAULT_INTERVAL_S = 3.0
# Bounds keep the wait from overflowing and back-off from collapsing to zero.
MIN_INTERVAL_S = 0.1
MAX_INTERVAL_S = 3600.0
DEFAULT_MAX_ITEMS = 10
DEFAULT_PENDING_URL = "http://127.0.0.1:4111/pending"
DEFAULT_UI_URL = "http://127.0.0.1:8511/Pending_Prompts"
MAX_RESPONSE_BYTES = 1_048_576
READ_CHUNK_BYTES = 8192
ELLIPSIS = "…"
MAX_JSON_DEPTH = 64
_JSON_TOKEN = re.compile(rb'"(?:[^"\\]|\\.)*"|[\[{]|[\]}]')
_WHITESPACE_CTRL = re.compile(r"[\n\r\t\x85]")
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def clean(text: str, limit: int | None = None) -> str:
    """Return plain text: control/format characters dropped, optionally cut to `limit`.

    Drops Unicode categories Cc, Cf (e.g. U+202E bidi override), Zl and Zp
    (U+2028/2029). Newlines, tabs and NEL (U+0085) become spaces so multi-line
    commands stay readable. Nothing is escaped: the tray shows plain text, not
    Pango markup. When cut, the result is `limit` characters plus a single "…".
    The limit is applied after cleaning. `limit=None` means no cut and "" stays
    "". `limit=0` gives "" for empty cleaned text, otherwise just "…". A negative
    `limit` raises ValueError. Dropping format characters also removes ZWJ
    (U+200D), so emoji sequences can split.
    """
    if limit is not None and limit < 0:
        raise ValueError(f"limit must be >= 0, got {limit}")
    text = _WHITESPACE_CTRL.sub(" ", text)
    text = "".join(c for c in text if unicodedata.category(c) not in _DROPPED_CATEGORIES)
    if limit is not None and len(text) > limit:
        return text[:limit] + ELLIPSIS
    return text


def format_left(seconds: float) -> str:
    """Time left for a menu row: `<1m left`, `Nm left` or `Nh Mm left`."""
    total = max(0, int(seconds))
    if total < 60:
        return "<1m left"
    if total < 3600:
        return f"{total // 60}m left"
    return f"{total // 3600}h {total % 3600 // 60}m left"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _log(message: str) -> None:
    # Escape non-printable characters (newlines etc.) so a hostile prompt id or
    # field cannot forge extra journal lines.
    safe = "".join(c if c.isprintable() else c.encode("unicode_escape").decode() for c in message)
    print(f"shell-runner-notify: {safe}", file=sys.stderr)


def parse_expiry(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Snapshot:
    """One fetch result: live prompts sorted by expiry, and whether the fetch worked.

    An "unreachable" snapshot carries the prompts of the last successful fetch.
    """

    prompts: tuple[dict[str, Any], ...]
    status: Literal["ok", "unreachable"]


@dataclass
class Notifier:
    """Polling core. `fetch()` returns prompt dicts or raises OSError/ValueError;
    `on_update(snapshot)` receives every fetch result; `clock()` returns aware UTC."""

    fetch: Callable[[], list[dict[str, Any]]]
    on_update: Callable[[Snapshot], None]
    clock: Callable[[], datetime] = _utcnow
    interval_s: float = DEFAULT_INTERVAL_S
    last_prompts: list[dict[str, Any]] = field(default_factory=list, init=False)
    _failures: int = field(default=0, init=False)
    _next_poll: datetime | None = field(default=None, init=False)

    def tick(self) -> None:
        now = self.clock()
        if self._next_poll is not None and now < self._next_poll:
            return  # skipped by back-off: no fetch and no on_update
        try:
            prompts = self.fetch()
        except (OSError, ValueError) as exc:
            # A ValueError from http_fetch (bad scheme, oversize, too deep, wrong
            # shape, bad JSON) takes the same path as a network error.
            self._on_unreachable(now, exc)
            self.on_update(Snapshot(prompts=tuple(self.last_prompts), status="unreachable"))
            return
        self._failures = 0
        self._next_poll = None
        # Re-read the clock: a slow fetch must not leave an expired prompt looking live.
        self.last_prompts = self._handle(prompts, self.clock())
        self.on_update(Snapshot(prompts=tuple(self.last_prompts), status="ok"))

    def _on_unreachable(self, now: datetime, exc: Exception) -> None:
        # Back-off state is recorded before on_update runs, so a failing
        # callback can never make the loop poll on every tick.
        # Capped counter: 2**n must stay float-convertible on very long outages.
        self._failures = min(self._failures + 1, 30)
        delay = min(self.interval_s * 2**self._failures, MAX_BACKOFF_S)
        self._next_poll = now + timedelta(seconds=delay)
        _log(f"POST /pending failed: {exc}")

    def _handle(self, prompts: list[Any], now: datetime) -> list[dict[str, Any]]:
        """Return the live prompt dicts sorted by parsed ``expires_at`` (soonest first)."""
        live: list[tuple[datetime, dict[str, Any]]] = []
        for prompt in prompts:
            if not isinstance(prompt, dict):
                _log(f"skipping non-object prompt entry: {repr(prompt)[:80]}")
                continue
            try:
                expiry = self._handle_one(prompt, now)
            except (KeyError, TypeError, ValueError) as exc:
                _log(f"skipping malformed prompt {prompt.get('id')}: {exc!r}")
                continue
            if expiry is not None:
                live.append((expiry, prompt))
        live.sort(key=lambda pair: pair[0])  # stable: equal expiries keep server order
        return [prompt for _expiry, prompt in live]

    def _handle_one(self, prompt: dict[str, Any], now: datetime) -> datetime | None:
        """Return the expiry of a live prompt; None if expired, decided or unparseable."""
        if "id" not in prompt:
            raise KeyError("id")
        expiry = parse_expiry(prompt.get("expires_at"))
        if expiry is None:
            _log(f"skipping prompt {prompt.get('id')}: bad expires_at")
            return None
        if expiry <= now or prompt.get("approve_decision") is not None:
            return None
        return expiry


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


def _json_depth_exceeds(raw: bytes, limit: int) -> bool:
    """Return True if bracket nesting in ``raw`` goes deeper than ``limit``, ignoring strings."""
    depth = 0
    for match in _JSON_TOKEN.finditer(raw):
        token = match.group()
        if token.startswith(b'"'):
            continue
        depth += 1 if token in (b"[", b"{") else -1
        if depth > limit:
            return True
    return False


def http_fetch(url: str, timeout: float = 5.0) -> list[dict[str, Any]]:
    """POST `{}` to /pending and return the prompt list.

    Raises OSError (incl. URLError/HTTPError/timeout, or a non-200 status) when
    unreachable and ValueError on a non-http(s) URL, an oversized or malformed
    response; `Notifier.tick` treats both as "server unreachable".
    """
    scheme = urllib.parse.urlsplit(url).scheme
    if scheme not in ("http", "https"):
        # Fixed text only: a scheme-less URL such as "user:pw@host" parses its
        # username as the scheme, and any part of the URL could reach the log.
        raise ValueError("unsupported URL scheme (only http and https are allowed)")
    request = urllib.request.Request(
        url,
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # urlopen's timeout applies per socket operation, so bound the whole read too.
    deadline = time.monotonic() + timeout
    try:
        # B310 and the Semgrep dynamic-urllib rule are suppressed below: the
        # scheme was restricted to http/https above, so file:// etc. is rejected.
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            if response.status != 200:
                raise OSError(f"unexpected HTTP status {response.status}")
            raw = _read_bounded(response, deadline)
    except http.client.HTTPException as exc:
        raise OSError(f"HTTP protocol error: {exc!r}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("/pending response too large")
    if _json_depth_exceeds(raw, MAX_JSON_DEPTH):
        raise ValueError("response JSON too deeply nested")
    try:
        data = json.loads(raw)
    except RecursionError as exc:
        raise ValueError("response JSON too deeply nested") from exc
    prompts = data.get("prompts") if isinstance(data, dict) else None
    if not isinstance(prompts, list):
        raise ValueError("unexpected /pending response")
    return prompts


def spawn_daemon(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


def open_detached(argv: list[str]) -> None:
    proc = subprocess.Popen(  # nosec B603
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    def reap() -> None:
        proc.wait()

    spawn_daemon(reap)  # reap the child so it does not linger as a zombie


def run_loop(notifier: Notifier, stop: threading.Event, interval: float) -> None:
    """Tick, then wait `interval` seconds (interruptible); return once `stop` is set."""
    while not stop.is_set():
        notifier.tick()
        stop.wait(interval)


def _http_url(value: str) -> str:
    try:
        parts = urllib.parse.urlsplit(value)
        host = parts.hostname
    except ValueError:
        host = None
        parts = None
    if parts is None or parts.scheme.lower() not in {"http", "https"} or not host:
        raise argparse.ArgumentTypeError("not an http(s) URL with a host")
    return value


def _positive_interval(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("interval must be a number") from None
    if not math.isfinite(seconds) or not MIN_INTERVAL_S <= seconds <= MAX_INTERVAL_S:
        raise argparse.ArgumentTypeError(
            f"interval must be between {MIN_INTERVAL_S} and {MAX_INTERVAL_S} seconds"
        )
    return seconds


def _max_items(value: str) -> int:
    try:
        count = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("max-items must be an integer") from None
    if count < 1:
        raise argparse.ArgumentTypeError("max-items must be at least 1")
    return count


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="shell-runner-tray",
        description="Silent tray indicator for pending shell-runner approval prompts (read-only).",
    )
    parser.add_argument(
        "--interval", type=_positive_interval, default=DEFAULT_INTERVAL_S, help="poll seconds"
    )
    parser.add_argument("--url", type=_http_url, default=DEFAULT_PENDING_URL, help="/pending URL")
    parser.add_argument(
        "--ui-url", type=_http_url, default=DEFAULT_UI_URL, help="opened by the menu items"
    )
    parser.add_argument(
        "--max-items",
        type=_max_items,
        default=DEFAULT_MAX_ITEMS,
        help="prompt rows shown in the menu",
    )
    return parser.parse_args(argv)
