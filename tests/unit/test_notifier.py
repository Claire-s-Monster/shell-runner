"""Unit tests for the polling core of the approval tray (issues #136, #138).

Fetch, on_update and clock are injected, so nothing here touches the
network, the desktop or real time.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import socket
import subprocess
import sys
import threading
import types
from datetime import UTC, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from shell_runner import notifier
from shell_runner.notifier import (
    DEFAULT_PENDING_URL,
    DEFAULT_UI_URL,
    MAX_RESPONSE_BYTES,
    Notifier,
    Snapshot,
    clean,
    format_left,
    http_fetch,
    open_detached,
    parse_args,
    run_loop,
    spawn_daemon,
)


def test_clean_does_not_escape_markup() -> None:
    assert clean("<b>x</b> & y") == "<b>x</b> & y"


def test_clean_still_strips_control_characters_around_markup() -> None:
    assert clean("<b>\x1bx\x00</b> &\x07 y") == "<b>x</b> & y"


def test_clean_strips_control_characters() -> None:
    assert clean("a\x1b[31mb\x00c\x7f") == "a[31mbc"


def test_clean_drops_bidi_and_line_separator_characters() -> None:
    text = "a\N{RIGHT-TO-LEFT OVERRIDE}b\N{LINE SEPARATOR}c\N{PARAGRAPH SEPARATOR}d"
    assert clean(text) == "abcd"


def test_clean_turns_newlines_and_tabs_into_spaces() -> None:
    assert clean("a\nb\tc\r") == "a b c "


def test_clean_turns_nel_into_space() -> None:
    assert clean("a\x85b") == "a b"


@pytest.mark.parametrize(
    ("length", "expected"),
    [
        (119, "x" * 119),
        (120, "x" * 120),
        (121, "x" * 120 + "…"),
    ],
)
def test_clean_limit_boundaries(length: int, expected: str) -> None:
    assert clean("x" * length, 120) == expected


def test_clean_negative_limit_raises() -> None:
    with pytest.raises(ValueError, match="limit"):
        clean("abc", -1)


def test_clean_limit_zero() -> None:
    assert clean("", 0) == ""
    assert clean("abc", 0) == "…"


def test_clean_applies_limit_after_cleaning() -> None:
    assert clean("a\x00" + "b" * 5, 6) == "abbbbb"
    assert clean("a\nbcd", 3) == "a b…"


def test_clean_without_limit_and_empty_string() -> None:
    assert clean("") == ""
    assert clean("x" * 500) == "x" * 500


def test_clean_cuts_long_text_with_a_single_ellipsis() -> None:
    assert clean("x" * 121, 120) == "x" * 120 + "…"
    assert clean("&" * 130, 120) == "&" * 120 + "…"


def test_clean_leaves_text_at_limit_unmarked() -> None:
    assert clean("x" * 120, 120) == "x" * 120
    assert clean("<" * 120, 120) == "<" * 120


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (-5, "<1m left"),
        (0, "<1m left"),
        (59.9, "<1m left"),
        (60, "1m left"),
        (240, "4m left"),
        (119, "1m left"),
        (3599, "59m left"),
        (3599.9, "59m left"),
        (3600, "1h 0m left"),
        (3661, "1h 1m left"),
        (7322, "2h 2m left"),
    ],
)
def test_format_left(seconds: float, expected: str) -> None:
    assert format_left(seconds) == expected


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


def make_notifier() -> tuple[Notifier, Feed, Clock, list[Snapshot]]:
    feed, clock = Feed(), Clock()
    snapshots: list[Snapshot] = []
    notifier_ = Notifier(fetch=feed, on_update=snapshots.append, clock=clock)
    return notifier_, feed, clock, snapshots


def ids(snapshot: Snapshot) -> list[str]:
    return [prompt["id"] for prompt in snapshot.prompts]


def test_live_prompt_is_published_in_an_ok_snapshot() -> None:
    notifier_, feed, _clock, snapshots = make_notifier()
    prompt = make_prompt()
    feed.prompts = [prompt]
    notifier_.tick()
    assert snapshots == [Snapshot(prompts=(prompt,), status="ok")]
    assert notifier_.last_prompts == (prompt,)


def test_snapshot_is_frozen() -> None:
    snapshot = Snapshot(prompts=(), status="ok")
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.status = "unreachable"  # type: ignore[misc]


def test_every_fetching_tick_publishes_a_snapshot_of_the_current_list() -> None:
    notifier_, feed, clock, snapshots = make_notifier()
    feed.prompts = [make_prompt("p1")]
    notifier_.tick()
    clock.advance(3)
    notifier_.tick()  # unchanged list still publishes
    feed.prompts = [make_prompt("p1"), make_prompt("p2")]
    notifier_.tick()
    feed.prompts = []
    notifier_.tick()
    assert [ids(s) for s in snapshots] == [["p1"], ["p1"], ["p1", "p2"], []]
    assert notifier_.last_prompts == ()


def test_snapshot_rows_are_read_only() -> None:
    notifier_, feed, _clock, snapshots = make_notifier()
    feed.prompts = [make_prompt("p1")]
    notifier_.tick()
    row = snapshots[0].prompts[0]
    with pytest.raises(TypeError):
        row["id"] = "x"  # type: ignore[index]
    with pytest.raises(TypeError):
        notifier_.last_prompts[0]["id"] = "x"  # type: ignore[index]
    assert row["id"] == "p1"
    assert row.get("agent_id") == "agent-1"


def test_server_dict_mutated_after_tick_does_not_change_last_prompts() -> None:
    notifier_, feed, _clock, snapshots = make_notifier()
    server_prompt = make_prompt("p1")
    feed.prompts = [server_prompt]
    notifier_.tick()
    server_prompt["id"] = "mutated"
    server_prompt["extra"] = 1
    assert notifier_.last_prompts[0]["id"] == "p1"
    assert "extra" not in notifier_.last_prompts[0]
    assert snapshots[0].prompts[0]["id"] == "p1"


def test_last_prompts_are_sorted_by_parsed_expiry() -> None:
    plus_two = timezone(timedelta(hours=2))
    # Sorts after "late" as a string, but expires 60s from NOW.
    offset_expiry = (NOW + timedelta(seconds=60)).astimezone(plus_two).isoformat()
    notifier_, feed, _clock, _snapshots = make_notifier()
    feed.prompts = [
        make_prompt("late", expires_in=300),
        make_prompt("soon", expires_in=30),
        make_prompt("offset", expires_at=offset_expiry),
        make_prompt("mid", expires_in=120),
    ]
    notifier_.tick()
    assert [p["id"] for p in notifier_.last_prompts] == ["soon", "offset", "mid", "late"]


def test_equal_expiries_keep_server_order() -> None:
    notifier_, feed, _clock, _snapshots = make_notifier()
    same = (NOW + timedelta(seconds=90)).isoformat()
    feed.prompts = [
        make_prompt("b", expires_at=same),
        make_prompt("a", expires_at=same),
        make_prompt("c", expires_at=same),
        make_prompt("early", expires_in=10),
    ]
    notifier_.tick()
    assert [p["id"] for p in notifier_.last_prompts] == ["early", "b", "a", "c"]


def test_expired_prompts_are_skipped() -> None:
    notifier_, feed, _clock, snapshots = make_notifier()
    feed.prompts = [make_prompt(expires_in=-1), make_prompt("p2", expires_in=0)]
    notifier_.tick()
    assert snapshots == [Snapshot(prompts=(), status="ok")]


def test_decided_prompts_are_skipped() -> None:
    notifier_, feed, _clock, snapshots = make_notifier()
    feed.prompts = [make_prompt(approve_decision="approve_once")]
    notifier_.tick()
    assert snapshots == [Snapshot(prompts=(), status="ok")]


def test_unparseable_expiry_is_skipped_not_fatal() -> None:
    notifier_, feed, _clock, snapshots = make_notifier()
    feed.prompts = [make_prompt(expires_at="garbage"), make_prompt("p2")]
    notifier_.tick()
    assert ids(snapshots[0]) == ["p2"]


def test_malformed_prompt_does_not_kill_the_poll_loop() -> None:
    notifier_, feed, _clock, snapshots = make_notifier()
    bad_type = make_prompt("bad", expires_at=12345)  # TypeError inside parse_expiry
    no_id = make_prompt("gone")
    del no_id["id"]
    feed.prompts = [bad_type, no_id, make_prompt("p2")]
    notifier_.tick()  # must not raise
    assert ids(snapshots[0]) == ["p2"]


def test_non_dict_entry_is_skipped_not_fatal() -> None:
    notifier_, feed, _clock, snapshots = make_notifier()
    feed.prompts = ["oops", None, make_prompt("p2")]  # type: ignore[list-item]
    notifier_.tick()  # must not raise
    assert ids(snapshots[0]) == ["p2"]


def test_prompt_missing_raw_cmd_is_kept_for_the_tray() -> None:
    notifier_, feed, _clock, snapshots = make_notifier()
    broken = make_prompt("bad")
    del broken["raw_cmd"]
    feed.prompts = [broken, make_prompt("p2")]
    notifier_.tick()
    assert ids(snapshots[0]) == ["bad", "p2"]


def test_prompt_with_non_int_tier_is_kept_for_the_tray() -> None:
    notifier_, feed, _clock, snapshots = make_notifier()
    feed.prompts = [make_prompt("bad", tier="not-a-number"), make_prompt("p2")]  # type: ignore[arg-type]
    notifier_.tick()
    assert ids(snapshots[0]) == ["bad", "p2"]


def test_unreachable_publishes_once_per_fetch_and_backs_off() -> None:
    notifier_, feed, clock, snapshots = make_notifier()
    feed.error = ConnectionRefusedError("down")
    notifier_.tick()
    assert snapshots == [Snapshot(prompts=(), status="unreachable")]
    assert feed.calls == 1
    clock.advance(3)
    notifier_.tick()  # inside the 6s back-off window: no fetch, no on_update
    assert feed.calls == 1
    assert len(snapshots) == 1
    clock.advance(3)
    notifier_.tick()  # window over: retries and publishes again
    assert feed.calls == 2
    assert [s.status for s in snapshots] == ["unreachable", "unreachable"]


def test_unreachable_is_logged_on_one_line(capsys: pytest.CaptureFixture[str]) -> None:
    notifier_, feed, _clock, _snapshots = make_notifier()
    feed.error = OSError("down\nforged")
    notifier_.tick()
    err = capsys.readouterr().err
    assert "POST /pending failed: down\\nforged" in err
    assert err.count("\n") == 1


def test_backoff_grows_and_is_capped() -> None:
    notifier_, feed, clock, snapshots = make_notifier()
    feed.error = OSError("down")
    for _ in range(12):
        notifier_.tick()
        clock.advance(61)
    calls = feed.calls
    notifier_.tick()
    clock.advance(59)
    notifier_.tick()
    assert feed.calls == calls + 1  # cap is 60s
    assert len(snapshots) == feed.calls  # skipped ticks publish nothing


def test_backoff_doubles_before_the_cap() -> None:
    notifier_, feed, clock, snapshots = make_notifier()  # interval 3s: delays 6, 12, 24
    feed.error = OSError("down")
    for delay in (6, 12, 24):
        before = feed.calls
        notifier_.tick()
        assert feed.calls == before + 1
        clock.advance(delay - 1)
        notifier_.tick()
        assert feed.calls == before + 1  # still backing off
        clock.advance(1)
    assert len(snapshots) == 3


def test_very_long_outage_does_not_overflow() -> None:
    notifier_, feed, clock, snapshots = make_notifier()
    feed.error = OSError("down")
    for _ in range(1200):
        notifier_.tick()
        clock.advance(61)
    assert feed.calls == 1200
    assert len(snapshots) == 1200
    assert {s.status for s in snapshots} == {"unreachable"}


def test_recovery_publishes_ok_and_a_new_outage_publishes_unreachable() -> None:
    notifier_, feed, clock, snapshots = make_notifier()
    feed.error = OSError("down")
    notifier_.tick()
    feed.error = None
    clock.advance(10)
    notifier_.tick()  # recovered
    feed.error = OSError("down again")
    clock.advance(3)
    notifier_.tick()
    assert [s.status for s in snapshots] == ["unreachable", "ok", "unreachable"]


def test_unreachable_snapshot_carries_the_last_known_prompts() -> None:
    notifier_, feed, clock, snapshots = make_notifier()
    feed.error = OSError("down")
    notifier_.tick()  # failed first poll: nothing known yet
    feed.error = None
    p1 = make_prompt("p1")
    feed.prompts = [p1]
    clock.advance(10)
    notifier_.tick()
    feed.error = OSError("down again")
    clock.advance(3)
    notifier_.tick()
    feed.error = None
    feed.prompts = [p1, make_prompt("p2")]
    clock.advance(10)
    notifier_.tick()
    assert snapshots[0] == Snapshot(prompts=(), status="unreachable")
    assert snapshots[1] == Snapshot(prompts=(p1,), status="ok")
    assert snapshots[2] == Snapshot(prompts=(p1,), status="unreachable")
    assert ids(snapshots[3]) == ["p1", "p2"]


def test_value_error_from_bad_response_counts_as_unreachable() -> None:
    notifier_, feed, _clock, snapshots = make_notifier()
    feed.error = ValueError("not json")
    notifier_.tick()
    notifier_.tick()  # same instant: inside the back-off window
    assert snapshots == [Snapshot(prompts=(), status="unreachable")]
    assert feed.calls == 1


def test_failing_on_update_does_not_bypass_backoff() -> None:
    feed, clock = Feed(), Clock()
    published: list[Snapshot] = []

    def boom(snapshot: Snapshot) -> None:
        published.append(snapshot)
        raise RuntimeError("tray gone")

    notifier_ = Notifier(fetch=feed, on_update=boom, clock=clock)
    feed.error = OSError("down")
    with pytest.raises(RuntimeError, match="tray gone"):
        notifier_.tick()
    clock.advance(3)
    notifier_.tick()  # back-off state was recorded before on_update raised
    assert feed.calls == 1
    assert [s.status for s in published] == ["unreachable"]


def test_failing_on_update_on_ok_path_leaves_consistent_state() -> None:
    feed, clock = Feed(), Clock()
    published: list[Snapshot] = []
    fail = True

    def flaky(snapshot: Snapshot) -> None:
        published.append(snapshot)
        if fail:
            raise RuntimeError("tray gone")

    notifier_ = Notifier(fetch=feed, on_update=flaky, clock=clock)
    prompt = make_prompt("p1")
    feed.prompts = [prompt]
    with pytest.raises(RuntimeError, match="tray gone"):
        notifier_.tick()
    assert notifier_.last_prompts == (prompt,)  # updated before on_update raised
    assert notifier_._next_poll is None
    fail = False
    clock.advance(3)
    notifier_.tick()  # next tick still polls
    assert feed.calls == 2
    assert [ids(s) for s in published] == [["p1"], ["p1"]]


def test_backoff_is_anchored_after_a_slow_failed_fetch() -> None:
    clock = Clock()
    calls = 0

    def slow_failure() -> list[dict]:
        nonlocal calls
        calls += 1
        clock.advance(4)  # the request hangs for 4s, then fails
        raise OSError("down")

    notifier_ = Notifier(fetch=slow_failure, on_update=lambda _s: None, clock=clock)
    notifier_.tick()  # fails at NOW+4s; back-off is 6s from there
    clock.advance(5)  # NOW+9s: inside the window (would be over if anchored at NOW)
    notifier_.tick()
    assert calls == 1
    clock.advance(1)  # NOW+10s: window over
    notifier_.tick()
    assert calls == 2


@pytest.mark.parametrize("bad", [0, -1.0, float("nan"), float("inf"), threading.TIMEOUT_MAX * 2])
def test_notifier_rejects_unusable_interval(bad: float) -> None:
    with pytest.raises(ValueError, match="interval"):
        Notifier(fetch=Feed(), on_update=lambda _s: None, interval_s=bad)


@pytest.mark.parametrize("bad", [0, -2.0, float("inf"), threading.TIMEOUT_MAX * 2])
def test_run_loop_rejects_non_positive_interval(bad: float) -> None:
    feed = Feed()
    core = Notifier(fetch=feed, on_update=lambda _s: None)
    with pytest.raises(ValueError, match="interval"):
        run_loop(core, threading.Event(), bad)
    assert feed.calls == 0


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
    snapshots: list[Snapshot] = []
    try:
        notifier_ = Notifier(fetch=lambda: http_fetch(_url(server)), on_update=snapshots.append)
        notifier_.tick()
    finally:
        server.shutdown()
        server.server_close()
    assert [s.status for s in snapshots] == ["unreachable"]


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


def test_json_depth_exceeds_boundary() -> None:
    limit = 5
    assert notifier._json_depth_exceeds(b"[" * limit + b"]" * limit, limit) is False
    assert notifier._json_depth_exceeds(b"[" * (limit + 1) + b"]" * (limit + 1), limit) is True


def test_json_depth_exceeds_ignores_brackets_in_strings() -> None:
    raw = b'{"a": "[[[[[[[[", "b": "x\\"[[[[[[[["}'
    assert notifier._json_depth_exceeds(raw, 1) is False


def test_json_depth_exceeds_normal_pending_payload() -> None:
    raw = b'{"prompts": [{"id": 1, "command": "ls [a]", "cwd": "/tmp"}]}'
    assert notifier._json_depth_exceeds(raw, notifier.MAX_JSON_DEPTH) is False


def test_http_fetch_failures_are_what_tick_treats_as_unreachable() -> None:
    server, _seen = _serve(b"not json")
    snapshots: list[Snapshot] = []
    try:
        notifier_ = Notifier(fetch=lambda: http_fetch(_url(server)), on_update=snapshots.append)
        notifier_.tick()
    finally:
        server.shutdown()
        server.server_close()
    assert [s.status for s in snapshots] == ["unreachable"]


UI_URL = "http://127.0.0.1:8511/Pending_Prompts"


def test_spawn_daemon_runs_function_in_a_daemon_thread() -> None:
    seen: dict = {}
    done = threading.Event()

    def work() -> None:
        seen["daemon"] = threading.current_thread().daemon
        seen["main"] = threading.current_thread() is threading.main_thread()
        done.set()

    spawn_daemon(work)
    assert done.wait(2)
    assert seen == {"daemon": True, "main": False}


def test_open_detached_starts_a_reaper(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProc:
        waited = False

        def wait(self) -> int:
            FakeProc.waited = True
            return 0

    popen_args: list[Any] = []
    reapers: list[Any] = []

    def fake_popen(argv: list[str], **kwargs: Any) -> FakeProc:
        popen_args.append(argv)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(notifier, "spawn_daemon", reapers.append)
    open_detached(["xdg-open", UI_URL])
    assert popen_args == [["xdg-open", UI_URL]]
    assert len(reapers) == 1
    reapers[0]()
    assert FakeProc.waited


def test_parse_args_defaults() -> None:
    args = parse_args([])
    assert args.interval == 3.0
    assert args.url == "http://127.0.0.1:4111/pending" == DEFAULT_PENDING_URL
    assert args.ui_url == "http://127.0.0.1:8511/Pending_Prompts" == DEFAULT_UI_URL
    assert args.max_items == 10


def test_parse_args_overrides() -> None:
    args = parse_args(
        [
            "--interval",
            "5",
            "--url",
            "http://h:1/pending",
            "--ui-url",
            "https://u/x",
            "--max-items",
            "3",
        ]
    )
    assert (args.interval, args.url, args.ui_url, args.max_items) == (
        5.0,
        "http://h:1/pending",
        "https://u/x",
        3,
    )


def test_parse_args_accepts_max_items_of_one() -> None:
    assert parse_args(["--max-items", "1"]).max_items == 1


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "2.5", ""])
def test_parse_args_rejects_bad_max_items(bad: str) -> None:
    with pytest.raises(SystemExit):
        parse_args(["--max-items", bad])


def test_parse_args_help_uses_tray_wording(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")  # keep the description on one line
    monkeypatch.setenv("NO_COLOR", "1")  # Python 3.14 argparse can colour its help
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    with pytest.raises(SystemExit):
        parse_args(["--help"])
    out = capsys.readouterr().out
    assert "usage: shell-runner-tray" in out
    assert "Silent tray indicator for pending shell-runner approval prompts (read-only)." in out
    assert "popup" not in out.lower()


@pytest.mark.parametrize("flag", ["--url", "--ui-url"])
def test_parse_args_rejects_non_http_urls(flag: str) -> None:
    with pytest.raises(SystemExit):
        parse_args([flag, "file:///etc/passwd"])


@pytest.mark.parametrize("flag", ["--url", "--ui-url"])
@pytest.mark.parametrize("bad", ["http://", "https:///x"])
def test_parse_args_rejects_hostless_urls(flag: str, bad: str) -> None:
    with pytest.raises(SystemExit):
        parse_args([flag, bad])


def test_parse_args_accepts_uppercase_scheme() -> None:
    args = parse_args(["--url", "HTTP://127.0.0.1:4111/pending"])
    assert args.url == "HTTP://127.0.0.1:4111/pending"


@pytest.mark.parametrize("bad", ["0", "-1", "nan", "inf", "abc", "0.05", "3601", "1e999", "1e308"])
def test_parse_args_rejects_bad_interval(bad: str) -> None:
    with pytest.raises(SystemExit):
        parse_args(["--interval", bad])


def test_parse_args_accepts_fractional_interval() -> None:
    assert parse_args(["--interval", "0.5"]).interval == 0.5


def test_parse_args_accepts_interval_bounds() -> None:
    assert parse_args(["--interval", "0.1"]).interval == 0.1
    assert parse_args(["--interval", "3600"]).interval == 3600.0


class CountingStop(threading.Event):
    """Stop event that records wait timeouts and sets itself on the third wait."""

    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        if len(self.waits) == 3:
            self.set()
        return self.is_set()


def test_run_loop_ticks_then_waits_until_stop_is_set() -> None:
    feed = Feed()
    snapshots: list[Snapshot] = []
    stop = CountingStop()
    run_loop(Notifier(fetch=feed, on_update=snapshots.append), stop, 2.0)
    assert feed.calls == 3  # keeps polling between waits
    assert stop.waits == [2.0, 2.0, 2.0]
    assert len(snapshots) == 3


def test_run_loop_returns_at_once_when_stop_is_already_set() -> None:
    feed = Feed()
    stop = threading.Event()
    stop.set()
    run_loop(Notifier(fetch=feed, on_update=lambda _snapshot: None), stop, 2.0)
    assert feed.calls == 0


def test_run_loop_wait_is_interruptible() -> None:
    ticked = threading.Event()
    stop = threading.Event()
    core = Notifier(fetch=Feed(), on_update=lambda _snapshot: ticked.set())
    worker = threading.Thread(target=run_loop, args=(core, stop, 3600.0), daemon=True)
    worker.start()
    assert ticked.wait(2)
    stop.set()  # must end the 3600s wait at once
    worker.join(timeout=2)
    assert not worker.is_alive()


def test_slow_fetch_drops_a_prompt_that_expired_meanwhile() -> None:
    clock = Clock()
    snapshots: list[Snapshot] = []

    def slow_fetch() -> list[dict]:
        clock.advance(400)  # prompt (300s left) expires while the request is in flight
        return [make_prompt(expires_in=300)]

    notifier_ = Notifier(fetch=slow_fetch, on_update=snapshots.append, clock=clock)
    notifier_.tick()
    assert snapshots == [Snapshot(prompts=(), status="ok")]


def test_log_escapes_newlines_in_prompt_id(capsys: pytest.CaptureFixture[str]) -> None:
    notifier_, feed, _clock, snapshots = make_notifier()
    feed.prompts = [make_prompt(pid="x\nshell-runner-notify: forged", expires_at="bad")]
    notifier_.tick()
    err = capsys.readouterr().err
    assert snapshots == [Snapshot(prompts=(), status="ok")]
    assert err.count("\n") == 1
    assert "x\\nshell-runner-notify: forged" in err


def test_no_src_module_references_notify_send() -> None:
    src = Path(notifier.__file__).parent
    offenders = [p.name for p in src.rglob("*.py") if "notify-send" in p.read_text()]
    assert offenders == []


def test_notifier_module_is_stdlib_only() -> None:
    source = Path(notifier.__file__).read_text()
    roots = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module
            roots.add(node.module.split(".")[0])
    assert roots <= set(sys.stdlib_module_names)


def test_notifier_module_is_read_only() -> None:
    source = Path(notifier.__file__).read_text()
    assert "approve_pending" not in source
    assert "/approve" not in source
    assert "shell=True" not in source
