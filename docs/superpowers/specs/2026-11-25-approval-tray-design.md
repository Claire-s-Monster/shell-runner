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
- The failure counter and back-off in `Notifier.tick()` / `_on_unreachable`: `delay = min(interval*2**n, MAX_BACKOFF_S)` with `MAX_BACKOFF_S` = 60s.
- The expired/decided/malformed filtering in `_handle` / `_handle_one`, and the seen-set pruning.
- `_log` (escapes non-printable characters), `parse_expiry`, `_http_url`, `_positive_interval`, `shorten_home`.
- `open_detached` (stdlib, `xdg-open` argv list, reaper thread), reused by `tray.py`. It reaps via `spawn_daemon`, so `spawn_daemon` also stays (checked: nothing else uses it).

Change:

- The `notify(title, body, urgency)` callback becomes `on_update(snapshot)`, called after every tick that fetched, successfully or not (not on ticks skipped by back-off).
- `Snapshot` is a frozen dataclass:
  - `prompts`: tuple of live pending prompt dicts, sorted by soonest `expires_at`.
  - `status`: `"ok"` or `"unreachable"`. No `retry_in_s`.
- `Notifier.last_prompts` holds the prompts from the last successful fetch. The unreachable `Snapshot` carries them.
- `_on_unreachable` no longer calls `notify`, and the `first` flag is dropped.
- `clean()` no longer escapes markup (plain text, not Pango). It still drops control/format characters and turns newlines and tabs into spaces. Its optional length limit stays; `TRUNCATED` is replaced by a single `…`.
- New `format_left(seconds)` for the menu (see Display). `format_remaining` is removed: only `build_body` used it.
- `parse_args` gains `--max-items` (default 10, at least 1). Existing flags are unchanged: `--interval` (3.0, range 0.1-3600), `--url`, `--ui-url`. `tray.main()` reuses `notifier.parse_args`.
- New `run_loop(notifier, stop, interval)`: calls `tick()`, then `stop.wait(interval)` (interruptible), and returns when `stop` is set. The existing main-loop tests move onto it.

Remove: `DesktopNotifier`, `run_argv` (checked: `restart.run_argv` is unrelated), `Runner`, the `notify-send` handling and `_supports_action` probe, `REMIND_BEFORE_S` and `reminded`, urgency, `_popup`, `build_body`, `CMD_LIMIT` (and its use in `_on_unreachable`), and the module `main()` / `__main__` block. The tray owns the entry point.

The existing tests `test_notifier_module_is_stdlib_only` and `test_notifier_module_is_read_only` must pass unchanged.

### tray.py: the only gi consumer

New `src/shell_runner/tray.py`:

- It is the ONLY module that imports `gi`, and only inside functions (never at module level). Importing `tray.py` therefore works without `gi`.
- Pure `render(snapshot, now, max_items) -> View`. `render` always re-filters prompts against `now`, dropping those with `expires_at <= now`. `View` is a frozen dataclass:
  - chrome: `icon_name`, `label`, `tooltip`.
  - `rows`: a row's identity is `(id, tier, agent, cmd)`; its time-left text is a separate field.
- Pure `plan_update(old_view, new_view, menu_open) -> {set_chrome: bool, relabel: bool, rebuild: bool | "deferred"}`:
  - `set_chrome`: chrome differs.
  - Identities unchanged: `relabel` is true if any time-left text differs, `rebuild` is false. The applier updates existing `Gtk.MenuItem`s with `set_label`; the menu is not swapped.
  - Identities changed: `rebuild` is `"deferred"` if `menu_open`, else true. A deferred rebuild runs when the menu closes.
  - `menu_open` is tracked with the menu's `show`/`hide` signals.
- The gi-side applier is a thin untested shim (no icon adapter protocol or fake); the smoke test covers it.
- Opener: reuses `notifier.open_detached(["xdg-open", ui_url])`; failures are logged with `_log`.
- `MENU_CMD_LIMIT = 50` (in `tray.py`) cuts the command shown in a row.
- `main()` reuses `notifier.parse_args`. Entry point: `/usr/bin/python3 -m shell_runner.tray`.

### Lifecycle

- `tray.main()` owns a `threading.Event` called `stop`.
- The worker is `threading.Thread(daemon=True, target=<run_loop wrapper>)`, since `http_fetch` can block for about 5s.
- `on_update` callbacks reach the GTK thread only via `GLib.idle_add`; the icon is touched only there.
- `GLib.unix_signal_add` for SIGTERM and SIGINT sets `stop` and quits the GLib loop: exit 0.
- An uncaught worker exception is logged, then `GLib.idle_add(quit, exit_code=1)` runs (recorded code 1).
- After `loop.run()` returns: `stop.set()`, `worker.join(timeout=interval + 6)`, then exit with the recorded code.

### XApp mapping

- Icon: `set_icon_name`; tooltip: `set_tooltip_text`; label: `set_label`.
- The SAME `Gtk.Menu` is passed to `set_primary_menu` and `set_secondary_menu`, so left and right click both open it.
- The tooltip always contains the count, in case the panel host does not render labels.
- The gi smoke test asserts these methods exist on `XApp.StatusIcon`.

## Display

| State | Icon | Label | Tooltip |
|---|---|---|---|
| 0 pending | `security-high-symbolic` | empty | `shell-runner: no pending prompts` |
| N pending | `dialog-warning-symbolic` | `N`, or `99+` when N > 99 | `shell-runner: N pending`, plus ` (k × T4)` only when k > 0 |
| Unreachable | `network-offline-symbolic` | `?` | `shell-runner unreachable — retrying (back-off up to 60s)` |

N counts all tiers; the icon is the same for any N > 0. Icon names can be tuned later.

Menu (opens on left or right click):

- Unreachable only: first an insensitive header row `Server unreachable — showing last known`, then the last known rows (stale rows are re-filtered against now).
- Up to `--max-items` rows, each `T4 · <agent_id> · <raw_cmd cut to MENU_CMD_LIMIT, ~ for home> · 4m left`. Plain text.
- If there are more prompts, a `… and N more` row.
- A separator, then `Open Pending Prompts`.
- `Open Pending Prompts` and every prompt row run `xdg-open <ui-url>`. There are NO approve or deny actions in the panel.

Time left (`format_left`): under 60s `<1m left`; under 1h `Nm left`; otherwise `Nh Mm left`. It refreshes every poll (3s) via in-place relabel. Expired and decided prompts drop out on the next poll.

## Error handling

- Exit codes: 2 = `python3-gi` or the XApp typelib missing (`ImportError` or `gi.require_version` `ValueError`), one line naming the package, never restarted; 3 = no display (`Gtk.init_check()` returns False), restarts normally; 1 = worker crash.
- Server unreachable: the existing back-off; the icon shows the unreachable state and recovers on the next successful poll.
- Malformed, oversized or too-deep `/pending` response: rejected by `http_fetch` and logged.
  - Decision: `tick()` catches `(OSError, ValueError)` in one `except`, so a `ValueError` from `http_fetch` (bad scheme, oversize, too deep, wrong shape, bad JSON) takes the same `_on_unreachable` path as a network error. It counts toward back-off, and the tray shows the unreachable state. It is not a separate "bad data" state.
  - The unreachable `Snapshot` carries `last_prompts`, so the menu keeps the last known rows (under the stale header) while the icon and tooltip show the outage. `tick()` leaves `seen` untouched on failure, as today.
- Worker thread crash: see Lifecycle (exit 1; systemd restarts the unit).
- `xdg-open` failure: logged with `_log`, and the tray continues.

## systemd

`scripts/shell-runner-notify.service.example` becomes:

- `ExecStart=/usr/bin/python3 -m shell_runner.tray`
- `Environment=PYTHONPATH=<development>/src` and `WorkingDirectory=<development>`
- `[Unit]`: `PartOf=graphical-session.target`, `After=graphical-session.target`, `StartLimitIntervalSec=300`, `StartLimitBurst=5`.
- `[Service]`: `Restart=on-failure`, `RestartSec=5`, `RestartPreventExitStatus=2` (missing dependencies are never restarted; no display, exit 3, is).
- `[Install]`: `WantedBy=graphical-session.target`.

Verification after login: `systemctl --user is-active graphical-session.target`.

Fallback if the Debian 12 Cinnamon 5.6 session does not activate that target: use `WantedBy=default.target` and run `systemctl --user import-environment DISPLAY WAYLAND_DISPLAY DBUS_SESSION_BUS_ADDRESS`; the unit relies on exit 3 and restart until the display is up. Document both in the example file.

Install note: `sudo apt install python3-gi gir1.2-xapp-1.0`.

The unit file name stays `shell-runner-notify.service`, so existing installs upgrade in place.

## Tests

All in the pixi suite, no `gi` needed unless stated.

notifier (rework, assert `Snapshot`s instead of popup counts): fetch, depth, back-off, filtering, args (incl. `--max-items`), log-escape tests; main-loop tests move onto `run_loop`. `test_notifier_module_is_stdlib_only` and `test_notifier_module_is_read_only` stay unchanged. Delete the popup-only tests: `DesktopNotifier`, reminders, T4 urgency, the `notify-send` probe, `run_argv`. Keep `open_detached` tests.

tray:

- `render()` for each state, the `--max-items` cap with the `… and N more` row, `99+`, `(k × T4)` only when k > 0, stale header, re-filtering of expired rows, home shortening, `format_left` boundaries.
- `plan_update()` truth table (chrome-only, relabel-only, rebuild, deferred while open), no `gi`.
- Opener test: the command is an argv list, no shell.
- Read-only string check for `tray.py` (no `approve_pending`, `/approve`, `shell=True`).
- AST test: `tray.py` has no module-level `import gi` (only inside functions).
- With `monkeypatch.setitem(sys.modules, "gi", None)`, importing `tray.py` works and `main()` returns 2.
- gi smoke test: `pytest.importorskip("gi")`, construct an `XApp.StatusIcon` without a main loop and assert `set_icon_name`, `set_tooltip_text`, `set_label`, `set_primary_menu`, `set_secondary_menu` exist. Skipped in CI.
- Unit-file test: parse `scripts/shell-runner-notify.service.example` with `configparser` (`strict=False`) and assert `ExecStart=/usr/bin/python3 -m shell_runner.tray` with no `pixi` anywhere; `StartLimit*` in `[Unit]`; `Restart`, `RestartSec` and `RestartPreventExitStatus=2` in `[Service]`; `graphical-session.target` in `PartOf`, `After` and `WantedBy`.

## Docs

- README: the "Desktop approval notifier" section becomes the tray section.
- `scripts/shell-runner-notify.service.example`: as above.
- `2026-11-19-approval-wait-and-notifier-design.md`: a short note under Part N saying it is superseded by this spec (not a rewrite).
- `docs/superpowers/plans/2026-11-19-approval-notifier.md`: a one-line superseded note at the top.

## Acceptance criteria

- [ ] No popups and no sounds; `notify-send` is not referenced in `src/`.
- [ ] `notifier.py` is stdlib only and read-only, with `on_update(Snapshot)` replacing `notify`.
- [ ] `tray.py` imports `gi` only inside functions, and is read-only.
- [ ] States 0 / N / unreachable render as in the Display table; the menu is capped at `--max-items`.
- [ ] Panel actions only open the UI via an argv-list `xdg-open`.
- [ ] Missing `gi`/typelib exits 2 (no restart); no display exits 3 (restarts); worker crash exits 1.
- [ ] `pyproject.toml` and `pixi.lock` are unchanged.
- [ ] Test suite passes in CI without `gi`.

## Out of scope

- Approve/deny from the panel.
- A native Cinnamon JS applet.
- Sounds.
- Configuration UI.
