# Silent Cinnamon tray notifier for pending approvals

Date: 2026-11-25
Base: development a4bb64a (includes the merged #137 notifier).
Issue: #138. Supersedes Part N of `2026-11-19-approval-wait-and-notifier-design.md`.

## Problem

- The `notify-send` popups from #136/#137 are noisy: one popup per prompt, a reminder popup, critical urgency for T4, and sounds from the desktop's notification daemon.
- The human wants to see that approvals are waiting without being interrupted.

## Goal

A silent Cinnamon panel badge showing how many prompts are pending. No popups and no sounds, ever.

## Constraints

- Read-only: never call `/approve` or `approve_pending`, never use `shell=True`. This now applies to `tray.py` as well as `notifier.py`.
- `pyproject.toml` and `pixi.lock` are NOT touched: CI pins pixi 0.49.0 with `--locked`, and any `pyproject.toml` edit forces a v7 re-solve that breaks CI.
- The tray runs outside pixi, with `/usr/bin/python3` (3.11 on Debian 12) and the Debian packages `python3-gi` and `gir1.2-xapp-1.0`.
- The pixi test suite must pass without `gi` installed.

## Architecture

### notifier.py: stdlib-only polling core

`src/shell_runner/notifier.py` stays standard-library only. Keep unchanged:

- `http_fetch` (1 MiB cap, read deadline, `MAX_JSON_DEPTH` 64 pre-check) and `_read_bounded`.
- The back-off in `Notifier.tick()` / `_on_unreachable` (interval doubling, capped at `MAX_BACKOFF_S` = 60s).
- The expired/decided/malformed filtering in `_handle` / `_handle_one`, and the seen-set pruning.
- `_log` (escapes non-printable characters), `parse_expiry`, `_http_url`, `_positive_interval` and `parse_args`.
- `shorten_home` and `format_remaining`, which the tray uses.

Change:

- The `notify(title, body, urgency)` callback becomes `on_update(snapshot)`.
- `Snapshot` is a frozen dataclass:
  - `prompts`: tuple of live pending prompt dicts, sorted by soonest `expires_at`.
  - `status`: `"ok"` or `"unreachable"`.
  - `retry_in_s`: optional float, set when `status="unreachable"`.
- `tick()` calls `on_update` after every poll attempt that actually fetched (not on ticks skipped by back-off).
- `clean()` no longer escapes markup (the tray draws plain text, not Pango markup). It still drops control/format characters and turns newlines and tabs into spaces. Its optional length limit stays; `TRUNCATED` is replaced by a single `…`.
- `parse_args` gains `--max-items` (default 10, at least 1). Existing flags are unchanged: `--interval` (3.0, range 0.1-3600), `--url` (`http://127.0.0.1:4111/pending`), `--ui-url` (`http://127.0.0.1:8511/Pending_Prompts`).

Remove everything popup-specific: `DesktopNotifier`, `run_argv`, `Runner`, `spawn_daemon`, `open_detached`, the `notify-send --wait/--action` handling and `_supports_action` probe, the reminder logic (`REMIND_BEFORE_S`, `reminded`), urgency, `build_body` and the module `main()` / `__main__` block. The tray owns the entry point.

The existing tests `test_notifier_module_is_stdlib_only` and `test_notifier_module_is_read_only` must pass unchanged.

### tray.py: the only gi consumer

New `src/shell_runner/tray.py`:

- It is the ONLY module that imports `gi`, and only inside `main()` and the real-icon adapter. Importing `tray.py` therefore works without `gi`.
- Pure `render(snapshot, now, max_items) -> View`. `View` is a frozen dataclass with `icon_name`, `label`, `tooltip` and `rows` (menu rows).
- `IconAdapter` protocol (set view, set menu rows, set row-activate handler). Implemented by a real `XApp.StatusIcon` adapter and by `FakeIcon` in tests.
- Threading:
  - The worker thread runs `Notifier.tick()` in a loop, since `http_fetch` can block for about 5s.
  - Each snapshot is handed to the GTK thread with `GLib.idle_add`.
  - The icon is touched only on the GTK thread.
  - The icon is redrawn only when the new `View` differs from the last one applied.
- Opener: `xdg-open <ui-url>` as an argv list, no shell, started detached (`start_new_session=True`, stdio to `DEVNULL`, child reaped).
- Entry point: `/usr/bin/python3 -m shell_runner.tray`.

## Display

| State | Icon | Label | Tooltip |
|---|---|---|---|
| 0 pending | `security-high-symbolic` | empty | `shell-runner: no pending prompts` |
| N pending | `dialog-warning-symbolic` | `N` | `shell-runner: N pending (k × T4)` |
| Unreachable | `network-offline-symbolic` | `?` | `shell-runner unreachable, retrying in Xs` |

Icon names can be tuned later.

Menu (opens on left or right click):

- Up to `--max-items` rows, each `T4 · <agent_id> · <raw_cmd cut to ~50 chars, ~ for home> · 4m left`. Plain text, control characters escaped.
- If there are more prompts, a `… and N more` row.
- A separator, then `Open Pending Prompts`.
- `Open Pending Prompts` and every prompt row run `xdg-open <ui-url>`. There are NO approve or deny actions in the panel.

The "left" text refreshes every poll (3s). Expired and decided prompts drop out on the next poll.

## Error handling

- Missing `gi` or the XApp typelib: exit code 2 with one line naming the missing package (`python3-gi` or `gir1.2-xapp-1.0`).
- No display or D-Bus session: the same clean exit 2.
- Server unreachable: the existing back-off; the icon shows the unreachable state and recovers on the next successful poll.
- Malformed, oversized or too-deep `/pending` response: rejected by `http_fetch` and logged.
  - Decision: `tick()` catches `(OSError, ValueError)` in one `except`, so a `ValueError` from `http_fetch` (bad scheme, oversize, too deep, wrong shape, bad JSON) takes the same `_on_unreachable` path as a network error. It counts toward back-off, and the tray shows the unreachable state. It is not a separate "bad data" state.
  - The unreachable `Snapshot` carries the last good `prompts`, so the menu keeps the last known rows while the icon and tooltip show the outage. `tick()` leaves `seen` untouched on failure, as today.
- Worker thread crash: log it, quit the GTK main loop, exit code 1; systemd restarts the unit.
- `xdg-open` failure: logged with `_log`, and the tray continues.

## systemd

`scripts/shell-runner-notify.service.example` becomes:

- `ExecStart=/usr/bin/python3 -m shell_runner.tray`
- `Environment=PYTHONPATH=<development>/src` and `WorkingDirectory=<development>`
- `PartOf=graphical-session.target`, `After=graphical-session.target`, `WantedBy=graphical-session.target`
- `Restart=on-failure` with `StartLimitIntervalSec` / `StartLimitBurst` so it stops looping on repeated failures.
- `RestartPreventExitStatus=2`, so missing dependencies or no display do not restart at all.

Install note: `sudo apt install python3-gi gir1.2-xapp-1.0`. Keep the `systemctl --user import-environment DISPLAY WAYLAND_DISPLAY DBUS_SESSION_BUS_ADDRESS` hint.

The unit file name stays `shell-runner-notify.service`, so existing installs upgrade in place.

## Tests

All in the pixi suite, no `gi` needed unless stated.

notifier (rework, assert `Snapshot`s instead of popup counts): fetch, depth, back-off, filtering, args, main-loop and log-escape tests. Delete the popup-only tests: `DesktopNotifier`, reminders, T4 urgency, the `notify-send` probe, `run_argv`, and `spawn_daemon` / `open_detached` as used by popups.

tray:

- `render()` for each state, the `--max-items` cap with the `… and N more` row, escaping, home shortening and the time-left text.
- Redraw only on change, using `FakeIcon`.
- Opener test: the command is an argv list, no shell.
- Read-only source check for `tray.py`, mirroring the notifier's.
- Importing `tray.py` without `gi` works, and `main()` returns 2 when `gi` is missing (monkeypatch the import).
- gi smoke test: `pytest.importorskip("gi")`, construct an `XApp.StatusIcon` without a main loop. Skipped in CI.
- Unit-file test: the notify unit's `ExecStart` is `/usr/bin/python3 -m shell_runner.tray`, does not use pixi, and binds to `graphical-session.target`.

## Docs

- README: the "Desktop approval notifier" section becomes the tray section.
- `scripts/shell-runner-notify.service.example`: as above.
- `2026-11-19-approval-wait-and-notifier-design.md`: a short note under Part N saying it is superseded by this spec (not a rewrite).
- `docs/superpowers/plans/2026-11-19-approval-notifier.md`: a one-line superseded note at the top.

## Acceptance criteria

- [ ] No popups and no sounds; `notify-send` is not referenced in `src/`.
- [ ] `notifier.py` is stdlib only and read-only, with `on_update(Snapshot)` replacing `notify`.
- [ ] `tray.py` imports `gi` only inside `main()` and the real-icon adapter, and is read-only.
- [ ] States 0 / N / unreachable render as in the Display table; the menu is capped at `--max-items`.
- [ ] Panel actions only open the UI via an argv-list `xdg-open`.
- [ ] Missing `gi`/typelib or no display exits 2; the unit does not restart on 2.
- [ ] `pyproject.toml` and `pixi.lock` are unchanged.
- [ ] Test suite passes in CI without `gi`.

## Out of scope

- Approve/deny from the panel.
- A native Cinnamon JS applet.
- Sounds.
- Configuration UI.
