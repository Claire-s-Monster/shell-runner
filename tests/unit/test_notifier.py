"""Unit tests for the desktop approval notifier (issue #136).

Fetch, notify, run and clock are injected, so nothing here touches the
network, the desktop or real time.
"""

from __future__ import annotations

import json
import socket
import threading
import types
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from shell_runner.notifier import (
    MAX_RESPONSE_BYTES,
    Notifier,
    build_body,
    clean,
    format_remaining,
    http_fetch,
    shorten_home,
)


def test_clean_escapes_markup() -> None:
    assert clean("<b>x</b> & y") == "&lt;b&gt;x&lt;/b&gt; &amp; y"


def test_clean_strips_control_characters() -> None:
    assert clean("a\x1b[31mb\x00c\x7f") == "a[31mbc"


def test_clean_drops_bidi_and_line_separator_characters() -> None:
    text = "a\N{RIGHT-TO-LEFT OVERRIDE}b\N{LINE SEPARATOR}c\N{PARAGRAPH SEPARATOR}d"
    assert clean(text) == "abcd"


def test_clean_turns_newlines_and_tabs_into_spaces() -> None:
    assert clean("a\nb\tc\r") == "a b c "


def test_clean_turns_nel_into_space() -> None:
    assert clean("a\x85b") == "a b"


def test_clean_truncates_long_text_with_marker() -> None:
    assert clean("x" * 121, 120) == "x" * 120 + "… (truncated)"


def test_clean_leaves_text_at_limit_unmarked() -> None:
    assert clean("x" * 120, 120) == "x" * 120


def test_clean_truncates_before_escaping() -> None:
    assert clean("&" * 130, 120) == "&amp;" * 120 + "… (truncated)"


def test_shorten_home() -> None:
    assert shorten_home("/home/me/proj", "/home/me") == "~/proj"
    assert shorten_home("/home/me", "/home/me") == "~"
    assert shorten_home("/home/mex/proj", "/home/me") == "/home/mex/proj"
    assert shorten_home("/srv/x", "/home/me") == "/srv/x"


def test_shorten_home_with_empty_or_root_home_leaves_cwd_unchanged() -> None:
    assert shorten_home("/home/me/proj", "") == "/home/me/proj"
    assert shorten_home("/home/me/proj", "/") == "/home/me/proj"


def test_format_remaining() -> None:
    assert format_remaining(299) == "4m 59s"
    assert format_remaining(5) == "0m 5s"
    assert format_remaining(-3) == "0m 0s"


def test_build_body_order_and_content() -> None:
    prompt = {
        "agent_id": "a1",
        "cwd": "/home/me/proj",
        "raw_cmd": "curl -sI https://example.com",
        "scope_warning": "template is broader than the command",
    }
    body = build_body(prompt, 299, "/home/me")
    assert body.splitlines() == [
        "template is broader than the command",
        "agent: a1",
        "cwd: ~/proj",
        "cmd: curl -sI https://example.com",
        "expires in 4m 59s",
        "decide on the Pending Prompts page",
    ]


def test_build_body_without_scope_warning_starts_with_agent() -> None:
    prompt = {"agent_id": "a1", "cwd": "/x", "raw_cmd": "ls", "scope_warning": None}
    assert build_body(prompt, 10, "/home/me").splitlines()[0] == "agent: a1"


def test_build_body_without_scope_warning_key_starts_with_agent() -> None:
    prompt = {"agent_id": "a1", "cwd": "/x", "raw_cmd": "ls"}
    assert build_body(prompt, 10, "/home/me").splitlines()[0] == "agent: a1"


def test_build_body_sanitises_every_untrusted_field() -> None:
    prompt = {
        "agent_id": "<b>a</b>\x1b",
        "cwd": "/tmp/&x\x00",
        "raw_cmd": "echo <i>&\x07",
        "scope_warning": "<u>w</u>",
    }
    body = build_body(prompt, 10, "/home/me")
    assert "<" not in body
    assert "\x1b" not in body
    assert "\x00" not in body
    assert "\x07" not in body
    assert "agent: &lt;b&gt;a&lt;/b&gt;" in body
    assert "cmd: echo &lt;i&gt;&amp;" in body
    assert body.splitlines()[0] == "&lt;u&gt;w&lt;/u&gt;"


def test_build_body_marks_truncation_only_when_cut() -> None:
    long_cmd = {"agent_id": "a", "cwd": "/x", "raw_cmd": "y" * 200}
    short_cmd = {"agent_id": "a", "cwd": "/x", "raw_cmd": "y" * 20}
    assert "… (truncated)" in build_body(long_cmd, 10, "/h")
    assert "… (truncated)" not in build_body(short_cmd, 10, "/h")


def test_build_body_tolerates_missing_fields() -> None:
    body = build_body({}, 10, "/home/me")
    assert body.splitlines() == [
        "agent: ?",
        "cwd: ?",
        "cmd: ?",
        "expires in 0m 10s",
        "decide on the Pending Prompts page",
    ]


def test_build_body_tolerates_none_fields() -> None:
    prompt = {"agent_id": None, "cwd": None, "raw_cmd": None, "scope_warning": None}
    body = build_body(prompt, 10, "/home/me")
    assert "agent: ?" in body.splitlines()
    assert "cwd: ?" in body.splitlines()
    assert "cmd: ?" in body.splitlines()


NOW = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Feed:
    """Injected fetcher: returns `prompts`, or raises `error` when set."""

    def __init__(self) -> None:
        self.prompts: list[dict] = []
        self.error: Exception | None = None
        self.calls = 0

    def __call__(self) -> list[dict]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.prompts)


def make_prompt(pid: str = "p1", tier: int = 3, expires_in: float = 300, **extra) -> dict:
    return {
        "id": pid,
        "agent_id": "agent-1",
        "cwd": "/home/me/proj",
        "raw_cmd": "curl -sI https://example.com",
        "command_tier": tier,
        "expires_at": (NOW + timedelta(seconds=expires_in)).isoformat(),
        "approve_decision": None,
        "scope_warning": None,
        **extra,
    }


def make_notifier() -> tuple[Notifier, Feed, Clock, list[tuple[str, str, str]]]:
    feed, clock = Feed(), Clock()
    popups: list[tuple[str, str, str]] = []
    notifier = Notifier(
        fetch=feed,
        notify=lambda title, body, urgency: popups.append((title, body, urgency)),
        clock=clock,
        home="/home/me",
    )
    return notifier, feed, clock, popups


def test_new_prompt_gives_one_popup() -> None:
    notifier, feed, _clock, popups = make_notifier()
    feed.prompts = [make_prompt()]
    notifier.tick()
    assert len(popups) == 1
    title, body, urgency = popups[0]
    assert title == "shell-runner: T3 approval needed"
    assert urgency == "normal"
    assert "cwd: ~/proj" in body


def test_same_id_is_not_repeated() -> None:
    notifier, feed, clock, popups = make_notifier()
    feed.prompts = [make_prompt()]
    for _ in range(3):
        notifier.tick()
        clock.advance(3)
    assert len(popups) == 1


def test_second_prompt_gets_its_own_popup() -> None:
    notifier, feed, _clock, popups = make_notifier()
    feed.prompts = [make_prompt("p1")]
    notifier.tick()
    feed.prompts = [make_prompt("p1"), make_prompt("p2")]
    notifier.tick()
    assert len(popups) == 2


def test_t4_is_critical() -> None:
    notifier, feed, _clock, popups = make_notifier()
    feed.prompts = [make_prompt(tier=4)]
    notifier.tick()
    assert popups[0][0] == "shell-runner: T4 approval needed"
    assert popups[0][2] == "critical"


def test_scope_warning_is_first_line_of_body() -> None:
    notifier, feed, _clock, popups = make_notifier()
    feed.prompts = [make_prompt(scope_warning="broad template")]
    notifier.tick()
    assert popups[0][1].splitlines()[0] == "broad template"


def test_expired_prompts_are_skipped() -> None:
    notifier, feed, _clock, popups = make_notifier()
    feed.prompts = [make_prompt(expires_in=-1), make_prompt("p2", expires_in=0)]
    notifier.tick()
    assert popups == []


def test_decided_prompts_are_skipped() -> None:
    notifier, feed, _clock, popups = make_notifier()
    feed.prompts = [make_prompt(approve_decision="approve_once")]
    notifier.tick()
    assert popups == []


def test_unparseable_expiry_is_skipped_not_fatal() -> None:
    notifier, feed, _clock, popups = make_notifier()
    feed.prompts = [make_prompt(expires_at="garbage"), make_prompt("p2")]
    notifier.tick()
    assert len(popups) == 1


def test_malformed_prompt_does_not_kill_the_poll_loop() -> None:
    notifier, feed, _clock, popups = make_notifier()
    broken = make_prompt("bad", tier="not-a-number")  # type: ignore[arg-type]
    feed.prompts = [broken, make_prompt("p2")]
    notifier.tick()  # must not raise
    assert len(popups) == 1
    assert "agent: agent-1" in popups[0][1]
    notifier.tick()  # the bad id is not retried
    assert len(popups) == 1


def test_non_dict_entry_is_skipped_not_fatal() -> None:
    notifier, feed, _clock, popups = make_notifier()
    feed.prompts = ["oops", None, make_prompt("p2")]  # type: ignore[list-item]
    notifier.tick()  # must not raise
    assert len(popups) == 1


def test_seen_is_pruned_when_prompt_disappears() -> None:
    notifier, feed, clock, _popups = make_notifier()
    feed.prompts = [make_prompt("p1"), make_prompt("p2")]
    notifier.tick()
    assert notifier.seen == {"p1", "p2"}
    feed.prompts = [make_prompt("p1"), make_prompt("p2", approve_decision="approve_once")]
    clock.advance(3)
    notifier.tick()
    assert notifier.seen == {"p1"}
    feed.prompts = []
    notifier.tick()
    assert notifier.seen == set()


def test_reminder_fires_once_about_60s_before_expiry() -> None:
    notifier, feed, clock, popups = make_notifier()
    feed.prompts = [make_prompt(expires_in=300)]
    notifier.tick()  # 300s left: initial popup only
    clock.advance(239)  # 61s left
    notifier.tick()
    assert len(popups) == 1
    clock.advance(1)  # 60s left
    notifier.tick()
    assert len(popups) == 2
    assert popups[1][0] == "shell-runner: T3 approval expiring"
    assert "expires in 1m 0s" in popups[1][1]
    clock.advance(3)
    notifier.tick()
    clock.advance(3)
    notifier.tick()
    assert len(popups) == 2  # once only


def test_no_reminder_when_prompt_was_decided() -> None:
    notifier, feed, clock, popups = make_notifier()
    feed.prompts = [make_prompt(expires_in=300)]
    notifier.tick()
    feed.prompts = []  # /pending no longer lists decided prompts
    clock.advance(245)
    notifier.tick()
    feed.prompts = [make_prompt(expires_in=300, approve_decision="deny")]
    notifier.tick()
    assert len(popups) == 1


def test_no_second_reminder_when_first_seen_inside_window() -> None:
    notifier, feed, clock, popups = make_notifier()
    feed.prompts = [make_prompt(expires_in=40)]
    notifier.tick()
    clock.advance(3)
    notifier.tick()
    assert len(popups) == 1  # initial popup already doubles as the reminder


def test_t4_reminder_is_critical() -> None:
    notifier, feed, clock, popups = make_notifier()
    feed.prompts = [make_prompt(tier=4, expires_in=300)]
    notifier.tick()
    clock.advance(245)
    notifier.tick()
    assert popups[1][0] == "shell-runner: T4 approval expiring"
    assert popups[1][2] == "critical"


def test_no_reminder_after_expiry() -> None:
    notifier, feed, clock, popups = make_notifier()
    feed.prompts = [make_prompt(expires_in=300)]
    notifier.tick()
    clock.advance(301)
    notifier.tick()
    assert len(popups) == 1


def test_reminded_is_pruned_when_prompt_disappears() -> None:
    notifier, feed, clock, _popups = make_notifier()
    feed.prompts = [make_prompt("p1"), make_prompt("p2")]
    notifier.tick()
    clock.advance(245)
    notifier.tick()
    assert notifier.reminded == {"p1", "p2"}
    feed.prompts = [make_prompt("p1", expires_in=300)]
    notifier.tick()
    assert notifier.reminded == {"p1"}
    feed.prompts = []
    notifier.tick()
    assert notifier.reminded == set()


def test_failed_reminder_popup_is_not_retried() -> None:
    notifier, feed, clock, popups = make_notifier()
    feed.prompts = [make_prompt(expires_in=300)]
    notifier.tick()
    feed.prompts[0]["command_tier"] = "not-a-number"
    clock.advance(245)
    notifier.tick()  # reminder popup raises ValueError, which is caught
    feed.prompts[0]["command_tier"] = 3
    clock.advance(3)
    notifier.tick()
    assert len(popups) == 1


def test_prompt_missing_raw_cmd_still_gets_a_popup() -> None:
    notifier, feed, _clock, popups = make_notifier()
    broken = make_prompt("bad")
    del broken["raw_cmd"]
    feed.prompts = [broken, make_prompt("p2")]
    notifier.tick()
    assert len(popups) == 2
    assert "cmd: ?" in popups[0][1].splitlines()


def test_unreachable_gives_one_popup_then_backs_off() -> None:
    notifier, feed, clock, popups = make_notifier()
    feed.error = ConnectionRefusedError("down")
    notifier.tick()
    assert len(popups) == 1
    assert popups[0][0] == "shell-runner-notify: server unreachable"
    assert feed.calls == 1
    clock.advance(3)
    notifier.tick()  # inside the 6s back-off window: no fetch
    assert feed.calls == 1
    clock.advance(3)
    notifier.tick()  # window over: retries quietly
    assert feed.calls == 2
    assert len(popups) == 1


def test_backoff_grows_and_is_capped() -> None:
    notifier, feed, clock, _popups = make_notifier()
    feed.error = OSError("down")
    for _ in range(12):
        notifier.tick()
        clock.advance(61)
    clock.advance(0)
    calls = feed.calls
    notifier.tick()
    clock.advance(59)
    notifier.tick()
    assert feed.calls == calls + 1  # cap is 60s


def test_very_long_outage_does_not_overflow() -> None:
    notifier, feed, clock, popups = make_notifier()
    feed.error = OSError("down")
    for _ in range(1200):
        notifier.tick()
        clock.advance(61)
    assert feed.calls == 1200
    assert len(popups) == 1


def test_recovery_resets_and_a_new_outage_notifies_again() -> None:
    notifier, feed, clock, popups = make_notifier()
    feed.error = OSError("down")
    notifier.tick()
    feed.error = None
    clock.advance(10)
    notifier.tick()  # recovered: quiet (no "reconnected" popup)
    assert len(popups) == 1
    feed.error = OSError("down again")
    clock.advance(3)
    notifier.tick()
    assert len(popups) == 2


def test_prompts_are_not_lost_across_an_outage() -> None:
    notifier, feed, clock, popups = make_notifier()
    feed.error = OSError("down")
    notifier.tick()
    feed.error = None
    feed.prompts = [make_prompt()]
    clock.advance(10)
    notifier.tick()
    assert [p[0] for p in popups] == [
        "shell-runner-notify: server unreachable",
        "shell-runner: T3 approval needed",
    ]


def test_value_error_from_bad_response_counts_as_unreachable() -> None:
    notifier, feed, _clock, popups = make_notifier()
    feed.error = ValueError("not json")
    notifier.tick()
    assert len(popups) == 1


def test_outage_keeps_seen_and_reminded_and_does_not_repopup_after_recovery() -> None:
    notifier, feed, clock, popups = make_notifier()
    feed.prompts = [make_prompt("p1", expires_in=300), make_prompt("p2", expires_in=50)]
    notifier.tick()
    assert notifier.seen == {"p1", "p2"}
    assert notifier.reminded == {"p2"}
    assert len(popups) == 2
    feed.error = OSError("down")
    clock.advance(3)
    notifier.tick()
    assert notifier.seen == {"p1", "p2"}  # a failed fetch must not prune
    assert notifier.reminded == {"p2"}
    feed.error = None
    clock.advance(10)
    notifier.tick()  # recovered with the same prompts: no re-popups
    assert [p[0] for p in popups].count("shell-runner: T3 approval needed") == 2
    assert len(popups) == 3  # 2 prompts + 1 unreachable, nothing repeated
    assert notifier.seen == {"p1", "p2"}
    assert notifier.reminded == {"p2"}


def test_failed_unreachable_popup_is_not_retried_every_tick() -> None:
    feed, clock = Feed(), Clock()
    calls: list[str] = []

    def boom(title: str, _body: str, _urgency: str) -> None:
        calls.append(title)
        raise OSError("notify-send missing")

    notifier = Notifier(fetch=feed, notify=boom, clock=clock, home="/home/me")
    feed.error = OSError("down")
    with pytest.raises(OSError, match="notify-send missing"):
        notifier.tick()
    clock.advance(6)
    notifier.tick()  # still failing: back-off state was recorded before the popup
    assert len(calls) == 1
    assert feed.calls == 2


def _serve(payload: bytes, status: int = 200) -> tuple[HTTPServer, dict[str, Any]]:
    seen: dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 (http.server handler naming)
            seen["path"] = self.path
            seen["method"] = self.command
            seen["body"] = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, seen


def _url(server: HTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_port}/pending"


def test_http_fetch_posts_empty_json_and_returns_prompts() -> None:
    server, seen = _serve(json.dumps({"prompts": [{"id": "p1"}]}).encode())
    try:
        assert http_fetch(_url(server)) == [{"id": "p1"}]
    finally:
        server.shutdown()
        server.server_close()
    assert seen["method"] == "POST"
    assert seen["path"] == "/pending"
    assert json.loads(seen["body"]) == {}


def test_http_fetch_rejects_unexpected_payload_as_value_error() -> None:
    server, _seen = _serve(b'{"nope": 1}')
    try:
        with pytest.raises(ValueError, match="unexpected"):
            http_fetch(_url(server))
    finally:
        server.shutdown()
        server.server_close()


def test_http_fetch_connection_refused_is_oserror() -> None:
    # Bound but never listening: the port stays reserved for the whole call,
    # and Linux answers connect() with ECONNREFUSED.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        with pytest.raises(OSError):  # noqa: PT011
            http_fetch(f"http://127.0.0.1:{port}/pending", timeout=1)


def test_http_fetch_malformed_json_is_value_error() -> None:
    server, _seen = _serve(b"not json")
    try:
        with pytest.raises(ValueError):  # noqa: PT011
            http_fetch(_url(server))
    finally:
        server.shutdown()
        server.server_close()


def test_http_fetch_non_200_is_oserror() -> None:
    server, _seen = _serve(b'{"prompts": []}', status=500)
    try:
        with pytest.raises(OSError):  # noqa: PT011
            http_fetch(_url(server))
    finally:
        server.shutdown()
        server.server_close()


def test_http_fetch_non_200_success_status_is_oserror() -> None:
    server, _seen = _serve(b'{"prompts": []}', status=202)
    try:
        with pytest.raises(OSError, match="202"):
            http_fetch(_url(server))
    finally:
        server.shutdown()
        server.server_close()


def test_http_fetch_oversized_response_is_value_error() -> None:
    server, _seen = _serve(b" " * (MAX_RESPONSE_BYTES + 1))
    try:
        with pytest.raises(ValueError, match="too large"):
            http_fetch(_url(server))
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://127.0.0.1/pending", "/pending"])
def test_http_fetch_rejects_non_http_schemes(url: str) -> None:
    with pytest.raises(ValueError, match="scheme"):
        http_fetch(url)


def test_http_fetch_sends_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(_request: object, timeout: float | None = None) -> None:
        captured["timeout"] = timeout
        raise OSError("stop")

    monkeypatch.setattr("shell_runner.notifier.urllib.request.urlopen", fake_urlopen)
    with pytest.raises(OSError, match="stop"):
        http_fetch("http://127.0.0.1:1/pending", timeout=2.5)
    assert captured["timeout"] == 2.5


def _serve_truncated() -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 (http.server handler naming)
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b'{"prompts"')
            self.wfile.flush()
            self.close_connection = True

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_http_fetch_truncated_body_is_oserror() -> None:
    server = _serve_truncated()
    try:
        with pytest.raises(OSError, match="protocol error"):
            http_fetch(_url(server))
    finally:
        server.shutdown()
        server.server_close()


def test_tick_takes_unreachable_path_on_truncated_body() -> None:
    server = _serve_truncated()
    popups: list[str] = []
    try:
        notifier = Notifier(
            fetch=lambda: http_fetch(_url(server)),
            notify=lambda title, _body, _urgency: popups.append(title),
            home="/home/me",
        )
        notifier.tick()
    finally:
        server.shutdown()
        server.server_close()
    assert popups == ["shell-runner-notify: server unreachable"]


def test_http_fetch_enforces_overall_read_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 (http.server handler naming)
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            try:
                self.wfile.write(b"x")
                self.wfile.flush()
                release.wait(2)
            except OSError:
                pass
            self.close_connection = True

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    clock = iter([0.0, 100.0])  # deadline computed at 0.0; first chunk lands at 100.0
    monkeypatch.setattr(
        "shell_runner.notifier.time", types.SimpleNamespace(monotonic=lambda: next(clock))
    )
    try:
        with pytest.raises(OSError, match="read deadline exceeded"):
            http_fetch(_url(server), timeout=1)
    finally:
        release.set()
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("url", ["ftp://user:pw@host/x", "user:pw@host/x"])
def test_http_fetch_scheme_error_does_not_leak_credentials(url: str) -> None:
    with pytest.raises(ValueError, match="scheme") as excinfo:
        http_fetch(url)
    message = str(excinfo.value)
    assert "pw" not in message
    assert "user" not in message
    assert "host" not in message


def test_http_fetch_deeply_nested_json_is_value_error() -> None:
    server, _seen = _serve(b"[" * 100_000)
    try:
        with pytest.raises(ValueError, match="too deeply nested"):
            http_fetch(_url(server))
    finally:
        server.shutdown()
        server.server_close()


def test_http_fetch_failures_are_what_tick_treats_as_unreachable() -> None:
    server, _seen = _serve(b"not json")
    popups: list[str] = []
    try:
        notifier = Notifier(
            fetch=lambda: http_fetch(_url(server)),
            notify=lambda title, _body, _urgency: popups.append(title),
            home="/home/me",
        )
        notifier.tick()
    finally:
        server.shutdown()
        server.server_close()
    assert popups == ["shell-runner-notify: server unreachable"]
