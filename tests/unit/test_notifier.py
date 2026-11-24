"""Unit tests for the desktop approval notifier (issue #136).

Fetch, notify, run and clock are injected, so nothing here touches the
network, the desktop or real time.
"""

from __future__ import annotations

from shell_runner.notifier import build_body, clean, format_remaining, shorten_home


def test_clean_escapes_markup() -> None:
    assert clean("<b>x</b> & y") == "&lt;b&gt;x&lt;/b&gt; &amp; y"


def test_clean_strips_control_characters() -> None:
    assert clean("a\x1b[31mb\x00c\x7f") == "a[31mbc"


def test_clean_drops_bidi_and_line_separator_characters() -> None:
    assert clean("a‮b c d") == "abcd"


def test_clean_turns_newlines_and_tabs_into_spaces() -> None:
    assert clean("a\nb\tc\r") == "a b c "


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
