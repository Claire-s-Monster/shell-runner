"""Unit tests for the desktop approval notifier (issue #136).

Fetch, notify, run and clock are injected, so nothing here touches the
network, the desktop or real time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from shell_runner.notifier import Notifier, build_body, clean, format_remaining, shorten_home


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


def test_prompt_missing_raw_cmd_still_gets_a_popup() -> None:
    notifier, feed, _clock, popups = make_notifier()
    broken = make_prompt("bad")
    del broken["raw_cmd"]
    feed.prompts = [broken, make_prompt("p2")]
    notifier.tick()
    assert len(popups) == 2
    assert "cmd: ?" in popups[0][1].splitlines()
