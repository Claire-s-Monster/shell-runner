"""Desktop approval notifier for shell-runner (issue #136).

Polls the read-only ``POST /pending`` endpoint and raises one ``notify-send``
popup per new T3/T4 approval prompt. Standard library only. It never approves
anything: ``/pending`` returns no tokens and popups only inform.
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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

CMD_LIMIT = 120
REMIND_BEFORE_S = 60
MAX_BACKOFF_S = 60.0
DEFAULT_INTERVAL_S = 3.0
# Bounds keep time.sleep from overflowing and back-off from collapsing to zero.
MIN_INTERVAL_S = 0.1
MAX_INTERVAL_S = 3600.0
DEFAULT_PENDING_URL = "http://127.0.0.1:4111/pending"
DEFAULT_UI_URL = "http://127.0.0.1:8511/Pending_Prompts"
MAX_RESPONSE_BYTES = 1_048_576
READ_CHUNK_BYTES = 8192
ELLIPSIS = "…"
MAX_JSON_DEPTH = 64
_JSON_TOKEN = re.compile(rb'"(?:[^"\\]|\\.)*"|[\[{]|[\]}]')
_WHITESPACE_CTRL = re.compile(r"[\n\r\t\x85]")
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
_MISSING = "?"


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


def format_left(seconds: float) -> str:
    """Time left for a menu row: `<1m left`, `Nm left` or `Nh Mm left`."""
    total = max(0, int(seconds))
    if total < 60:
        return "<1m left"
    if total < 3600:
        return f"{total // 60}m left"
    return f"{total // 3600}h {total % 3600 // 60}m left"


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
        # Re-read the clock: a slow fetch must not leave an expired prompt looking live.
        self._handle(prompts, self.clock())

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
        # username as the scheme, and any part of the URL could reach the popup.
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


RUN_TIMEOUT_S = 10.0

# Called as run(argv) or run(argv, timeout=<seconds or None>).
Runner = Callable[..., tuple[int, str]]


def run_argv(argv: list[str], timeout: float | None = RUN_TIMEOUT_S) -> tuple[int, str]:
    """Run an argv list (never a shell); return (exit code, stdout).

    stderr is discarded. A timeout (None = wait forever) raises OSError.
    """
    try:
        proc = subprocess.run(  # nosec B603
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise OSError(f"{argv[0]} timed out after {timeout}s") from exc
    return proc.returncode, proc.stdout


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


@dataclass
class DesktopNotifier:
    """`notify(title, body, urgency)` implemented with notify-send / xdg-open.

    With `--action` support each popup waits for a click in its own daemon
    thread and opens `ui_url`; otherwise the URL is appended to the body.
    """

    ui_url: str
    run: Runner = run_argv
    spawn: Callable[[Callable[[], None]], None] = spawn_daemon
    opener: Callable[[list[str]], None] = open_detached
    _action_ok: bool | None = field(default=None, init=False)

    def __call__(self, title: str, body: str, urgency: str) -> None:
        base = ["notify-send", "--app-name=shell-runner", f"--urgency={urgency}"]
        if self._supports_action():
            argv = [*base, "--wait", "--action=open=Open", "--", title, body]
            self.spawn(lambda: self._wait_and_open(argv, base, title, body))
        else:
            self._plain(base, title, body)

    def _supports_action(self) -> bool:
        if self._action_ok is None:
            _code, out = self._run(["notify-send", "--help"])
            self._action_ok = "--action" in out
        return self._action_ok

    def _wait_and_open(self, argv: list[str], base: list[str], title: str, body: str) -> None:
        code, out = self._run(argv, timeout=None)  # --wait blocks until clicked
        if code != 0:
            self._action_ok = False
            self._plain(base, title, body)
        elif out.strip() == "open":
            try:
                self.opener(["xdg-open", self.ui_url])
            except OSError as exc:
                _log(f"xdg-open failed: {exc}")

    def _plain(self, base: list[str], title: str, body: str) -> None:
        self._run([*base, "--", title, f"{body}\n{clean(self.ui_url)}"])

    def _run(self, argv: list[str], timeout: float | None = RUN_TIMEOUT_S) -> tuple[int, str]:
        try:
            return self.run(argv, timeout=timeout)
        except OSError as exc:
            _log(f"{argv[0]} failed: {exc}")
            return 127, ""


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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="shell-runner-notify",
        description="Desktop popups for pending shell-runner approval prompts (read-only).",
    )
    parser.add_argument(
        "--interval", type=_positive_interval, default=DEFAULT_INTERVAL_S, help="poll seconds"
    )
    parser.add_argument("--url", type=_http_url, default=DEFAULT_PENDING_URL, help="/pending URL")
    parser.add_argument(
        "--ui-url", type=_http_url, default=DEFAULT_UI_URL, help="opened by the Open action"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    notifier = Notifier(
        fetch=lambda: http_fetch(args.url),
        notify=DesktopNotifier(ui_url=args.ui_url),
        interval_s=args.interval,
    )
    try:
        while True:
            notifier.tick()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
