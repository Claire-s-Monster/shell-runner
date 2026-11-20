# shell-runner-notify Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A standalone desktop notifier that polls shell-runner's `POST /pending` and pops up one `notify-send` notification per new T3/T4 approval prompt (with an "Open" action to the Pending Prompts UI and a single pre-expiry reminder), plus a static test proving the server unit files bind only to 127.0.0.1. Tracking issue: #119 (Part N only; Part B is a separate plan and N must not depend on it).

**Architecture:** `src/shell_runner/notifier.py` holds a pure polling core (`Notifier`) with injected `fetch`, `notify` and `clock` callables, so tests are deterministic and never touch the network, the desktop or real time. A thin `DesktopNotifier` adapter turns popups into `notify-send` argv lists (one daemon thread per popup for the click wait, `xdg-open` on click). A tiny argparse `main` runs the loop; it is started by `pixi run notify` or a systemd user unit.

**Tech Stack:** Python 3.11+, stdlib only (`urllib`, `subprocess`, `threading`, `argparse`, `html`), system `notify-send` and `xdg-open`, pytest, ruff, mypy, pixi.

---

## Constraints (read first)

- Stdlib only. No new dependencies. Do NOT touch `[project]`, `dependencies`, or any `[tool.pixi.*dependencies]` table (CI pins pixi 0.49.0 / lock v6; dependency edits force a v7 re-solve that breaks CI).
- Read-only: the notifier never calls `/approve_pending` or any mutating route (`/pending` returns no tokens).
- Every `notify-send` / `xdg-open` call is an argv list. Never `shell=True`.
- Test commands: `pixi run --frozen -e dev pytest <path> -v` (verified: `dev` env includes the `quality` feature with pytest; `pytest.ini_options` already sets `pythonpath = ["src", ...]`). Quality gate: `pixi run --frozen -e quality-ci ruff check src/ tests/` and `pixi run --frozen -e quality-ci mypy src/`, plus `pixi run --frozen -e quality-ci ruff format --check src/ tests/` (env `quality-ci` exists in `[tool.pixi.environments]`).
- Every test or quality report you write must state the commit SHA (`git rev-parse --short HEAD`) and the tree path (`pwd`) it ran against. Work in `/home/memento/ClaudeCode/Servers/shell-runner/worktrees/features` on branch `feat/approval-wait-notifier`.
- Code facts the plan relies on (already verified): `/pending` (`server.py` `get_pending_route`) takes body `{}` (`include_resolved` defaults false) and returns `{"prompts": [PendingPromptDetail...]}`. Fields used: `id`, `agent_id`, `cwd`, `raw_cmd`, `command_tier` (int), `expires_at` (UTC ISO-8601 string, from `datetime.now(UTC).isoformat()`), `approve_decision` (None while undecided), `scope_warning` (str or None). The UI page is `ui/pages/2_Pending_Prompts.py`, so Streamlit (port 8511) serves it at `/Pending_Prompts`.

## File Structure

- Create `src/shell_runner/notifier.py`: text sanitising/body building, `Notifier` polling core (seen-id set, reminder schedule, back-off), `http_fetch`, `DesktopNotifier` (notify-send/xdg-open), `parse_args`, `main`.
- Create `tests/unit/test_notifier.py`: all notifier tests (injected fetch/notify/clock/run).
- Create `tests/unit/test_service_bind.py`: static loopback-bind check on unit files and daemon script.
- Create `scripts/shell-runner-notify.service.example`: systemd user unit (repo convention: `scripts/*.service.example`).
- Modify `pyproject.toml`: one line in `[tool.pixi.tasks]` (`notify`).
- Modify `README.md`: new "Desktop approval notifier" section.

---

### Task 1: Text sanitising and popup body

**Files:**
- Create: `src/shell_runner/notifier.py`
- Create: `tests/unit/test_notifier.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for the desktop approval notifier (issue #119, Part N).

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
```

- [ ] **Step 2: Run, expect failure**

Run: `pixi run --frozen -e dev pytest tests/unit/test_notifier.py -v`
Expected: collection error `ModuleNotFoundError: No module named 'shell_runner.notifier'`.

- [ ] **Step 3: Implement**

Create `src/shell_runner/notifier.py`:

```python
"""Desktop approval notifier for shell-runner (issue #119, Part N).

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
    lines = []
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
```

- [ ] **Step 4: Run, expect pass**

Run: `pixi run --frozen -e dev pytest tests/unit/test_notifier.py -v`
Expected: 13 passed. Report SHA (`git rev-parse --short HEAD`) and tree (`pwd`).

- [ ] **Step 5: Quality gate and commit**

```bash
pixi run --frozen -e quality-ci ruff check --fix src/ tests/   # sorts imports (I001); re-run tests if it changed files
pixi run --frozen -e quality-ci ruff check src/ tests/
pixi run --frozen -e quality-ci ruff format --check src/ tests/
pixi run --frozen -e quality-ci mypy src/
git add src/shell_runner/notifier.py tests/unit/test_notifier.py
git commit -m "feat(notifier): popup text sanitising and body builder (#119)"
```

---

### Task 2: Polling core: one popup per new prompt, skip expired

**Files:**
- Modify: `src/shell_runner/notifier.py`
- Modify: `tests/unit/test_notifier.py`

- [ ] **Step 1: Write the failing tests**

Change the import line to
`from shell_runner.notifier import Notifier, build_body, clean, format_remaining, shorten_home`
and add at the top of the imports: `from datetime import UTC, datetime, timedelta`
(`timedelta` is used by the test helpers below, so it is not an unused import here). Append:

```python
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
    broken = make_prompt("bad")
    del broken["raw_cmd"]
    feed.prompts = [broken, make_prompt("p2")]
    notifier.tick()  # must not raise
    assert len(popups) == 1
    assert "agent: agent-1" in popups[0][1]
```

- [ ] **Step 2: Run, expect failure**

Run: `pixi run --frozen -e dev pytest tests/unit/test_notifier.py -v`
Expected: collection error `ImportError: cannot import name 'Notifier' from 'shell_runner.notifier'`.

- [ ] **Step 3: Implement**

In `notifier.py` replace the import block with:

```python
import html
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
```

(Also keep `import unicodedata` from Task 1 in the block, sorted alphabetically.
`timedelta` is imported in Task 4, where it is first used, to keep F401 clean.)

Append:

```python
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
```

- [ ] **Step 4: Run, expect pass**

Run: `pixi run --frozen -e dev pytest tests/unit/test_notifier.py -v`
Expected: 22 passed. Report SHA and tree.

- [ ] **Step 5: Quality gate and commit**

```bash
pixi run --frozen -e quality-ci ruff check --fix src/ tests/   # sorts imports (I001); re-run tests if it changed files
pixi run --frozen -e quality-ci ruff check src/ tests/
pixi run --frozen -e quality-ci ruff format --check src/ tests/
pixi run --frozen -e quality-ci mypy src/
git add src/shell_runner/notifier.py tests/unit/test_notifier.py
git commit -m "feat(notifier): polling core with one popup per new prompt (#119)"
```

---

### Task 3: Pre-expiry reminder (once, only if undecided)

**Files:**
- Modify: `src/shell_runner/notifier.py`
- Modify: `tests/unit/test_notifier.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
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
```

- [ ] **Step 2: Run, expect failure**

Run: `pixi run --frozen -e dev pytest tests/unit/test_notifier.py -v -k reminder`
Expected: `test_reminder_fires_once_about_60s_before_expiry` and `test_t4_reminder_is_critical` FAIL (`assert 1 == 2`, no reminder popup); the others may pass vacuously.

- [ ] **Step 3: Implement**

Add constant after `CMD_LIMIT`: `REMIND_BEFORE_S = 60`. In `Notifier` add the field `reminded: set[str] = field(default_factory=set)` directly after the `seen` field. Then replace the COMPLETE `_handle_one` method (the whole method, 4-space class indentation) with:

```python
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
            if remaining <= REMIND_BEFORE_S:
                self.reminded.add(prompt["id"])
        elif prompt["id"] not in self.reminded and remaining <= REMIND_BEFORE_S:
            self.reminded.add(prompt["id"])
            self._popup(prompt, remaining, "approval expiring")
```

and replace the COMPLETE `_handle` method with:

```python
    def _handle(self, prompts: list[dict], now: datetime) -> None:
        live: set[str] = set()
        for prompt in prompts:
            try:
                self._handle_one(prompt, now, live)
            except (KeyError, TypeError, ValueError) as exc:
                _log(f"skipping malformed prompt {prompt.get('id')}: {exc!r}")
        self.seen &= live
        self.reminded &= live
```

- [ ] **Step 4: Run, expect pass**

Run: `pixi run --frozen -e dev pytest tests/unit/test_notifier.py -v`
Expected: 27 passed. Report SHA and tree.

- [ ] **Step 5: Quality gate and commit**

```bash
pixi run --frozen -e quality-ci ruff check --fix src/ tests/   # sorts imports (I001); re-run tests if it changed files
pixi run --frozen -e quality-ci ruff check src/ tests/
pixi run --frozen -e quality-ci mypy src/
git add src/shell_runner/notifier.py tests/unit/test_notifier.py
git commit -m "feat(notifier): one reminder popup 60s before expiry (#119)"
```

---

### Task 4: Unreachable server: one popup, back-off, quiet until recovery

**Files:**
- Modify: `src/shell_runner/notifier.py`
- Modify: `tests/unit/test_notifier.py`

- [ ] **Step 1: Write the failing tests**

Append (the existing `make_notifier` uses the default 3s interval; backoff delay after the n-th consecutive failure is `min(interval * 2**n, 60)`, i.e. 6s, 12s, ...):

```python
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
```

- [ ] **Step 2: Run, expect failure**

Run: `pixi run --frozen -e dev pytest tests/unit/test_notifier.py -v -k "unreachable or backoff or recovery or outage or value_error"`
Expected: FAIL with the raw `ConnectionRefusedError`/`OSError`/`ValueError` propagating out of `tick()`.

- [ ] **Step 3: Implement**

First change the datetime import to `from datetime import UTC, datetime, timedelta` (`timedelta` is first used here by the back-off). Add constants after `REMIND_BEFORE_S`: `MAX_BACKOFF_S = 60.0` and a `DEFAULT_INTERVAL_S = 3.0`. Add `interval_s: float = DEFAULT_INTERVAL_S` and private state to `Notifier`:

```python
    interval_s: float = DEFAULT_INTERVAL_S
    _failures: int = field(default=0, init=False)
    _next_poll: datetime | None = field(default=None, init=False)
```

Replace `tick` and add `_on_unreachable`:

```python
    def tick(self) -> None:
        now = self.clock()
        if self._next_poll is not None and now < self._next_poll:
            return
        try:
            prompts = self.fetch()
        except (OSError, ValueError) as exc:
            self._on_unreachable(now, exc)
            return
        self._failures = 0
        self._next_poll = None
        self._handle(prompts, now)

    def _on_unreachable(self, now: datetime, exc: Exception) -> None:
        if self._failures == 0:
            self.notify(
                "shell-runner-notify: server unreachable",
                f"POST /pending failed: {clean(str(exc), CMD_LIMIT)}",
                "normal",
            )
        self._failures += 1
        delay = min(self.interval_s * 2**self._failures, MAX_BACKOFF_S)
        self._next_poll = now + timedelta(seconds=delay)
```

(`min(..., MAX_BACKOFF_S)` also bounds `2**n`; floats stay finite for realistic outage lengths because the exponent is only evaluated per failure and Python ints do not overflow.)

- [ ] **Step 4: Run, expect pass**

Run: `pixi run --frozen -e dev pytest tests/unit/test_notifier.py -v`
Expected: 32 passed. Report SHA and tree.

- [ ] **Step 5: Quality gate and commit**

```bash
pixi run --frozen -e quality-ci ruff check --fix src/ tests/   # sorts imports (I001); re-run tests if it changed files
pixi run --frozen -e quality-ci ruff check src/ tests/
pixi run --frozen -e quality-ci mypy src/
git add src/shell_runner/notifier.py tests/unit/test_notifier.py
git commit -m "feat(notifier): unreachable popup once with back-off (#119)"
```

---

### Task 5: stdlib HTTP fetcher

**Files:**
- Modify: `src/shell_runner/notifier.py`
- Modify: `tests/unit/test_notifier.py`

- [ ] **Step 1: Write the failing tests**

Add imports: `import json`, `import threading`, `from http.server import BaseHTTPRequestHandler, HTTPServer`, `import pytest`, and `http_fetch` to the notifier import. Append:

```python
def _serve(payload: bytes, status: int = 200):
    seen: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 (http.server handler naming)
            seen["path"] = self.path
            seen["method"] = self.command
            seen["body"] = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, seen


def test_http_fetch_posts_empty_json_and_returns_prompts() -> None:
    server, seen = _serve(json.dumps({"prompts": [{"id": "p1"}]}).encode())
    try:
        url = f"http://127.0.0.1:{server.server_port}/pending"
        assert http_fetch(url) == [{"id": "p1"}]
    finally:
        server.shutdown()
    assert seen["method"] == "POST"
    assert seen["path"] == "/pending"
    assert json.loads(seen["body"]) == {}


def test_http_fetch_rejects_unexpected_payload_as_value_error() -> None:
    server, _seen = _serve(b'{"nope": 1}')
    try:
        with pytest.raises(ValueError, match="unexpected"):
            http_fetch(f"http://127.0.0.1:{server.server_port}/pending")
    finally:
        server.shutdown()


def test_http_fetch_connection_refused_is_oserror() -> None:
    server, _seen = _serve(b"{}")
    port = server.server_port
    server.shutdown()
    server.server_close()
    with pytest.raises(OSError):  # noqa: PT011
        http_fetch(f"http://127.0.0.1:{port}/pending", timeout=1)
```

- [ ] **Step 2: Run, expect failure**

Run: `pixi run --frozen -e dev pytest tests/unit/test_notifier.py -v -k http_fetch`
Expected: `ImportError: cannot import name 'http_fetch'`.

- [ ] **Step 3: Implement**

Add imports `import json` and `import urllib.request`. Append:

```python
def http_fetch(url: str, timeout: float = 5.0) -> list[dict]:
    """POST `{}` to /pending and return the prompt list.

    Raises OSError (incl. URLError/timeout) when unreachable and ValueError on
    a malformed response; `Notifier.tick` treats both as "server unreachable".
    """
    request = urllib.request.Request(  # noqa: S310 (scheme validated in parse_args)
        url,
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
        data = json.load(response)
    prompts = data.get("prompts") if isinstance(data, dict) else None
    if not isinstance(prompts, list):
        raise ValueError("unexpected /pending response")
    return prompts
```

If `ruff` reports `RUF100` for the unused `noqa: S310` (only `E,W,F,I,N,UP,B,A,C4,PT,SIM,RET` are selected), delete that `noqa` comment; keep the bandit `nosec`.

- [ ] **Step 4: Run, expect pass**

Run: `pixi run --frozen -e dev pytest tests/unit/test_notifier.py -v`
Expected: 35 passed. Report SHA and tree.

- [ ] **Step 5: Quality gate and commit**

```bash
pixi run --frozen -e quality-ci ruff check --fix src/ tests/   # sorts imports (I001); re-run tests if it changed files
pixi run --frozen -e quality-ci ruff check src/ tests/
pixi run --frozen -e quality-ci mypy src/
pixi run --frozen -e quality-ci bandit -c pyproject.toml -r src/shell_runner/notifier.py
git add src/shell_runner/notifier.py tests/unit/test_notifier.py
git commit -m "feat(notifier): stdlib urllib fetcher for POST /pending (#119)"
```

---

### Task 6: Desktop adapter: notify-send argv, Open action, fallback

Detection rule (decided): `notify-send --help` is run once and cached; `--action` support means the text contains `--action`. Safety net: if the `--wait --action` call exits non-zero (e.g. an older libnotify rejecting the option), the adapter flips to "unsupported" for the rest of the run and re-sends the popup plain, with the URL in the body.

**Files:**
- Modify: `src/shell_runner/notifier.py`
- Modify: `tests/unit/test_notifier.py`

- [ ] **Step 1: Write the failing tests**

Add `DesktopNotifier` and `spawn_daemon` to the notifier import. Append:

```python
UI_URL = "http://127.0.0.1:8511/Pending_Prompts"
HELP_WITH_ACTION = "Usage:\n  -A, --action=[NAME=]Text  Specify action\n  -w, --wait\n"
HELP_WITHOUT_ACTION = "Usage:\n  -u, --urgency=LEVEL\n"


class FakeRun:
    def __init__(self, help_text: str = HELP_WITH_ACTION, code: int = 0, out: str = "") -> None:
        self.help_text, self.code, self.out = help_text, code, out
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if argv[1:] == ["--help"]:
            return 0, self.help_text
        return self.code, self.out


def make_desktop(run, opened: list[list[str]] | None = None, spawned: list | None = None):
    def spawn(fn) -> None:
        if spawned is not None:
            spawned.append(fn)
        fn()

    return DesktopNotifier(
        ui_url=UI_URL,
        run=run,
        spawn=spawn,
        opener=(opened if opened is not None else []).append,
    )


def test_action_popup_argv_and_click_opens_url() -> None:
    run, opened = FakeRun(out="open\n"), []
    make_desktop(run, opened)("shell-runner: T4 approval needed", "body", "critical")
    assert run.calls[-1] == [
        "notify-send",
        "--app-name=shell-runner",
        "--urgency=critical",
        "--wait",
        "--action=open=Open",
        "--",
        "shell-runner: T4 approval needed",
        "body",
    ]
    assert opened == [["xdg-open", UI_URL]]


def test_dismissed_popup_does_not_open_anything() -> None:
    run, opened = FakeRun(out=""), []
    make_desktop(run, opened)("t", "b", "normal")
    assert opened == []


def test_each_popup_gets_its_own_daemon_thread_spawn() -> None:
    spawned: list = []
    desktop = make_desktop(FakeRun(), spawned=spawned)
    desktop("t1", "b", "normal")
    desktop("t2", "b", "normal")
    assert len(spawned) == 2


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


def test_without_action_support_url_goes_in_body_and_no_thread() -> None:
    run, opened, spawned = FakeRun(help_text=HELP_WITHOUT_ACTION), [], []
    make_desktop(run, opened, spawned)("t", "line1", "normal")
    argv = run.calls[-1]
    assert "--wait" not in argv
    assert not any(a.startswith("--action") for a in argv)
    assert argv[-2:] == ["t", f"line1\n{UI_URL}"]
    assert spawned == []
    assert opened == []


def test_action_failure_at_runtime_falls_back_to_plain_popup() -> None:
    run = FakeRun(code=1)  # help advertises --action, but the call fails
    desktop = make_desktop(run)
    desktop("t", "b", "normal")
    assert "--wait" in run.calls[1]
    assert "--wait" not in run.calls[2]
    assert run.calls[2][-1] == f"b\n{UI_URL}"
    desktop("t2", "b", "normal")  # remembered: plain from now on
    assert "--wait" not in run.calls[3]


def test_help_probe_runs_once() -> None:
    run = FakeRun()
    desktop = make_desktop(run)
    desktop("t", "b", "normal")
    desktop("t", "b", "normal")
    assert [c for c in run.calls if c[1:] == ["--help"]] == [["notify-send", "--help"]]


def test_all_invocations_are_argv_lists_of_strings() -> None:
    run, opened = FakeRun(out="open"), []
    make_desktop(run, opened)("t", "b", "normal")
    make_desktop(FakeRun(help_text=HELP_WITHOUT_ACTION))("t", "b", "normal")
    for argv in [*run.calls, *opened]:
        assert isinstance(argv, list)
        assert all(isinstance(a, str) for a in argv)


def test_missing_notify_send_does_not_crash() -> None:
    def broken(argv: list[str]) -> tuple[int, str]:
        raise FileNotFoundError(argv[0])

    make_desktop(broken)("t", "b", "normal")  # must not raise
```

- [ ] **Step 2: Run, expect failure**

Run: `pixi run --frozen -e dev pytest tests/unit/test_notifier.py -v -k "action or popup or spawn or help or argv or notify_send"`
Expected: `ImportError: cannot import name 'DesktopNotifier'`.

- [ ] **Step 3: Implement**

Add imports `import subprocess  # nosec B404` and `import threading`. Append:

```python
Runner = Callable[[list[str]], tuple[int, str]]


def run_argv(argv: list[str]) -> tuple[int, str]:
    """Run an argv list (never a shell); return (exit code, stdout + stderr)."""
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)  # nosec B603
    return proc.returncode, proc.stdout + proc.stderr


def spawn_daemon(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


def open_detached(argv: list[str]) -> None:
    subprocess.Popen(  # nosec B603
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


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
        code, out = self._run(argv)
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

    def _run(self, argv: list[str]) -> tuple[int, str]:
        try:
            return self.run(argv)
        except OSError as exc:
            _log(f"{argv[0]} failed: {exc}")
            return 127, ""
```

- [ ] **Step 4: Run, expect pass**

Run: `pixi run --frozen -e dev pytest tests/unit/test_notifier.py -v`
Expected: 44 passed. Report SHA and tree.

- [ ] **Step 5: Quality gate and commit**

```bash
pixi run --frozen -e quality-ci ruff check --fix src/ tests/   # sorts imports (I001); re-run tests if it changed files
pixi run --frozen -e quality-ci ruff check src/ tests/
pixi run --frozen -e quality-ci ruff format --check src/ tests/
pixi run --frozen -e quality-ci mypy src/
pixi run --frozen -e quality-ci bandit -c pyproject.toml -r src/shell_runner/notifier.py
git add src/shell_runner/notifier.py tests/unit/test_notifier.py
git commit -m "feat(notifier): notify-send adapter with Open action and fallback (#119)"
```

---

### Task 7: CLI entry point, stdlib-only/read-only guards, pixi task

**Files:**
- Modify: `src/shell_runner/notifier.py`
- Modify: `tests/unit/test_notifier.py`
- Modify: `pyproject.toml` (one task line only)

- [ ] **Step 1: Write the failing tests**

Add imports `import ast`, `import sys`, `from pathlib import Path`, and `DEFAULT_PENDING_URL, DEFAULT_UI_URL, main, parse_args` to the notifier import (`import shell_runner.notifier as notifier_module` as well). Append:

```python
def test_parse_args_defaults() -> None:
    args = parse_args([])
    assert args.interval == 3.0
    assert args.url == "http://127.0.0.1:4111/pending" == DEFAULT_PENDING_URL
    assert args.ui_url == "http://127.0.0.1:8511/Pending_Prompts" == DEFAULT_UI_URL


def test_parse_args_overrides() -> None:
    args = parse_args(["--interval", "5", "--url", "http://h:1/pending", "--ui-url", "https://u/x"])
    assert (args.interval, args.url, args.ui_url) == (5.0, "http://h:1/pending", "https://u/x")


@pytest.mark.parametrize("flag", ["--url", "--ui-url"])
def test_parse_args_rejects_non_http_urls(flag: str) -> None:
    with pytest.raises(SystemExit):
        parse_args([flag, "file:///etc/passwd"])


def test_main_loops_until_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    fetched: list[str] = []
    monkeypatch.setattr(
        notifier_module, "http_fetch", lambda url, timeout=5.0: fetched.append(url) or []
    )

    def stop(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(notifier_module.time, "sleep", stop)
    assert main(["--url", "http://127.0.0.1:1/pending"]) == 0
    assert fetched == ["http://127.0.0.1:1/pending"]


def test_notifier_module_is_stdlib_only() -> None:
    source = Path(notifier_module.__file__).read_text()
    roots = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert roots <= set(sys.stdlib_module_names)


def test_notifier_module_is_read_only() -> None:
    source = Path(notifier_module.__file__).read_text()
    assert "approve_pending" not in source
    assert "/approve" not in source
    assert "shell=True" not in source
```

- [ ] **Step 2: Run, expect failure**

Run: `pixi run --frozen -e dev pytest tests/unit/test_notifier.py -v -k "parse_args or main or stdlib or read_only"`
Expected: `ImportError: cannot import name 'DEFAULT_PENDING_URL'`.

- [ ] **Step 3: Implement**

Add imports `import argparse`, `import time`. Add near the constants:

```python
DEFAULT_PENDING_URL = "http://127.0.0.1:4111/pending"
DEFAULT_UI_URL = "http://127.0.0.1:8511/Pending_Prompts"
```

Append:

```python
def _http_url(value: str) -> str:
    if not value.startswith(("http://", "https://")):
        raise argparse.ArgumentTypeError(f"not an http(s) URL: {value!r}")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="shell-runner-notify",
        description="Desktop popups for pending shell-runner approval prompts (read-only).",
    )
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S, help="poll seconds")
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
```

Note: `main` resolves `http_fetch` at call time through the module global, which is why the test's monkeypatch works.

Edit `pyproject.toml`: add exactly one line at the end of the `[tool.pixi.tasks]` table, directly after the `approval-tripwire = ...` line:

```toml
notify = { cmd = "python -m shell_runner.notifier", env = { PYTHONPATH = "src" } }
```

Do not touch `[project]`, `dependencies` or any dependencies table.

- [ ] **Step 4: Run, expect pass; prove the lock is untouched**

```bash
pixi run --frozen -e dev pytest tests/unit/test_notifier.py -v
git status --short pixi.lock   # expect EMPTY right after the test run (a newer local pixi could rewrite the lock to v7)
pixi install --locked -e dev
pixi run --frozen notify --help
git status --short pixi.lock   # still empty
```
Expected: 51 passed; `pixi.lock` unmodified after both runs (if the first `git status` shows it modified, run `git checkout -- pixi.lock` and always add `--frozen` to pixi invocations from here on); `pixi install --locked` succeeds (pixi tasks are not stored in `pixi.lock`, which records only environments and packages, so adding a task line does not re-solve; `--locked` errors if the lock were outdated); `--help` prints the usage; `git status` shows nothing for `pixi.lock`. If `--locked` fails or `pixi.lock` changes, revert the `pyproject.toml` line (`git checkout -- pyproject.toml`) and use `PYTHONPATH=src python -m shell_runner.notifier` documented in the README (Task 9) instead. Report SHA and tree.

- [ ] **Step 5: Quality gate and commit**

```bash
pixi run --frozen -e quality-ci ruff check --fix src/ tests/   # sorts imports (I001); re-run tests if it changed files
pixi run --frozen -e quality-ci ruff check src/ tests/
pixi run --frozen -e quality-ci ruff format --check src/ tests/
pixi run --frozen -e quality-ci mypy src/
git add src/shell_runner/notifier.py tests/unit/test_notifier.py pyproject.toml
git commit -m "feat(notifier): CLI entry point and pixi notify task (#119)"
```

---

### Task 8: Loopback bind check (acceptance criterion)

`POST /pending` is unauthenticated, so the server must bind only to 127.0.0.1. Statically assert it in the unit files and daemon script. Files checked: `scripts/shell-runner.service` (ExecStart, line 21), `scripts/shell-runner.service.example` (ExecStart, line 11; the example file exists), `scripts/shell-runner-daemon.sh` (`DEFAULT_HOST`, line 25). `scripts/shell-runner-daemon.sh` does honour a `SHELL_RUNNER_HOST` override (line 29 `${SHELL_RUNNER_HOST:-$DEFAULT_HOST}`, line 110 `--host "$SHELL_RUNNER_HOST"`), so the test also asserts the fallback line still uses `DEFAULT_HOST`. **Residual, documented gap:** a static test cannot see an explicit `SHELL_RUNNER_HOST=0.0.0.0` set in the environment of whoever starts the daemon; the manual `ss -ltnp` check in Task 10 is the only guard for that. (The `Environment=SHELL_RUNNER_HOST=127.0.0.1` lines in the unit files only affect the daemon script, not the uvicorn `ExecStart`, and are not asserted.) `/mcp` is served by the same process on 4111, so one bind covers HTTP API and `/mcp`; no separate 4112 listener exists in the units (4112 appears only in the throwaway `scripts/verify_mcp_mount.py`).

**Files:**
- Create: `tests/unit/test_service_bind.py`

- [ ] **Step 1: Write the tests**

```python
"""Static guard: shell-runner must only bind to loopback (issue #119).

POST /pending is unauthenticated, so a non-loopback bind would expose
approval-prompt details. Port 4111 serves both the HTTP API and /mcp.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
LOOPBACK_HOST = re.compile(r"--host 127\.0\.0\.1(\s|$)")


def _execstart_lines(name: str) -> list[str]:
    text = (SCRIPTS / name).read_text()
    return [line for line in text.splitlines() if line.startswith("ExecStart=")]


@pytest.mark.parametrize("name", ["shell-runner.service", "shell-runner.service.example"])
def test_unit_execstart_binds_loopback_only(name: str) -> None:
    lines = _execstart_lines(name)
    assert len(lines) == 1, f"{name}: expected exactly one ExecStart"
    assert LOOPBACK_HOST.search(lines[0]), f"{name}: ExecStart lacks --host 127.0.0.1"
    assert "0.0.0.0" not in lines[0]


def test_daemon_script_default_host_is_loopback() -> None:
    text = (SCRIPTS / "shell-runner-daemon.sh").read_text()
    assert re.search(r'^DEFAULT_HOST="127\.0\.0\.1"$', text, re.MULTILINE)


def test_daemon_script_fallback_still_uses_default_host() -> None:
    text = (SCRIPTS / "shell-runner-daemon.sh").read_text()
    assert re.search(
        r'^SHELL_RUNNER_HOST="\$\{SHELL_RUNNER_HOST:-\$DEFAULT_HOST\}"$', text, re.MULTILINE
    )


def test_loopback_pattern_rejects_other_hosts() -> None:
    assert not LOOPBACK_HOST.search("uvicorn app --host 0.0.0.0 --port 4111")
    assert not LOOPBACK_HOST.search("uvicorn app --host 127.0.0.10 --port 4111")
    assert not LOOPBACK_HOST.search("uvicorn app --port 4111")
```

- [ ] **Step 2: Run, expect pass (regression guard), then prove it can fail**

```bash
pixi run --frozen -e dev pytest tests/unit/test_service_bind.py -v
```
Expected: 5 passed (the files already comply, so this is a guard, not a red-first test). Demonstrate it fails on a violation: temporarily edit `scripts/shell-runner.service.example` `ExecStart` replacing `--host 127.0.0.1` with `--host 0.0.0.0`, rerun the command (expected: `test_unit_execstart_binds_loopback_only[shell-runner.service.example]` FAILS with "lacks --host 127.0.0.1"), then restore with `git checkout -- scripts/shell-runner.service.example`. Report SHA and tree.

Note: `addopts` has `--ignore=scripts`; that only skips collecting tests inside `scripts/`, reading files from it is fine.

- [ ] **Step 3: Quality gate and commit**

```bash
git status --short scripts/   # must be clean
pixi run --frozen -e quality-ci ruff check --fix src/ tests/   # sorts imports (I001); re-run tests if it changed files
pixi run --frozen -e quality-ci ruff check src/ tests/
pixi run --frozen -e quality-ci ruff format --check src/ tests/
git add tests/unit/test_service_bind.py
git commit -m "test(security): assert loopback-only bind in unit files and daemon script (#119)"
```

---

### Task 9: systemd user unit example and README

**Files:**
- Create: `scripts/shell-runner-notify.service.example`
- Modify: `README.md` (add a section before `## Approval precedence & agent_cap`)

- [ ] **Step 1: Create the unit example**

```ini
[Unit]
Description=Shell Runner desktop approval notifier
Documentation=https://github.com/Claire-s-Monster/shell-runner
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
WorkingDirectory=%h/ClaudeCode/Servers/shell-runner/development
Environment=PYTHONPATH=%h/ClaudeCode/Servers/shell-runner/development/src
ExecStart=%h/.pixi/bin/pixi run --frozen --manifest-path %h/ClaudeCode/Servers/shell-runner/development/pyproject.toml notify
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=graphical-session.target

# --frozen: never re-solve or rewrite pixi.lock (a newer local pixi would upgrade it to v7).
# NOTE: this unit points at the development/ tree, where the `notify` pixi task only
# exists after this branch is merged. Before then, point the paths at the worktree.
#
# Installation (systemd USER unit; needs notify-send and xdg-open installed):
#   cp shell-runner-notify.service.example ~/.config/systemd/user/shell-runner-notify.service
#   # Edit paths as needed
#   systemctl --user daemon-reload
#   systemctl --user enable --now shell-runner-notify
#
# If popups do not appear, the unit lacks your desktop session variables:
#   systemctl --user import-environment DISPLAY WAYLAND_DISPLAY DBUS_SESSION_BUS_ADDRESS
#   systemctl --user restart shell-runner-notify
#
# Logs: journalctl --user -u shell-runner-notify -f
```

- [ ] **Step 2: Add the README section**

````markdown
## Desktop approval notifier

`shell-runner-notify` pops up a desktop notification for each new T3/T4 approval prompt so you do not have to keep the UI page open. It is read-only (it polls `POST /pending`, never approves), uses only the standard library plus the system `notify-send` / `xdg-open`, and is separate from the server.

```bash
pixi run notify                       # defaults: poll every 3s
pixi run notify --interval 5 --url http://127.0.0.1:4111/pending \
  --ui-url http://127.0.0.1:8511/Pending_Prompts
```

Without the pixi task: `PYTHONPATH=src python -m shell_runner.notifier`. To run it as a systemd user service, copy `scripts/shell-runner-notify.service.example` (installation steps are in the file).

- One popup per new prompt (T4 is critical urgency), plus one reminder about 60 seconds before it expires if still undecided.
- Popups show agent, cwd, the command (cut at 120 characters), time left and any scope warning. Decide on the Pending Prompts page; the popup's "Open" button opens it. If your `notify-send` has no `--action` support, the URL is appended to the popup body instead.
- If the server is unreachable you get one popup, then quiet retries with back-off until it recovers.
- `POST /pending` is unauthenticated, so keep the server bound to `127.0.0.1` (guarded by `tests/unit/test_service_bind.py`).
````

- [ ] **Step 3: Verify and commit**

```bash
pixi run --frozen -e dev pytest tests/unit/test_service_bind.py -v   # new unit file must not break the bind test
git add scripts/shell-runner-notify.service.example README.md
git commit -m "docs(notifier): systemd user unit example and README section (#119)"
```
Expected: 5 passed (the bind test only reads `shell-runner.service`, `.service.example` and the daemon script; the notify unit has no `--host`). Report SHA and tree.

---

### Task 10: Full suite and acceptance

**Files:** none (verification only).

- [ ] **Step 1: Full suite and quality**

```bash
git rev-parse --short HEAD && pwd
pixi run --frozen -e dev pytest tests/ -v --ignore=tests/live --ignore=tests/debug
pixi run --frozen -e quality-ci ruff check --fix src/ tests/   # sorts imports (I001); re-run tests if it changed files
pixi run --frozen -e quality-ci ruff check src/ tests/
pixi run --frozen -e quality-ci ruff format --check src/ tests/ ui/ scripts/
pixi run --frozen -e quality-ci mypy src/
pixi run --frozen -e quality-ci bandit -c pyproject.toml -r src/
pixi run --frozen dead-code
pixi install --locked -e dev
```
Expected: everything green, `pixi.lock` unchanged (`git status --short pixi.lock` empty). Fix any `vulture` report for `notifier.py` (it should use every public name; `parse_args`/`main` are used). Record the SHA and tree path in the final report.

- [ ] **Step 2: Acceptance checklist (Part N criteria from the spec)**

- [ ] One popup per new prompt, no repeats, expired prompts skipped (`test_new_prompt_gives_one_popup`, `test_same_id_is_not_repeated`, `test_expired_prompts_are_skipped`).
- [ ] Click opens the UI URL (one thread per popup); URL in body when `--action` is unsupported (`test_action_popup_argv_and_click_opens_url`, `test_each_popup_gets_its_own_daemon_thread_spawn`, `test_without_action_support_url_goes_in_body_and_no_thread`).
- [ ] One reminder ~60s before expiry for still-undecided prompts (`test_reminder_fires_once_about_60s_before_expiry`, `test_no_reminder_when_prompt_was_decided`).
- [ ] Unreachable server: one popup, then back-off (`test_unreachable_gives_one_popup_then_backs_off`).
- [ ] Popups use argv lists, strip control characters, escape markup, mark truncation and say "decide on the Pending Prompts page" (`test_all_invocations_are_argv_lists_of_strings`, `test_build_body_sanitises_every_untrusted_field`, `test_build_body_marks_truncation_only_when_cut`, `test_build_body_order_and_content`).
- [ ] Notifier is read-only and stdlib only (`test_notifier_module_is_read_only`, `test_notifier_module_is_stdlib_only`).
- [ ] Static test: `--host 127.0.0.1` on the `ExecStart` of `scripts/shell-runner.service`, `DEFAULT_HOST` in `scripts/shell-runner-daemon.sh`, and `scripts/shell-runner.service.example`; port 4111 serves HTTP API and `/mcp`; no 4112 listener in the units (`tests/unit/test_service_bind.py`).
- [ ] Manual: the running server's listening sockets are loopback only. Run `ss -ltnp | grep -E ':4111|:4112'`; every line must show `127.0.0.1:4111` (or `:4112`). If not, file a separate issue.

- [ ] **Step 3: Manual end-to-end check (needs a desktop session and a running server)**

1. Install and start the unit: `cp scripts/shell-runner-notify.service.example ~/.config/systemd/user/shell-runner-notify.service`, `systemctl --user daemon-reload`, `systemctl --user enable --now shell-runner-notify`, then `systemctl --user status shell-runner-notify` shows active (otherwise follow the `import-environment` hint in the file). Alternative: `pixi run --frozen notify` in a terminal.
2. Start the UI if not running: `pixi run --frozen -e ui ui` (port 8511).
3. From a shell-runner client, trigger a T3 probe with the worktree as cwd: run `curl -sI https://example.com` through `shell_execute` with `cwd=/home/memento/ClaudeCode/Servers/shell-runner/worktrees/features`. Expect `decision=prompt_required`.
4. Within about 3 seconds a popup titled "shell-runner: T3 approval needed" appears, with agent, cwd as `~/ClaudeCode/...`, the command, "expires in 4m ..s" and "decide on the Pending Prompts page". No repeat popups follow.
5. Click "Open": the browser opens `http://127.0.0.1:8511/Pending_Prompts` showing the prompt. Leave it undecided: about 60s before expiry (about the 4-minute mark) one "T3 approval expiring" popup appears.
6. Stop the server (`systemctl --user stop shell-runner`) and confirm exactly one "server unreachable" popup, then silence; restart it and confirm no further popups for old prompts.
7. Report SHA and tree for this manual run, then hand back to the caller for the PR.
