# Approval Tray Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the noisy `notify-send` popups of #136/#137 with a silent Cinnamon panel icon that shows how many approval prompts are pending (issue #138, spec `docs/superpowers/specs/2026-11-25-approval-tray-design.md`).
**Architecture:** `src/shell_runner/notifier.py` stays a stdlib-only, read-only polling core that hands every fetch result to an `on_update(Snapshot)` callback and offers `run_loop(notifier, stop, interval)`. New `src/shell_runner/tray.py` is the only `gi` consumer: a pure, CI-tested layer (`render`, `plan_update`, `menu_entries`, `substitute_home`, `open_ui`) plus a thin, CI-uncovered GTK/XApp applier with a worker thread and `main(argv)`. A rewritten systemd user unit runs it with `/usr/bin/python3 -m shell_runner.tray`.
**Tech Stack:** Python 3.11 (pixi `dev` env for tests; system `/usr/bin/python3` 3.11 at runtime), stdlib (`urllib`, `threading`, `argparse`, `dataclasses`, `re`, `configparser` in tests), PyGObject from Debian `python3-gi` + `gir1.2-xapp-1.0` (Gtk 3.0, XApp 1.0, GLib), `xdg-open`, pytest, ruff, mypy, systemd user units.

---

## Constraints (read first, HARD)

- NEVER edit `pyproject.toml` or `pixi.lock`. CI pins pixi 0.49.0 with `--locked`; any `pyproject.toml` edit forces a v7 lock re-solve that breaks CI. No new dependencies, no new pixi tasks.
- NEVER call `/approve` or `approve_pending` from `notifier.py` or `tray.py`, and never use `shell=True`. The only action is `xdg-open <ui-url>` as an argv list.
- `test_notifier_module_is_stdlib_only` and `test_notifier_module_is_read_only` in `tests/unit/test_notifier.py` must stay green, unchanged, after every task.
- The full unit suite (`tests/unit`) must be green at the end of every task. Never leave it red between tasks.
- `tray.py` imports `gi` only inside functions, never at module level. The pixi suite must pass without `gi` installed.
- Work in `/home/memento/ClaudeCode/Servers/shell-runner/worktrees/features` on branch `feat/138-approval-tray` (check with `git branch --show-current` before starting). All paths below are relative to that directory.
- Commits stage explicit paths only (never `git add -A` / `git add .`) and use conventional messages ending in `(#138)`. Do not push.

### How to run commands

- Tests: `pixi run -e dev pytest tests/unit/<file>::<test> -q` (or a whole file / `tests/unit`). The `dev` env includes the `quality` feature with pytest, ruff and mypy; `pytest.ini_options` already sets `pythonpath = ["src", "tests", "."]`.
- Agents (no shell) run these through the pixi-task MCP `pixi_run_task` with `manifest_path=/home/memento/ClaudeCode/Servers/shell-runner/worktrees/features/pyproject.toml` and `environment="dev"`. The `test-unit` task is `pytest tests/unit -v`, so a narrow selection is `{"task_name": "test-unit", "args": ["-k", "<test name or file stem, e.g. test_tray>", "-q"], ...}`; the full suite is `{"task_name": "test"}` (use `"background": true` if it exceeds the transport window); lint/format/types are `{"task_name": "lint"}`, `{"task_name": "format-check"}`, `{"task_name": "type-check"}`.
- Lint / format / types: `pixi run -e dev lint` (ruff check src/), `pixi run -e dev format-check` (ruff format --check src/ tests/ ui/ scripts/), `pixi run -e dev type-check` (mypy src/). To fix import order and formatting of your own files only: `pixi run -e dev ruff check --fix <files>` and `pixi run -e dev ruff format <files>`.
- Git: agents use `mcp__git__execute_tool` (`git_add` with an explicit `files` list, `git_commit`).

### Per-task quality gate (run before every commit)

```bash
pixi run -e dev ruff check --fix <the .py files this task touched>   # sorts imports (I001); re-run tests if it changed anything
pixi run -e dev ruff format <the .py files this task touched>
pixi run -e dev lint
pixi run -e dev format-check
pixi run -e dev type-check
pixi run -e dev pytest tests/unit/test_notifier.py::test_notifier_module_is_stdlib_only tests/unit/test_notifier.py::test_notifier_module_is_read_only -q
pixi run -e dev pytest tests/unit -q
```

Expected: ruff/mypy report no errors, the two guard tests pass, and the unit suite ends with `passed` and no `failed`/`error` (skips are fine).

## File Structure

| File | Action | Single responsibility |
|---|---|---|
| `src/shell_runner/notifier.py` | Modify | Stdlib-only, read-only polling core: `http_fetch`, filtering + expiry sort, back-off, `Snapshot` / `on_update`, `run_loop`, `parse_args`, plain-text helpers `clean` / `format_left`, `open_detached` / `spawn_daemon`. |
| `src/shell_runner/tray.py` | Create | The only `gi` consumer: pure view layer (`render`, `plan_update`, `menu_entries`, `substitute_home`, `open_ui`) and the thin GTK/XApp applier, worker thread, signals and `main(argv)` entry point. |
| `tests/unit/test_notifier.py` | Modify | Tests of the polling core, fetcher, args, `run_loop`, text helpers and the stdlib-only / read-only guards. |
| `tests/unit/test_tray.py` | Create | Tests of the tray pure layer, `main()` exit codes with fake/missing `gi`, the gi-import AST guard, the read-only guard and the gi smoke test (skipped without gi). |
| `tests/unit/test_tray_service_unit.py` | Create | Static checks of `scripts/shell-runner-notify.service.example`. |
| `scripts/shell-runner-notify.service.example` | Rewrite | systemd user unit that runs the tray with the system Python. |
| `README.md` | Modify | The "Desktop approval notifier" section becomes "Desktop approval tray". |
| `docs/superpowers/specs/2026-11-19-approval-wait-and-notifier-design.md` | Modify | One superseded note under Part N. |
| `docs/superpowers/plans/2026-11-19-approval-notifier.md` | Modify | One-line superseded note at the top. |
| `pyproject.toml`, `pixi.lock` | NOT touched | — |

---

### Task 1: notifier.py text helpers (plain-text `clean`, `format_left`)

**Files:**
- Modify: `src/shell_runner/notifier.py`
- Modify: `tests/unit/test_notifier.py`

Note: between this task and Task 2 the old popup code still exists and now receives unescaped text. That is intended and short-lived (Task 2 deletes the popup code); nothing is released in between.

- [ ] **Step 1: Rewrite the affected tests**

In `tests/unit/test_notifier.py`:

1. In the `from shell_runner.notifier import (...)` block, remove `build_body`, `format_remaining` and `shorten_home`, and add `format_left`. The block becomes:

```python
from shell_runner.notifier import (
    DEFAULT_PENDING_URL,
    DEFAULT_UI_URL,
    MAX_RESPONSE_BYTES,
    RUN_TIMEOUT_S,
    DesktopNotifier,
    Notifier,
    clean,
    format_left,
    http_fetch,
    main,
    open_detached,
    parse_args,
    run_argv,
    spawn_daemon,
)
```

2. Replace `test_clean_escapes_markup` (whole function) with:

```python
def test_clean_does_not_escape_markup() -> None:
    assert clean("<b>x</b> & y") == "<b>x</b> & y"


def test_clean_still_strips_control_characters_around_markup() -> None:
    assert clean("<b>\x1bx\x00</b> &\x07 y") == "<b>x</b> & y"
```

3. Replace `test_clean_truncates_long_text_with_marker`, `test_clean_leaves_text_at_limit_unmarked` and `test_clean_truncates_before_escaping` (all three whole functions; the last one is deleted, its `&` case moves into the first) with:

```python
def test_clean_cuts_long_text_with_a_single_ellipsis() -> None:
    assert clean("x" * 121, 120) == "x" * 120 + "…"
    assert clean("&" * 130, 120) == "&" * 120 + "…"


def test_clean_leaves_text_at_limit_unmarked() -> None:
    assert clean("x" * 120, 120) == "x" * 120
    assert clean("<" * 120, 120) == "<" * 120
```

4. Delete these whole functions: `test_shorten_home`, `test_shorten_home_with_empty_or_root_home_leaves_cwd_unchanged`, `test_format_remaining`, `test_build_body_order_and_content`, `test_build_body_without_scope_warning_starts_with_agent`, `test_build_body_without_scope_warning_key_starts_with_agent`, `test_build_body_sanitises_every_untrusted_field`, `test_build_body_marks_truncation_only_when_cut`, `test_build_body_tolerates_missing_fields`, `test_build_body_tolerates_none_fields`. (Home substitution is retested in `tests/unit/test_tray.py`, Task 3.)

5. In their place (right after `test_clean_leaves_text_at_limit_unmarked`) add:

```python
@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (-5, "<1m left"),
        (0, "<1m left"),
        (59.9, "<1m left"),
        (60, "1m left"),
        (240, "4m left"),
        (3599, "59m left"),
        (3600, "1h 0m left"),
        (3661, "1h 1m left"),
        (7322, "2h 2m left"),
    ],
)
def test_format_left(seconds: float, expected: str) -> None:
    assert format_left(seconds) == expected
```

- [ ] **Step 2: Run, expect failure**

Run: `pixi run -e dev pytest tests/unit/test_notifier.py -q`
Expected: collection error `ImportError: cannot import name 'format_left' from 'shell_runner.notifier'`.

- [ ] **Step 3: Implement**

In `src/shell_runner/notifier.py`:

1. Delete the line `import html`.
2. Replace the line `TRUNCATED = "… (truncated)"` with `ELLIPSIS = "…"`.
3. Replace the whole `clean` function with:

```python
def clean(text: str, limit: int | None = None) -> str:
    """Return plain text: control/format characters dropped, optionally cut to `limit`.

    Drops Unicode categories Cc, Cf (e.g. U+202E bidi override), Zl and Zp
    (U+2028/2029). Newlines, tabs and NEL (U+0085) become spaces so multi-line
    commands stay readable. Nothing is escaped: the tray shows plain text, not
    Pango markup. When cut, the result is `limit` characters plus a single "…".
    """
    text = _WHITESPACE_CTRL.sub(" ", text)
    text = "".join(c for c in text if unicodedata.category(c) not in _DROPPED_CATEGORIES)
    if limit is not None and len(text) > limit:
        return text[:limit] + ELLIPSIS
    return text
```

4. Directly after the `format_remaining` function (it stays until Task 2), add:

```python
def format_left(seconds: float) -> str:
    """Time left for a menu row: `<1m left`, `Nm left` or `Nh Mm left`."""
    total = max(0, int(seconds))
    if total < 60:
        return "<1m left"
    if total < 3600:
        return f"{total // 60}m left"
    return f"{total // 3600}h {total % 3600 // 60}m left"
```

- [ ] **Step 4: Run, expect pass**

Run: `pixi run -e dev pytest tests/unit/test_notifier.py -q`
Expected: all passed, 0 failed (the remaining popup tests still pass: they only check plain substrings such as `cwd: ~/proj`).

- [ ] **Step 5: Quality gate and commit**

Run the per-task quality gate with files `src/shell_runner/notifier.py tests/unit/test_notifier.py`. Then:

```bash
git add src/shell_runner/notifier.py tests/unit/test_notifier.py
git commit -m "refactor(notifier): plain-text clean() with ellipsis cut and format_left (#138)"
```

---

### Task 2: notifier.py core (`Snapshot`, `on_update`, `run_loop`, no popups)

**Files:**
- Modify (full rewrite): `src/shell_runner/notifier.py`
- Modify: `tests/unit/test_notifier.py`

- [ ] **Step 1: Rewrite the tests**

In `tests/unit/test_notifier.py`, apply these edits in order. Functions are identified by name; "replace" means the whole `def` including its body.

1. Replace the module docstring and the whole import section (everything from the first line down to the closing `)` of the `from shell_runner.notifier import (` block) with:

```python
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
```

2. Keep `NOW`, `Clock`, `Feed` and `make_prompt` unchanged. Replace everything from `def make_notifier` down to and including `test_failed_unreachable_popup_is_not_retried_every_tick` (this contiguous block holds `make_notifier`, all popup-count tests, `test_t4_is_critical`, `test_scope_warning_is_first_line_of_body`, `test_seen_is_pruned_when_prompt_disappears`, all reminder tests, `test_outage_keeps_seen_and_reminded_and_does_not_repopup_after_recovery` and `test_failed_unreachable_popup_is_not_retried_every_tick`) with:

```python
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
    assert notifier_.last_prompts == [prompt]


def test_snapshot_is_frozen() -> None:
    snapshot = Snapshot(prompts=(), status="ok")
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.status = "unreachable"  # type: ignore[misc]


def test_every_fetching_tick_publishes_a_snapshot() -> None:
    notifier_, feed, clock, snapshots = make_notifier()
    feed.prompts = [make_prompt()]
    for _ in range(3):
        notifier_.tick()
        clock.advance(3)
    assert [ids(s) for s in snapshots] == [["p1"], ["p1"], ["p1"]]


def test_snapshot_follows_the_current_prompt_list() -> None:
    notifier_, feed, _clock, snapshots = make_notifier()
    feed.prompts = [make_prompt("p1")]
    notifier_.tick()
    feed.prompts = [make_prompt("p1"), make_prompt("p2")]
    notifier_.tick()
    feed.prompts = []
    notifier_.tick()
    assert [ids(s) for s in snapshots] == [["p1"], ["p1", "p2"], []]
    assert notifier_.last_prompts == []


def test_last_prompts_are_sorted_by_parsed_expiry() -> None:
    plus_two = timezone(timedelta(hours=2))
    # Sorts after "late" as a string, but expires 60s from NOW.
    offset_expiry = (NOW + timedelta(seconds=60)).astimezone(plus_two).isoformat()
    notifier_, feed, _clock, snapshots = make_notifier()
    feed.prompts = [
        make_prompt("late", expires_in=300),
        make_prompt("soon", expires_in=30),
        make_prompt("offset", expires_at=offset_expiry),
        make_prompt("mid", expires_in=120),
    ]
    notifier_.tick()
    assert [p["id"] for p in notifier_.last_prompts] == ["soon", "offset", "mid", "late"]
    assert ids(snapshots[0]) == ["soon", "offset", "mid", "late"]


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
```

3. Replace `test_tick_takes_unreachable_path_on_truncated_body` with:

```python
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
```

4. Replace `test_http_fetch_failures_are_what_tick_treats_as_unreachable` with:

```python
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
```

5. Replace everything from the line `UI_URL = "http://127.0.0.1:8511/Pending_Prompts"` down to and including `test_opener_failure_in_worker_does_not_propagate` (this deletes `HELP_WITH_ACTION`, `HELP_WITHOUT_ACTION`, `FakeRun`, `make_desktop` and every `DesktopNotifier` / `run_argv` / `notify-send` test: `test_action_popup_argv_and_click_opens_url`, `test_dismissed_popup_does_not_open_anything`, `test_each_popup_gets_its_own_daemon_thread_spawn`, `test_without_action_support_url_goes_in_body_and_no_thread`, `test_action_failure_at_runtime_falls_back_to_plain_popup`, `test_help_probe_runs_once`, `test_all_invocations_are_argv_lists_of_strings`, `test_missing_notify_send_does_not_crash`, `test_probe_and_plain_calls_get_a_finite_timeout_but_wait_call_does_not`, `test_run_argv_timeout_becomes_oserror`, `test_hung_plain_popup_is_handled_without_raising`, `test_run_argv_decodes_leniently_and_drops_stderr`, `test_run_argv_survives_non_utf8_output`, `test_stderr_noise_does_not_hide_the_open_click`, `test_dash_leading_title_and_body_come_after_double_dash`, `test_opener_failure_in_worker_does_not_propagate`) with the two kept tests:

```python
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
```

6. Replace `test_parse_args_defaults` and `test_parse_args_overrides` with:

```python
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
```

7. Replace `test_main_loops_until_keyboard_interrupt` with:

```python
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
```

8. Replace `test_slow_fetch_does_not_popup_for_prompt_that_expired_meanwhile` and `test_log_escapes_newlines_in_prompt_id` with:

```python
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
```

9. Leave unchanged: every `clean` / `format_left` test from Task 1, `_serve`, `_url`, `_serve_truncated`, every `test_http_fetch_*` and `test_json_depth_*` test, `test_parse_args_rejects_non_http_urls`, `test_parse_args_rejects_hostless_urls`, `test_parse_args_accepts_uppercase_scheme`, `test_parse_args_rejects_bad_interval`, `test_parse_args_accepts_fractional_interval`, `test_parse_args_accepts_interval_bounds`, `test_notifier_module_is_stdlib_only`, `test_notifier_module_is_read_only`.

- [ ] **Step 2: Run, expect failure**

Run: `pixi run -e dev pytest tests/unit/test_notifier.py -q`
Expected: collection error `ImportError: cannot import name 'Snapshot' from 'shell_runner.notifier'`.

- [ ] **Step 3: Implement (replace the whole file)**

Replace the entire contents of `src/shell_runner/notifier.py` with:

```python
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
    """
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
```

This removes, as the spec requires: `CMD_LIMIT`, `REMIND_BEFORE_S`, `TRUNCATED`, `RUN_TIMEOUT_S`, `Runner`, `run_argv`, `DesktopNotifier`, `shorten_home`, `format_remaining`, `build_body`, `_MISSING`, `Notifier.home` / `seen` / `reminded` / `notify` / `_popup`, urgency, the `first` flag, and the module `main()` / `__main__` block (the tray owns the entry point). It keeps `http_fetch`, `_read_bounded`, `_json_depth_exceeds`, `MAX_JSON_DEPTH`, the back-off, the filtering, `_log`, `parse_expiry`, `_http_url`, `_positive_interval`, `open_detached` and `spawn_daemon`.

- [ ] **Step 4: Run, expect pass**

Run: `pixi run -e dev pytest tests/unit/test_notifier.py -q`
Expected: all passed, 0 failed. Then confirm the guards explicitly:
`pixi run -e dev pytest tests/unit/test_notifier.py::test_notifier_module_is_stdlib_only tests/unit/test_notifier.py::test_notifier_module_is_read_only -q` → 2 passed.

- [ ] **Step 5: Quality gate and commit**

Run the per-task quality gate with files `src/shell_runner/notifier.py tests/unit/test_notifier.py`. Then:

```bash
git add src/shell_runner/notifier.py tests/unit/test_notifier.py
git commit -m "feat(notifier): Snapshot/on_update polling core and run_loop, drop popups (#138)"
```

---

### Task 3: tray.py pure layer (render, plan_update, home substitution, opener)

**Files:**
- Create: `src/shell_runner/tray.py`
- Create: `tests/unit/test_tray.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_tray.py`:

```python
"""Unit tests for the approval tray (issue #138).

The pure layer (render, plan_update, menu_entries, substitute_home, open_ui)
needs no gi; nothing here touches the desktop, the network or real time.
"""

from __future__ import annotations

import copy
import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from shell_runner import tray
from shell_runner.notifier import Snapshot
from shell_runner.tray import (
    Row,
    View,
    menu_entries,
    open_ui,
    plan_update,
    render,
    substitute_home,
)

NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)
HOME = "/home/me"
UI_URL = "http://127.0.0.1:8511/Pending_Prompts"
MISSING = object()
TAIL = [("separator", ""), ("open", "Open Pending Prompts")]


def make_prompt(pid: str = "p1", tier: Any = 3, expires_in: float = 300, **extra: Any) -> dict:
    prompt = {
        "id": pid,
        "agent_id": "agent-1",
        "raw_cmd": "ls /home/me/proj",
        "command_tier": tier,
        "expires_at": (NOW + timedelta(seconds=expires_in)).isoformat(),
        "approve_decision": None,
        **extra,
    }
    return {key: value for key, value in prompt.items() if value is not MISSING}


def ok(*prompts: dict) -> Snapshot:
    return Snapshot(prompts=tuple(prompts), status="ok")


def down(*prompts: dict) -> Snapshot:
    return Snapshot(prompts=tuple(prompts), status="unreachable")


def view_of(snapshot: Snapshot, max_items: int = 10, now: datetime = NOW) -> View:
    return render(snapshot, now, max_items, HOME)


def row_ids(view: View) -> list[str]:
    return [row.identity[0] for row in view.rows]


# --- render: states -----------------------------------------------------------


def test_zero_pending_state() -> None:
    view = view_of(ok())
    assert (view.icon_name, view.label, view.tooltip) == (
        "security-high-symbolic",
        "",
        "shell-runner: no pending prompts",
    )
    assert view.header is None
    assert view.rows == ()
    assert view.overflow == 0
    assert menu_entries(view) == TAIL


def test_n_pending_state() -> None:
    view = view_of(ok(make_prompt("p1"), make_prompt("p2")))
    assert (view.icon_name, view.label, view.tooltip) == (
        "dialog-warning-symbolic",
        "2",
        "shell-runner: 2 pending",
    )
    assert view.header is None
    assert row_ids(view) == ["p1", "p2"]


def test_t4_count_is_in_the_tooltip_only_when_present() -> None:
    view = view_of(ok(make_prompt("a", tier=4), make_prompt("b", tier=4), make_prompt("c")))
    assert view.tooltip == "shell-runner: 3 pending (2 × T4)"
    assert view_of(ok(make_prompt("c"))).tooltip == "shell-runner: 1 pending"


@pytest.mark.parametrize(("count", "label"), [(99, "99"), (100, "99+")])
def test_label_caps_at_99_plus(count: int, label: str) -> None:
    prompts = [make_prompt(f"p{i}") for i in range(count)]
    view = view_of(ok(*prompts))
    assert view.label == label
    assert view.tooltip == f"shell-runner: {count} pending"


def test_unreachable_with_last_known_rows() -> None:
    view = view_of(down(make_prompt("p1")))
    assert (view.icon_name, view.label, view.tooltip) == (
        "network-offline-symbolic",
        "?",
        "shell-runner unreachable — retrying (back-off up to 60s)",
    )
    assert view.header == "Server unreachable — showing last known"
    assert row_ids(view) == ["p1"]
    assert menu_entries(view)[0] == ("header", "Server unreachable — showing last known")


def test_unreachable_without_last_known_rows() -> None:
    view = view_of(down())
    assert view.header == "Server unreachable"
    assert view.rows == ()
    assert menu_entries(view) == [("header", "Server unreachable"), *TAIL]


def test_unreachable_stale_rows_are_refiltered_against_now() -> None:
    view = view_of(down(make_prompt("old", expires_in=-1), make_prompt("p2")))
    assert view.header == "Server unreachable — showing last known"
    assert row_ids(view) == ["p2"]


def test_unreachable_with_every_stale_prompt_expired_shows_the_empty_header() -> None:
    view = view_of(down(make_prompt("a", expires_in=-1), make_prompt("b", expires_in=0)))
    assert view.header == "Server unreachable"
    assert view.rows == ()
    assert menu_entries(view) == [("header", "Server unreachable"), *TAIL]


def test_rows_that_expired_since_the_poll_are_dropped() -> None:
    snapshot = ok(make_prompt("p1", expires_in=300), make_prompt("p2", expires_in=600))
    later = view_of(snapshot, now=NOW + timedelta(seconds=300))  # p1 expires exactly now
    assert row_ids(later) == ["p2"]
    assert later.label == "1"
    gone = view_of(snapshot, now=NOW + timedelta(seconds=601))
    assert gone.icon_name == "security-high-symbolic"
    assert gone.rows == ()


@pytest.mark.parametrize("bad", ["garbage", 12345, None, MISSING])
def test_row_with_unparseable_expiry_is_dropped(bad: Any) -> None:
    view = view_of(ok(make_prompt("bad", expires_at=bad), make_prompt("p2")))
    assert row_ids(view) == ["p2"]


def test_render_does_not_mutate_the_snapshot() -> None:
    snapshot = ok(make_prompt("p1", raw_cmd="cat /home/me/x"), make_prompt("p2", tier="x"))
    before = copy.deepcopy(snapshot.prompts)
    view_of(snapshot)
    assert snapshot.prompts == before


# --- render: rows ---------------------------------------------------------------


def test_row_text_identity_and_label() -> None:
    view = view_of(ok(make_prompt("p1", tier=4, expires_in=240)))
    (row,) = view.rows
    assert row.identity == ("p1", "T4", "agent-1", "ls ~/proj")
    assert row.text_without_left == "T4 · agent-1 · ls ~/proj"
    assert row.left_text == "4m left"
    assert row.label == "T4 · agent-1 · ls ~/proj · 4m left"


def test_command_is_cut_to_the_menu_limit() -> None:
    assert tray.MENU_CMD_LIMIT == 50
    long_view = view_of(ok(make_prompt(raw_cmd="x" * 80)))
    assert long_view.rows[0].identity[3] == "x" * 50 + "…"
    exact_view = view_of(ok(make_prompt(raw_cmd="x" * 50)))
    assert exact_view.rows[0].identity[3] == "x" * 50


def test_home_is_shortened_before_the_cut() -> None:
    cmd = "cat /home/me/" + "y" * 48  # 61 chars raw, 54 after "~", then cut to 50
    view = view_of(ok(make_prompt(raw_cmd=cmd)))
    assert view.rows[0].identity[3] == "cat ~/" + "y" * 44 + "…"


def test_row_text_is_plain_not_escaped() -> None:
    view = view_of(ok(make_prompt(raw_cmd="echo <b>&</b>\nnext\x1b")))
    assert view.rows[0].identity[3] == "echo <b>&</b> next"


# --- render: fallbacks (render is total) ------------------------------------------


@pytest.mark.parametrize("tier", [MISSING, "4", None, 4.0, True])
def test_bad_command_tier_shows_t_question_and_is_not_counted_as_t4(tier: Any) -> None:
    view = view_of(ok(make_prompt("p1", tier=tier)))
    assert view.rows[0].text_without_left.startswith("T? · ")
    assert view.tooltip == "shell-runner: 1 pending"


@pytest.mark.parametrize("agent", [MISSING, 42, None])
def test_bad_agent_id_shows_question_mark(agent: Any) -> None:
    view = view_of(ok(make_prompt("p1", agent_id=agent)))
    assert view.rows[0].text_without_left == "T3 · ? · ls ~/proj"


def test_long_agent_id_is_cut_to_the_menu_limit() -> None:
    assert view_of(ok(make_prompt(agent_id="a" * 80))).rows[0].identity[2] == "a" * 50 + "…"


@pytest.mark.parametrize("cmd", [MISSING, ["ls"], None])
def test_bad_raw_cmd_shows_empty_command(cmd: Any) -> None:
    view = view_of(ok(make_prompt("p1", raw_cmd=cmd)))
    assert view.rows[0].text_without_left == "T3 · agent-1 · "
    assert view.rows[0].identity[3] == ""


# --- render: max-items cap and menu entries ----------------------------------------


def test_max_items_caps_rows_and_adds_an_overflow_row() -> None:
    prompts = [make_prompt(f"p{i}") for i in range(12)]
    view = view_of(ok(*prompts), max_items=10)
    assert len(view.rows) == 10
    assert view.overflow == 2
    assert view.label == "12"  # the count covers every prompt, not just the shown rows
    entries = menu_entries(view)
    assert [kind for kind, _text in entries] == ["row"] * 10 + ["overflow", "separator", "open"]
    assert entries[10] == ("overflow", "… and 2 more")


def test_no_overflow_row_when_everything_fits() -> None:
    prompts = [make_prompt(f"p{i}") for i in range(12)]
    view = view_of(ok(*prompts), max_items=12)
    assert view.overflow == 0
    assert "overflow" not in [kind for kind, _text in menu_entries(view)]


def test_header_overflow_separator_and_open_do_not_count_toward_max_items() -> None:
    view = view_of(down(make_prompt("a"), make_prompt("b"), make_prompt("c")), max_items=2)
    assert menu_entries(view) == [
        ("header", "Server unreachable — showing last known"),
        ("row", view.rows[0].label),
        ("row", view.rows[1].label),
        ("overflow", "… and 1 more"),
        *TAIL,
    ]


def test_view_structure_and_chrome() -> None:
    view = view_of(ok(make_prompt("p1")))
    assert view.chrome == (view.icon_name, view.label, view.tooltip)
    assert view.structure == ((view.rows[0].identity,), False, 0)
    assert view_of(down()).structure == ((), True, 0)


# --- home substitution ----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("cp /home/me/a /home/me/b", "cp ~/a ~/b"),
        ("cd /home/me", "cd ~"),
        ("ls /home/me /tmp", "ls ~ /tmp"),
        ("ls /home/me\t/tmp", "ls ~\t/tmp"),
        ("ls /home/me2/x", "ls /home/me2/x"),
        ("ls /home/meow", "ls /home/meow"),
        ("/srv/x", "/srv/x"),
    ],
)
def test_substitute_home(text: str, expected: str) -> None:
    assert substitute_home(text, HOME) == expected


def test_substitute_home_strips_a_trailing_slash_on_home() -> None:
    assert substitute_home("cat /home/me/x", "/home/me/") == "cat ~/x"


@pytest.mark.parametrize("home", ["", "/"])
def test_substitute_home_with_empty_home_is_a_no_op(home: str) -> None:
    assert substitute_home("cat /home/me/x", home) == "cat /home/me/x"


def test_substitute_home_treats_home_literally_not_as_a_regex() -> None:
    assert substitute_home("ls /home/aXb/x", "/home/a.b") == "ls /home/aXb/x"


# --- plan_update truth table ------------------------------------------------------

ROW = Row(("p1", "T3", "a", "ls"), "T3 · a · ls", "4m left")
BASE = View(
    icon_name="dialog-warning-symbolic",
    label="1",
    tooltip="shell-runner: 1 pending",
    header=None,
    rows=(ROW,),
    overflow=0,
)
STRUCTURAL_CHANGES = [
    pytest.param(
        dataclasses.replace(BASE, rows=(Row(("p2", "T3", "a", "ls"), "T3 · a · ls", "4m left"),)),
        id="row-identity",
    ),
    pytest.param(
        dataclasses.replace(BASE, header="Server unreachable — showing last known"),
        id="stale-header",
    ),
    pytest.param(dataclasses.replace(BASE, overflow=1), id="overflow-count"),
]


@pytest.mark.parametrize("menu_open", [False, True])
@pytest.mark.parametrize("applied_chrome", [None, tray.STARTING_CHROME])
def test_plan_first_update_always_rebuilds(
    menu_open: bool, applied_chrome: tuple[str, str, str] | None
) -> None:
    assert plan_update(None, BASE, menu_open, applied_chrome) == {
        "set_chrome": True,
        "relabel": False,
        "rebuild": True,
    }


def test_plan_no_change_does_nothing() -> None:
    assert plan_update(BASE, BASE, False, BASE.chrome) == {
        "set_chrome": False,
        "relabel": False,
        "rebuild": False,
    }


def test_plan_chrome_only() -> None:
    new = dataclasses.replace(BASE, label="2", tooltip="shell-runner: 2 pending")
    assert plan_update(BASE, new, False, BASE.chrome) == {
        "set_chrome": True,
        "relabel": False,
        "rebuild": False,
    }


def test_plan_chrome_is_compared_with_the_applied_chrome_not_the_applied_view() -> None:
    # Regression: the structure stayed at BASE (a deferred rebuild), but chrome B
    # was already applied. Going back to BASE's chrome must set it again, and
    # staying at B must not.
    chrome_b = dataclasses.replace(BASE, label="2", tooltip="shell-runner: 2 pending")
    assert plan_update(BASE, BASE, False, chrome_b.chrome)["set_chrome"] is True
    assert plan_update(BASE, chrome_b, False, chrome_b.chrome)["set_chrome"] is False


@pytest.mark.parametrize("menu_open", [False, True])
def test_plan_relabel_only_even_while_open(menu_open: bool) -> None:
    new = dataclasses.replace(BASE, rows=(ROW._replace(left_text="3m left"),))
    assert plan_update(BASE, new, menu_open, BASE.chrome) == {
        "set_chrome": False,
        "relabel": True,
        "rebuild": False,
    }


@pytest.mark.parametrize("new", STRUCTURAL_CHANGES)
def test_plan_rebuild_on_each_structural_component(new: View) -> None:
    assert plan_update(BASE, new, False, BASE.chrome) == {
        "set_chrome": False,
        "relabel": False,
        "rebuild": True,
    }


@pytest.mark.parametrize("new", STRUCTURAL_CHANGES)
def test_plan_rebuild_is_deferred_while_the_menu_is_open(new: View) -> None:
    assert plan_update(BASE, new, True, BASE.chrome) == {
        "set_chrome": False,
        "relabel": False,
        "rebuild": "deferred",
    }


def test_plan_structural_and_chrome_change_together() -> None:
    new = dataclasses.replace(BASE, overflow=1, label="11")
    assert plan_update(BASE, new, False, BASE.chrome) == {
        "set_chrome": True,
        "relabel": False,
        "rebuild": True,
    }


# --- opener -----------------------------------------------------------------------


def test_open_ui_runs_xdg_open_as_an_argv_list(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(tray, "open_detached", calls.append)
    open_ui(UI_URL)
    assert calls == [["xdg-open", UI_URL]]
    assert all(isinstance(arg, str) for arg in calls[0])


def test_open_ui_failure_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing(argv: list[str]) -> None:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(tray, "open_detached", failing)
    open_ui(UI_URL)  # must not raise
    assert "xdg-open failed" in capsys.readouterr().err
```

- [ ] **Step 2: Run, expect failure**

Run: `pixi run -e dev pytest tests/unit/test_tray.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'shell_runner.tray'`.

- [ ] **Step 3: Implement**

Create `src/shell_runner/tray.py`:

```python
"""Silent Cinnamon tray indicator for pending shell-runner approvals (issue #138).

The pure layer (render, plan_update, menu_entries, substitute_home, open_ui)
has no gi dependency and is fully unit-tested. This is the only module that
uses gi, and only inside functions, so importing it never needs gi.
Read-only: the only action is opening the UI page with an argv-list xdg-open.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, NamedTuple, TypedDict

from shell_runner.notifier import (
    Snapshot,
    _log,
    clean,
    format_left,
    open_detached,
    parse_expiry,
)

MENU_CMD_LIMIT = 50
LABEL_CAP = 99
SEP = " · "
ICON_IDLE = "security-high-symbolic"
ICON_PENDING = "dialog-warning-symbolic"
ICON_OFFLINE = "network-offline-symbolic"
TOOLTIP_IDLE = "shell-runner: no pending prompts"
TOOLTIP_OFFLINE = "shell-runner unreachable — retrying (back-off up to 60s)"
HEADER_STALE = "Server unreachable — showing last known"
HEADER_EMPTY = "Server unreachable"
OPEN_LABEL = "Open Pending Prompts"
TOOLTIP_STARTING = "shell-runner: starting…"
# (icon_name, label, tooltip) applied before the first poll; same order as View.chrome.
STARTING_CHROME = (ICON_IDLE, "", TOOLTIP_STARTING)

# (id, tier text, agent text, command text)
Identity = tuple[str, str, str, str]
# (row identities, stale/unreachable header present, overflow count)
Structure = tuple[tuple[Identity, ...], bool, int]
MenuKind = Literal["header", "row", "overflow", "separator", "open"]


class Row(NamedTuple):
    identity: Identity
    text_without_left: str
    left_text: str

    @property
    def label(self) -> str:
        return f"{self.text_without_left}{SEP}{self.left_text}"


@dataclass(frozen=True)
class View:
    icon_name: str
    label: str
    tooltip: str
    header: str | None
    rows: tuple[Row, ...]
    overflow: int

    @property
    def chrome(self) -> tuple[str, str, str]:
        return (self.icon_name, self.label, self.tooltip)

    @property
    def structure(self) -> Structure:
        return (tuple(row.identity for row in self.rows), self.header is not None, self.overflow)


class Plan(TypedDict):
    set_chrome: bool
    relabel: bool
    rebuild: bool | Literal["deferred"]


def substitute_home(text: str, home: str) -> str:
    """Replace every `home` followed by `/`, whitespace or the end of `text` with `~`."""
    home = home.rstrip("/")
    if not home:
        return text
    return re.sub(re.escape(home) + r"(?=/|\s|\Z)", "~", text)


def _expiry(prompt: dict[str, Any]) -> datetime | None:
    value = prompt.get("expires_at")
    return parse_expiry(value) if isinstance(value, str) else None


def _row(prompt: dict[str, Any], left_s: float, home: str) -> tuple[Row, bool]:
    """Build one menu row; also return whether the prompt counts as T4."""
    tier = prompt.get("command_tier")
    tier_ok = isinstance(tier, int) and not isinstance(tier, bool)
    tier_text = f"T{tier}" if tier_ok else "T?"
    agent = prompt.get("agent_id")
    agent_text = clean(agent, MENU_CMD_LIMIT) if isinstance(agent, str) else "?"
    cmd = prompt.get("raw_cmd")
    cmd_text = clean(substitute_home(clean(cmd), home), MENU_CMD_LIMIT) if isinstance(cmd, str) else ""
    identity = (str(prompt.get("id")), tier_text, agent_text, cmd_text)
    text = SEP.join((tier_text, agent_text, cmd_text))
    return Row(identity, text, format_left(left_s)), tier_ok and tier == 4


def _live_rows(
    prompts: tuple[dict[str, Any], ...], now: datetime, home: str
) -> list[tuple[Row, bool]]:
    rows: list[tuple[Row, bool]] = []
    for prompt in prompts:
        expiry = _expiry(prompt)
        if expiry is None or expiry <= now:
            continue
        rows.append(_row(prompt, (expiry - now).total_seconds(), home))
    return rows


def _chrome(status: str, count: int, t4_count: int) -> tuple[str, str, str]:
    if status == "unreachable":
        return ICON_OFFLINE, "?", TOOLTIP_OFFLINE
    if count == 0:
        return ICON_IDLE, "", TOOLTIP_IDLE
    label = str(count) if count <= LABEL_CAP else f"{LABEL_CAP}+"
    tooltip = f"shell-runner: {count} pending"
    if t4_count:
        tooltip += f" ({t4_count} × T4)"
    return ICON_PENDING, label, tooltip


def render(snapshot: Snapshot, now: datetime, max_items: int, home: str) -> View:
    """Pure: turn a Snapshot into the icon chrome and menu rows. Never raises on row data.

    `now` is an aware UTC datetime. Rows are re-filtered against it, so stale
    (unreachable) rows and rows that expired since the poll drop out.
    """
    built = _live_rows(snapshot.prompts, now, home)
    shown = tuple(row for row, _is_t4 in built[:max_items])
    t4_count = sum(1 for _built_row, is_t4 in built if is_t4)
    icon_name, label, tooltip = _chrome(snapshot.status, len(built), t4_count)
    header: str | None = None
    if snapshot.status == "unreachable":
        # Chosen from the rows that survive the re-filter: if no last-known
        # prompt is still live, there is nothing "last known" to show.
        header = HEADER_STALE if built else HEADER_EMPTY
    return View(
        icon_name=icon_name,
        label=label,
        tooltip=tooltip,
        header=header,
        rows=shown,
        overflow=len(built) - len(shown),
    )


def menu_entries(view: View) -> list[tuple[MenuKind, str]]:
    """The menu, top to bottom: header?, rows, overflow?, separator, Open."""
    entries: list[tuple[MenuKind, str]] = []
    if view.header is not None:
        entries.append(("header", view.header))
    for row in view.rows:
        entries.append(("row", row.label))
    if view.overflow:
        entries.append(("overflow", f"… and {view.overflow} more"))
    entries.append(("separator", ""))
    entries.append(("open", OPEN_LABEL))
    return entries


def plan_update(
    old_view: View | None,
    new_view: View,
    menu_open: bool,
    applied_chrome: tuple[str, str, str] | None,
) -> Plan:
    """Pure: decide how to apply `new_view`.

    `old_view` is the view whose STRUCTURE was last applied to the menu
    (`applied_view`); it is None on the first update, which always means a full
    rebuild. `applied_chrome` is the chrome last applied to the icon, tracked
    independently of the structure: a deferred rebuild leaves `old_view` behind
    while the chrome keeps moving, so chrome is never compared with `old_view`.
    """
    if old_view is None:
        return {"set_chrome": True, "relabel": False, "rebuild": True}
    set_chrome = applied_chrome != new_view.chrome
    if old_view.structure == new_view.structure:
        old_lefts = [row.left_text for row in old_view.rows]
        new_lefts = [row.left_text for row in new_view.rows]
        return {"set_chrome": set_chrome, "relabel": old_lefts != new_lefts, "rebuild": False}
    if menu_open:
        return {"set_chrome": set_chrome, "relabel": False, "rebuild": "deferred"}
    return {"set_chrome": set_chrome, "relabel": False, "rebuild": True}


def open_ui(ui_url: str) -> None:
    """Open the Pending Prompts page; argv list, no shell. Failures are logged."""
    try:
        open_detached(["xdg-open", ui_url])
    except OSError as exc:
        _log(f"xdg-open failed: {exc}")
```

- [ ] **Step 4: Run, expect pass**

Run: `pixi run -e dev pytest tests/unit/test_tray.py -q`
Expected: all passed, 0 failed.

- [ ] **Step 5: Quality gate and commit**

Run the per-task quality gate with files `src/shell_runner/tray.py tests/unit/test_tray.py` (ruff may re-wrap the long `cmd_text` line and reorder the `shell_runner.notifier` import names; that is expected). Then:

```bash
git add src/shell_runner/tray.py tests/unit/test_tray.py
git commit -m "feat(tray): pure render, plan_update and menu layer (#138)"
```

---

### Task 4: tray.py GTK layer (applier, worker, signals, `main`)

**Files:**
- Modify: `src/shell_runner/tray.py`
- Modify: `tests/unit/test_tray.py`

Keep the `_Tray` applier a thin shim: every decision lives in `render` / `plan_update` / `menu_entries` (Task 3). CI has no gi, so it is tested with `unittest.mock.MagicMock` stand-ins for GLib, Gtk and XApp (starting state, worker crash, chrome tracking, deferred rebuild). Real GTK behaviour (widget rendering, the hide→activate→idle ordering, `main()`'s run path, signals) is NOT covered by CI; it is covered only by the gi smoke test (skipped in CI) and the manual checklist in Task 7. Do not add an icon adapter protocol or a hand-written fake GTK.

- [ ] **Step 1: Write the failing tests**

1. In `tests/unit/test_tray.py`, replace the import section (from `import copy` through the closing `)` of `from shell_runner.tray import (`) with:

```python
import ast
import copy
import dataclasses
import importlib
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from shell_runner import tray
from shell_runner.notifier import Snapshot, parse_args
from shell_runner.tray import (
    Row,
    View,
    menu_entries,
    open_ui,
    plan_update,
    render,
    substitute_home,
)
```

2. Append to the end of `tests/unit/test_tray.py`:

```python
# --- module guards and entry point -------------------------------------------------


def _imports_gi(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(alias.name.split(".")[0] == "gi" for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return (node.module or "").split(".")[0] == "gi"
    return False


def test_tray_imports_gi_only_inside_functions() -> None:
    tree = ast.parse(Path(tray.__file__).read_text())
    in_functions = {
        id(child)
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
        for child in ast.walk(fn)
    }
    gi_imports = [node for node in ast.walk(tree) if _imports_gi(node)]
    assert gi_imports, "main() must import gi"
    assert all(id(node) in in_functions for node in gi_imports)


def test_tray_module_is_read_only() -> None:
    source = Path(tray.__file__).read_text()
    assert "approve_pending" not in source
    assert "/approve" not in source
    assert "shell=True" not in source


def test_tray_imports_and_main_returns_2_without_gi(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "gi", None)  # makes `import gi` raise ImportError
    monkeypatch.delitem(sys.modules, "shell_runner.tray")
    fresh = importlib.import_module("shell_runner.tray")  # importing needs no gi
    assert fresh.main([]) == 2
    err = capsys.readouterr().err
    assert "python3-gi" in err
    assert "gir1.2-xapp-1.0" in err
    assert err.count("\n") == 1


def _fake_gi(monkeypatch: pytest.MonkeyPatch, *, display: bool, typelib: bool = True) -> list:
    required: list[tuple[str, str]] = []

    def require_version(namespace: str, version: str) -> None:
        if not typelib:
            raise ValueError(f"Namespace {namespace} not available")
        required.append((namespace, version))

    def init_check() -> tuple[bool, list[str]]:
        return display, []

    gi = types.ModuleType("gi")
    gi.require_version = require_version  # type: ignore[attr-defined]
    repository = types.ModuleType("gi.repository")
    repository.GLib = types.SimpleNamespace()  # type: ignore[attr-defined]
    repository.XApp = types.SimpleNamespace()  # type: ignore[attr-defined]
    repository.Gtk = types.SimpleNamespace(init_check=init_check)  # type: ignore[attr-defined]
    gi.repository = repository  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    return required


def test_main_returns_2_when_a_typelib_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_gi(monkeypatch, display=True, typelib=False)
    assert tray.main([]) == 2


def test_main_requires_gtk3_and_xapp1_and_returns_3_without_a_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required = _fake_gi(monkeypatch, display=False)
    assert tray.main([]) == 3
    assert set(required) == {("Gtk", "3.0"), ("XApp", "1.0")}


def test_main_parses_args_before_the_display_check(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_gi(monkeypatch, display=False)
    with pytest.raises(SystemExit) as info:
        tray.main(["--max-items", "0"])
    assert info.value.code == 2  # argparse error: also exit 2, so systemd does not restart


def test_gi_smoke_status_icon_has_the_methods_the_applier_calls() -> None:
    gi = pytest.importorskip("gi")  # skipped in CI and in the pixi env
    try:
        gi.require_version("Gtk", "3.0")
        gi.require_version("XApp", "1.0")
    except ValueError:
        pytest.skip("Gtk 3.0 or XApp 1.0 typelib not installed")
    from gi.repository import Gtk, XApp

    assert isinstance(Gtk.init_check(), tuple)  # main() indexes element 0
    icon = XApp.StatusIcon()  # constructed without a main loop
    for name in (
        "set_icon_name",
        "set_tooltip_text",
        "set_label",
        "set_primary_menu",
        "set_secondary_menu",
    ):
        assert callable(getattr(icon, name)), name


# --- applier logic with MagicMock stand-ins for GLib/Gtk/XApp (runs in CI) ----------


def make_tray(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, MagicMock, MagicMock, MagicMock]:
    """A _Tray over MagicMocks; render() sees the fixed NOW. Returns (tray, glib, icon, menu)."""
    monkeypatch.setattr(tray, "_utcnow", lambda: NOW)
    glib, gtk, xapp = MagicMock(), MagicMock(), MagicMock()
    instance = tray._Tray(glib, gtk, xapp, parse_args([]))
    return instance, glib, xapp.StatusIcon.return_value, gtk.Menu.return_value


def test_starting_state_is_applied_before_the_first_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    instance, glib, icon, menu = make_tray(monkeypatch)
    icon.set_icon_name.assert_called_once_with("security-high-symbolic")
    icon.set_tooltip_text.assert_called_once_with("shell-runner: starting…")
    icon.set_label.assert_called_once_with("")
    icon.set_primary_menu.assert_called_once_with(menu)
    icon.set_secondary_menu.assert_called_once_with(menu)
    instance.gtk.MenuItem.assert_called_once_with(label="Open Pending Prompts")
    assert menu.append.call_count == 1  # only the Open item
    menu.show_all.assert_called_once()
    assert instance.applied_chrome == tray.STARTING_CHROME
    assert instance.applied_view is None
    glib.idle_add.assert_not_called()  # the worker has not started


def test_worker_crash_schedules_quit_with_code_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    instance, glib, _icon, _menu = make_tray(monkeypatch)

    def crash(*_args: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(tray, "run_loop", crash)
    instance._work()  # must not raise
    glib.idle_add.assert_called_once_with(instance._quit, 1)
    assert "worker crashed" in capsys.readouterr().err
    assert instance._quit(1) is False
    assert instance.code == 1
    instance.loop.quit.assert_called_once()


def test_chrome_follows_the_applied_chrome_while_a_rebuild_is_deferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: chrome A -> B while the rebuild is deferred, then back to A,
    # must leave the icon showing A.
    instance, _glib, icon, menu = make_tray(monkeypatch)
    instance._apply(ok(make_prompt("p1")))  # A: closed menu, full rebuild
    instance._on_show(menu)
    instance._apply(ok(make_prompt("p1"), make_prompt("p2")))  # B: structure change, deferred
    assert instance.rebuild_pending is True
    assert icon.set_label.call_args.args == ("2",)
    instance._apply(ok(make_prompt("p1")))  # back to A's structure and chrome
    assert icon.set_label.call_args.args == ("1",)
    assert icon.set_tooltip_text.call_args.args == ("shell-runner: 1 pending",)
    assert instance.applied_chrome == instance.latest_view.chrome


def test_deferred_rebuild_runs_from_idle_after_hide_with_the_latest_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, glib, _icon, menu = make_tray(monkeypatch)
    instance._apply(ok(make_prompt("p1")))
    first = instance.applied_view
    instance._on_show(menu)
    instance._apply(ok(make_prompt("p1"), make_prompt("p2")))
    assert plan_update(first, instance.latest_view, True, None)["rebuild"] == "deferred"
    instance._apply(ok(make_prompt("p1"), make_prompt("p2"), make_prompt("p3")))
    assert instance.applied_view is first  # nothing rebuilt while open
    shows_before = menu.show_all.call_count
    instance._on_hide(menu)
    glib.idle_add.assert_called_once_with(instance._rebuild)  # no view argument
    assert menu.show_all.call_count == shows_before  # not rebuilt inside the hide handler
    assert instance._rebuild() is False
    assert instance.applied_view is instance.latest_view
    assert row_ids(instance.applied_view) == ["p1", "p2", "p3"]
    assert instance.rebuild_pending is False
    assert menu.show_all.call_count == shows_before + 1


def test_hide_without_a_pending_rebuild_schedules_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    instance, glib, _icon, menu = make_tray(monkeypatch)
    instance._apply(ok(make_prompt("p1")))
    instance._on_show(menu)
    instance._on_hide(menu)
    glib.idle_add.assert_not_called()
```

- [ ] **Step 2: Run, expect failure**

Run: `pixi run -e dev pytest tests/unit/test_tray.py -q`
Expected: FAIL in `test_tray_imports_gi_only_inside_functions` (`AssertionError: main() must import gi`), `AttributeError: ... has no attribute 'main'` in the four `main` tests, and `AttributeError: module 'shell_runner.tray' has no attribute '_Tray'` in the five MagicMock applier tests. `test_tray_module_is_read_only` already passes (it is a guard); the smoke test is SKIPPED.

- [ ] **Step 3: Implement**

1. In `src/shell_runner/tray.py`, replace the import section (from `import re` through the closing `)` of `from shell_runner.notifier import (`) with:

```python
import argparse
import re
import signal
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, NamedTuple, TypedDict

from shell_runner.notifier import (
    Notifier,
    Snapshot,
    _log,
    _utcnow,
    clean,
    format_left,
    http_fetch,
    open_detached,
    parse_args,
    parse_expiry,
    run_loop,
)
```

2. Append to the end of `src/shell_runner/tray.py`:

```python
# --- GTK/XApp layer -------------------------------------------------------------
# A deliberately thin shim over render(), plan_update() and menu_entries().
# CI (no gi) covers its logic with MagicMock stand-ins for GLib/Gtk/XApp: the
# starting state, the worker-crash path, chrome tracking and the deferred
# rebuild. Real GTK behaviour (widgets, signal ordering, main()'s run path) is
# covered only by the gi smoke test and the manual desktop checklist.
# Every GLib.idle_add callback takes positional arguments and returns False.


class _Tray:
    """Owns the single Gtk.Menu, the XApp.StatusIcon, the GLib loop and the worker."""

    def __init__(self, glib: Any, gtk: Any, xapp: Any, args: argparse.Namespace) -> None:
        self.glib = glib
        self.gtk = gtk
        self.args = args
        self.home = str(Path.home())
        self.stop = threading.Event()
        self.code = 0
        # Main-thread-only state.
        self.latest_view: View | None = None
        self.applied_view: View | None = None  # whose STRUCTURE the menu shows
        self.applied_chrome: tuple[str, str, str] | None = None  # what the icon shows
        self.menu_open = False
        self.rebuild_pending = False
        self.row_items: list[Any] = []
        self.loop = glib.MainLoop()
        # Created once: rebuilds replace its children, so the show/hide handlers
        # and the primary/secondary bindings survive.
        self.menu = gtk.Menu()
        self.menu.connect("show", self._on_show)
        self.menu.connect("hide", self._on_hide)
        self.icon = xapp.StatusIcon()
        self.icon.set_primary_menu(self.menu)
        self.icon.set_secondary_menu(self.menu)
        # "starting" state until the first poll: idle icon, empty label, Open only.
        self._set_chrome(STARTING_CHROME)
        self.menu.append(self._item("open", OPEN_LABEL))
        self.menu.show_all()

    def _set_chrome(self, chrome: tuple[str, str, str]) -> None:
        icon_name, label, tooltip = chrome
        self.icon.set_icon_name(icon_name)
        self.icon.set_tooltip_text(tooltip)
        self.icon.set_label(label)
        self.applied_chrome = chrome

    def run(self) -> int:
        for signum in (signal.SIGTERM, signal.SIGINT):
            self.glib.unix_signal_add(self.glib.PRIORITY_DEFAULT, signum, self._on_signal)
        worker = threading.Thread(target=self._work, daemon=True)
        worker.start()
        self.loop.run()
        self.stop.set()
        worker.join(timeout=self.args.interval + 6)
        return self.code

    # Worker thread: http_fetch can block for about 5s, so it never runs on GTK.
    def _work(self) -> None:
        try:
            core = Notifier(
                fetch=lambda: http_fetch(self.args.url),
                on_update=self._post,
                interval_s=self.args.interval,
            )
            run_loop(core, self.stop, self.args.interval)
        except Exception as exc:  # any worker crash ends the process with exit 1
            _log(f"worker crashed: {exc!r}")
            self.glib.idle_add(self._quit, 1)

    def _post(self, snapshot: Snapshot) -> None:
        self.glib.idle_add(self._apply, snapshot)  # only the Snapshot crosses threads

    # GTK thread from here on.
    def _apply(self, snapshot: Snapshot) -> bool:
        view = render(snapshot, _utcnow(), self.args.max_items, self.home)
        self.latest_view = view
        plan = plan_update(self.applied_view, view, self.menu_open, self.applied_chrome)
        if plan["set_chrome"]:
            self._set_chrome(view.chrome)  # also while a rebuild is deferred
        if plan["rebuild"] == "deferred":
            self.rebuild_pending = True
            return False
        if plan["rebuild"]:
            self._rebuild()
            return False
        if plan["relabel"]:
            for item, row in zip(self.row_items, view.rows, strict=True):
                item.set_label(row.label)
        self.applied_view = view  # same structure; record the labels now shown
        return False

    def _rebuild(self) -> bool:
        if self.menu_open:
            self.rebuild_pending = True  # the next hide schedules us again
            return False
        self.rebuild_pending = False
        view = self.latest_view
        if view is None:
            return False
        if self.applied_view is not None and view.structure == self.applied_view.structure:
            return False
        for child in self.menu.get_children():
            self.menu.remove(child)
            child.destroy()
        self.row_items = []
        for kind, text in menu_entries(view):
            self.menu.append(self._item(kind, text))
        self.menu.show_all()
        self.applied_view = view
        return False

    def _item(self, kind: str, text: str) -> Any:
        if kind == "separator":
            return self.gtk.SeparatorMenuItem()
        item = self.gtk.MenuItem(label=text)
        if kind in ("row", "open"):
            item.connect("activate", self._on_open)
        else:
            item.set_sensitive(False)  # header and overflow rows only inform
        if kind == "row":
            self.row_items.append(item)
        return item

    def _on_open(self, _item: Any) -> None:
        open_ui(self.args.ui_url)

    def _on_show(self, _menu: Any) -> None:
        self.menu_open = True

    def _on_hide(self, _menu: Any) -> None:
        self.menu_open = False
        if self.rebuild_pending:
            # Never rebuild inside the hide handler: a clicked item's
            # "activate" fires after "hide" and must still find its item.
            self.glib.idle_add(self._rebuild)

    def _on_signal(self) -> bool:
        self.stop.set()
        self.loop.quit()
        return self.glib.SOURCE_REMOVE

    def _quit(self, code: int) -> bool:
        self.code = code
        self.loop.quit()
        return False


def main(argv: list[str] | None = None) -> int:
    """Exit codes: 2 = gi/typelib missing or bad flags, 3 = no display, 1 = worker crash."""
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("XApp", "1.0")
        from gi.repository import GLib, Gtk, XApp
    except (ImportError, ValueError) as exc:
        _log(
            "python3-gi or the XApp typelib is missing; install them with "
            f"`sudo apt install python3-gi gir1.2-xapp-1.0` ({exc})"
        )
        return 2
    args = parse_args(argv)
    if not Gtk.init_check()[0]:  # PyGObject GTK3: init_check() returns (ok, argv)
        _log("no display available; cannot show the tray icon")
        return 3
    return _Tray(GLib, Gtk, XApp, args).run()


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run, expect pass**

Run: `pixi run -e dev pytest tests/unit/test_tray.py -q`
Expected: all passed except `test_gi_smoke_status_icon_has_the_methods_the_applier_calls`, which is SKIPPED (no gi in the pixi env). 0 failed.

- [ ] **Step 5: Quality gate and commit**

Run the per-task quality gate with files `src/shell_runner/tray.py tests/unit/test_tray.py`. `type-check` must pass without gi (mypy has `ignore_missing_imports = true`). Then:

```bash
git add src/shell_runner/tray.py tests/unit/test_tray.py
git commit -m "feat(tray): XApp status icon applier, worker thread and entry point (#138)"
```

---

### Task 5: systemd user unit for the tray

**Files:**
- Create: `tests/unit/test_tray_service_unit.py`
- Rewrite: `scripts/shell-runner-notify.service.example`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_tray_service_unit.py`:

```python
"""Static checks of the tray's systemd user unit example (issue #138)."""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

UNIT_FILE = Path(__file__).resolve().parents[2] / "scripts" / "shell-runner-notify.service.example"


def _unit() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # keep systemd's CamelCase keys
    parser.read_string(UNIT_FILE.read_text())
    return parser


def test_execstart_runs_the_tray_with_system_python() -> None:
    assert _unit()["Service"]["ExecStart"] == "/usr/bin/python3 -m shell_runner.tray"


@pytest.mark.parametrize("word", ["notify-send", "popup", "pixi"])
def test_no_popup_or_pixi_wording_anywhere(word: str) -> None:
    assert word not in UNIT_FILE.read_text().lower()


def test_description() -> None:
    assert (
        _unit()["Unit"]["Description"]
        == "shell-runner silent tray indicator for pending approval prompts"
    )


def test_start_limits_are_in_the_unit_section() -> None:
    unit = _unit()
    assert unit["Unit"]["StartLimitIntervalSec"] == "300"
    assert unit["Unit"]["StartLimitBurst"] == "10"
    assert "StartLimitIntervalSec" not in unit["Service"]
    assert "StartLimitBurst" not in unit["Service"]


def test_restart_policy_is_in_the_service_section() -> None:
    service = _unit()["Service"]
    assert service["Restart"] == "on-failure"
    assert service["RestartSec"] == "10"
    assert service["RestartPreventExitStatus"] == "2"


def test_bound_to_the_graphical_session() -> None:
    unit = _unit()
    assert unit["Unit"]["PartOf"] == "graphical-session.target"
    assert unit["Unit"]["After"] == "graphical-session.target"
    assert unit["Install"]["WantedBy"] == "graphical-session.target"


def test_paths_are_absolute_with_percent_h() -> None:
    service = _unit()["Service"]
    assert service["WorkingDirectory"] == "%h/ClaudeCode/Servers/shell-runner/development"
    assert (
        service["Environment"] == "PYTHONPATH=%h/ClaudeCode/Servers/shell-runner/development/src"
    )


def test_install_note_and_troubleshooting_are_documented() -> None:
    text = UNIT_FILE.read_text()
    assert "sudo apt install python3-gi gir1.2-xapp-1.0" in text
    assert "xdg-open" in text
    assert "# If the tray icon does not appear" in text
    assert "systemctl --user is-active graphical-session.target" in text
    assert "WantedBy=default.target" in text
    assert "import-environment DISPLAY WAYLAND_DISPLAY DBUS_SESSION_BUS_ADDRESS" in text
    assert "StartLimitIntervalSec=0" in text
```

- [ ] **Step 2: Run, expect failure**

Run: `pixi run -e dev pytest tests/unit/test_tray_service_unit.py -q`
Expected: FAIL, e.g. `test_execstart_runs_the_tray_with_system_python` (current ExecStart runs pixi), `test_no_popup_or_pixi_wording_anywhere[pixi]`, `[notify-send]` and `[popup]`, and `KeyError: 'StartLimitIntervalSec'`.

- [ ] **Step 3: Implement (replace the whole file)**

Replace the entire contents of `scripts/shell-runner-notify.service.example` with:

```ini
# shell-runner silent tray indicator (issue #138): a Cinnamon panel icon with the
# number of pending approval prompts. No sounds and no desktop notifications.
# Read-only: the menu only opens the Pending Prompts page; it never approves.
#
# Install note (Debian 12 / Cinnamon; runs with the system /usr/bin/python3):
#   sudo apt install python3-gi gir1.2-xapp-1.0
#   xdg-open must be installed too (Debian package xdg-utils).
#
# Installation (systemd USER unit):
#   cp scripts/shell-runner-notify.service.example ~/.config/systemd/user/shell-runner-notify.service
#   # Edit paths as needed
#   systemctl --user daemon-reload
#   systemctl --user enable --now shell-runner-notify
#
# Add flags (--interval, --url, --ui-url, --max-items) after the module name in ExecStart.
#
# Exit codes: 2 = python3-gi or the XApp typelib is missing, or a bad flag (never
# restarted: install the packages, then systemctl --user restart shell-runner-notify);
# 3 = no display yet (restarted); 1 = the poll worker crashed (restarted).
#
# After login, check that the session target is active:
#   systemctl --user is-active graphical-session.target
#
# If the tray icon does not appear and that prints "inactive" (some Debian 12
# Cinnamon 5.6 sessions never activate graphical-session.target), use this variant:
#   1. Change the [Install] line below to WantedBy=default.target, then run
#        systemctl --user daemon-reload
#        systemctl --user reenable shell-runner-notify
#   2. Export the desktop session variables to the user manager (e.g. from a
#      Cinnamon startup application):
#        systemctl --user import-environment DISPLAY WAYLAND_DISPLAY DBUS_SESSION_BUS_ADDRESS
#        systemctl --user restart shell-runner-notify
# With the limits below the unit does NOT wait forever for a display: after about
# 100s of failed starts (exit 3) systemd gives up. To retry forever, add a drop-in:
#   systemctl --user edit shell-runner-notify
# and enter these two lines:
#   [Unit]
#   StartLimitIntervalSec=0
#
# Logs: journalctl --user -u shell-runner-notify -f

[Unit]
Description=shell-runner silent tray indicator for pending approval prompts
Documentation=https://github.com/Claire-s-Monster/shell-runner
PartOf=graphical-session.target
After=graphical-session.target
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
WorkingDirectory=%h/ClaudeCode/Servers/shell-runner/development
Environment=PYTHONPATH=%h/ClaudeCode/Servers/shell-runner/development/src
ExecStart=/usr/bin/python3 -m shell_runner.tray
Restart=on-failure
RestartSec=10
RestartPreventExitStatus=2
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=graphical-session.target
```

(configparser treats every line whose stripped text starts with `#` as a comment, so the `[Unit]` in the drop-in example is not parsed as a section.)

- [ ] **Step 4: Run, expect pass**

Run: `pixi run -e dev pytest tests/unit/test_tray_service_unit.py tests/unit/test_service_bind.py -q`
Expected: all passed (`test_service_bind.py` only checks `shell-runner.service*`, so it is unaffected).

- [ ] **Step 5: Quality gate and commit**

Run the per-task quality gate with file `tests/unit/test_tray_service_unit.py`. Then:

```bash
git add scripts/shell-runner-notify.service.example tests/unit/test_tray_service_unit.py
git commit -m "feat(tray): systemd user unit runs the tray with system python (#138)"
```

---

### Task 6: Docs (README, superseded notes)

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-11-19-approval-wait-and-notifier-design.md`
- Modify: `docs/superpowers/plans/2026-11-19-approval-notifier.md`

Docs only: there is no failing test to write. The unit suite must still be green at the end.

- [ ] **Step 1: Replace the README section**

In `README.md`, replace everything from the line `## Desktop approval notifier` down to (not including) the line `## Approval precedence & agent_cap` with:

````markdown
## Desktop approval tray

`shell-runner-tray` shows a silent icon in the Cinnamon panel with the number of pending T3/T4 approval prompts, so you do not have to keep the UI page open. No popups and no sounds. It is read-only (it polls `POST /pending` and never approves; the menu only opens the Pending Prompts page) and is separate from the server.

| State | Icon | Label | Tooltip |
|---|---|---|---|
| Starting (before the first poll; menu holds only Open) | `security-high-symbolic` | empty | `shell-runner: starting…` |
| 0 pending | `security-high-symbolic` | empty | `shell-runner: no pending prompts` |
| N pending | `dialog-warning-symbolic` | `N` (`99+` above 99) | `shell-runner: N pending`, plus ` (k × T4)` when k > 0 |
| Unreachable | `network-offline-symbolic` | `?` | `shell-runner unreachable — retrying (back-off up to 60s)` |

Left or right click opens the menu: up to `--max-items` rows such as `T4 · agent-1 · curl -sI https://example.com · 4m left` (the command is cut at 50 characters and your home directory is shown as `~`), a `… and N more` row when there are more, then `Open Pending Prompts`. Clicking a row or Open runs `xdg-open` on the Pending Prompts page; there are no approve or deny actions in the panel. The time left refreshes on every poll. While the server is unreachable the menu keeps the last known rows that are still live under a `Server unreachable — showing last known` header (just `Server unreachable` when none is still live).

It runs with the system Python 3 (not the project environment) and needs two Debian packages:

```bash
sudo apt install python3-gi gir1.2-xapp-1.0
PYTHONPATH=src /usr/bin/python3 -m shell_runner.tray      # defaults: poll every 3s, 10 menu rows
PYTHONPATH=src /usr/bin/python3 -m shell_runner.tray --interval 5 --max-items 5 \
  --url http://127.0.0.1:4111/pending --ui-url http://127.0.0.1:8511/Pending_Prompts
```

`--interval` must be between 0.1 and 3600 seconds, `--max-items` at least 1, and `--url` / `--ui-url` must be http or https URLs with a host.

Exit codes: `2` = `python3-gi` or the XApp typelib is missing, or a bad flag (systemd does not restart it); `3` = no display (restarted); `1` = the poll worker crashed (restarted); `0` = stopped by SIGTERM/SIGINT.

To run it as a systemd user service, copy `scripts/shell-runner-notify.service.example` to `~/.config/systemd/user/shell-runner-notify.service`. Installation steps, the `graphical-session.target` check and the fallback for sessions that never activate that target are in the file. The unit name is unchanged, so an existing notifier install upgrades in place.

- `POST /pending` is unauthenticated, so keep the server bound to `127.0.0.1` (guarded by `tests/unit/test_service_bind.py`).

````

- [ ] **Step 2: Add the superseded note to the 2026-11-19 spec**

In `docs/superpowers/specs/2026-11-19-approval-wait-and-notifier-design.md`, directly under the line `## Part N: shell-runner-notify (desktop notifier)` insert a blank line and then:

```markdown
> **Superseded (2026-11-25):** the popup notifier in this Part is replaced by the silent panel tray in `2026-11-25-approval-tray-design.md` (issue #138). This Part is kept for history only.
```

- [ ] **Step 3: Add the superseded note to the 2026-11-19 plan**

In `docs/superpowers/plans/2026-11-19-approval-notifier.md`, directly under the first line (`# shell-runner-notify Implementation Plan`) insert a blank line and then this one line:

```markdown
> **Superseded (2026-11-25):** replaced by `docs/superpowers/plans/2026-11-25-approval-tray.md` (silent tray, issue #138); kept for history.
```

- [ ] **Step 4: Verify**

- Search `README.md` for `notify-send` and `Desktop approval notifier`: expected no matches (Grep tool, or `grep -n "notify-send\|Desktop approval notifier" README.md` → no output).
- Run: `pixi run -e dev pytest tests/unit -q` → passed, 0 failed.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/superpowers/specs/2026-11-19-approval-wait-and-notifier-design.md docs/superpowers/plans/2026-11-19-approval-notifier.md
git commit -m "docs(tray): README tray section and superseded notes (#138)"
```

---

### Task 7: Final verification and manual desktop checklist

**Files:** none (only fix-ups if a check fails; commit any fix with explicit paths and a `fix(tray): ... (#138)` message).

- [ ] **Step 1: Full suite**

Run: `pixi run -e dev test`
Expected: passed, 0 failed (skips allowed, incl. the gi smoke test).

- [ ] **Step 2: Lint, format and types**

Run: `pixi run -e dev lint`, then `pixi run -e dev format-check`, then `pixi run -e dev type-check`.
Expected: `All checks passed!`, `... files already formatted`, `Success: no issues found`.

- [ ] **Step 3: Acceptance checks**

- `src/` has no `notify-send`: Grep tool for `notify-send` in `src/` → no matches (also enforced by `test_no_src_module_references_notify_send`).
- `pyproject.toml` and `pixi.lock` unchanged: `git diff --stat development -- pyproject.toml pixi.lock` → empty (agents: the git MCP diff tool against `development`, restricted to those two paths; find it with `mcp__git__discover_tools("diff")`).
- `git status` clean; `git log --oneline development..HEAD` shows the spec commits plus the Task 1-6 commits. Do not push.

- [ ] **Step 4: Manual desktop checklist (for the user, on the Cinnamon desktop)**

These steps need a real desktop and cannot run in CI. Report results back on issue #138.

1. Install the dependencies: `sudo apt install python3-gi gir1.2-xapp-1.0 xdg-utils`.
2. gi smoke check with the system Python:
   `/usr/bin/python3 -c "import gi; gi.require_version('Gtk','3.0'); gi.require_version('XApp','1.0'); from gi.repository import Gtk, XApp; print(Gtk.init_check()[0], type(XApp.StatusIcon()).__name__)"`
   Expected: `True StatusIcon`.
3. Missing-dependency exit code (optional, any machine): `PYTHONPATH=src /usr/bin/python3 -I -c "import sys; sys.modules['gi'] = None; from shell_runner.tray import main; print(main([]))"` from the worktree → one log line naming `python3-gi gir1.2-xapp-1.0`, then `2`.
4. Foreground trial before merge (the unit points at `development/`, which only has the tray after merge): from the worktree run `PYTHONPATH=src /usr/bin/python3 -m shell_runner.tray`. Stop it with Ctrl+C; `echo $?` → `0`.
5. After merge, install the unit: copy `scripts/shell-runner-notify.service.example` to `~/.config/systemd/user/shell-runner-notify.service`, then `systemctl --user daemon-reload` and `systemctl --user enable --now shell-runner-notify`.
6. `systemctl --user is-active graphical-session.target` → `active`. If it prints `inactive`, apply the fallback documented in the unit file (`WantedBy=default.target` + `import-environment`), log out and in, and recheck `systemctl --user is-active shell-runner-notify` → `active`.
7. Verify the icon states:
   - No prompts: shield icon (`security-high-symbolic`), no label, tooltip `shell-runner: no pending prompts`.
   - Trigger a T3 prompt (an agent command that needs approval): warning icon, label `1`, tooltip `shell-runner: 1 pending`. A T4 prompt adds ` (1 × T4)` to the tooltip.
   - Stop the shell-runner server: within the back-off (up to 60s) the icon turns `network-offline-symbolic` with label `?`; the menu shows `Server unreachable — showing last known` above the last rows. Start the server again: the icon recovers on the next successful poll.
   - No popup and no sound at any point.
8. Verify the menu: left click and right click both open the same menu; each row reads `T3 · <agent> · <command> · Nm left` with `~` for your home; the time left changes while the menu stays open (every ~3s); with more than 10 prompts there is a `… and N more` row; there are no approve or deny entries.
9. Click a prompt row, then `Open Pending Prompts`: each opens the Pending Prompts page in the browser (`http://127.0.0.1:8511/Pending_Prompts`).
10. Deferred-rebuild ordering (the one hide→activate→idle-rebuild sequence CI cannot check): open the menu and keep it open, let the prompt set change (create a new prompt, or deny one in the UI) and wait for one poll (~3s), then click a prompt row. Confirm the Pending Prompts page opens, and that on reopening the menu it shows the new prompt set.
11. Starting state: right after `systemctl --user restart shell-runner-notify` (or with the server stopped, before the first poll returns) the icon is the shield with tooltip `shell-runner: starting…`, no label, and the menu holds only `Open Pending Prompts`.
12. Stop the unit: `systemctl --user stop shell-runner-notify`, then `systemctl --user show shell-runner-notify -p ExecMainStatus` → `ExecMainStatus=0`. Logs: `journalctl --user -u shell-runner-notify -n 50` show no tracebacks.

---

## Spec coverage map

| Spec requirement | Task |
|---|---|
| `clean()` plain text, single `…`, still strips control/format chars | T1 |
| `format_left` (`<1m` / `Nm` / `Nh Mm left`) | T1 (defined in `notifier.py` per the spec's notifier "Change" list; boundary tests in `test_notifier.py`) |
| `Snapshot` frozen (`prompts` tuple, `status` ok/unreachable, no `retry_in_s`) | T2 |
| `on_update` after every fetching tick, not on back-off-skipped ticks | T2 |
| `_handle` returns live list sorted by parsed `expires_at`; `last_prompts` | T2 |
| Unreachable snapshot carries `last_prompts`; `ValueError` = unreachable; logged | T2 |
| Remove `seen`, `home`, `reminded`, urgency, `_popup`, `build_body`, `shorten_home`, `format_remaining`, `CMD_LIMIT`, `DesktopNotifier`, `run_argv`, `Runner`, `first` flag, `main` / `__main__` | T1 (tests), T2 (code) |
| Keep `http_fetch`, `_read_bounded`, depth check, back-off, filtering, `_log`, `parse_expiry`, `_http_url`, `_positive_interval`, `open_detached`, `spawn_daemon` | T2 |
| `run_loop(notifier, stop, interval)`; main-loop test moved onto it | T2 |
| `parse_args --max-items` (default 10, ≥1), tray prog/description | T2 |
| Spec test rewrite/delete list for `test_notifier.py` | T1, T2 |
| `render` states, fallbacks, re-filtering, `max_items` cap, `99+`, `(k × T4)`, stale header | T3 |
| Home substitution helper | T3 |
| `View` / `Row` types, structural identity, `plan_update` truth table | T3 |
| Opener (`xdg-open` argv via `open_detached`, failures logged) | T3 |
| `MENU_CMD_LIMIT = 50` | T3 |
| Single `Gtk.Menu`, rebuild = destroy children + `show_all`, relabel, deferral via hide + `idle_add`, `latest_view` / `applied_view` / `menu_open` | T4 |
| `applied_chrome` tracked independently of `applied_view`; starting state (shield, `shell-runner: starting…`, Open only) | T3 (`plan_update`, `STARTING_CHROME`), T4 (applier + MagicMock tests) |
| Worker thread, `idle_add` pattern, `_utcnow` in the idle callback, signals, exit codes 2/3/1/0, join timeout; worker crash → `idle_add(_quit, 1)` tested with MagicMock | T4 |
| gi imported only inside functions; read-only; `main([])` → 2 without gi; smoke test | T4 |
| systemd unit rewrite and unit-file test | T5 |
| README, superseded notes | T6 |
| Acceptance criteria, `pyproject.toml` / `pixi.lock` unchanged, manual desktop check | T7 |
