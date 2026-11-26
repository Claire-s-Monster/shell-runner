"""Silent Cinnamon tray indicator for pending shell-runner approvals (issue #138).

The pure layer (render, plan_update, menu_entries, substitute_home, open_ui)
has no gi dependency and is fully unit-tested. This module holds the only
gi code (the _Tray applier and main), imported only inside main(), so
importing the module never needs gi.
Read-only: the only action is opening the UI page with an argv-list xdg-open.
"""

from __future__ import annotations

import argparse
import re
import signal
import threading
from collections.abc import Mapping
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
    """Replace `home` with `~` where it is a whole path prefix.

    It must start the text or follow whitespace or one of `=:"'`, and be
    followed by `/`, whitespace, a quote or the end of the text.
    """
    home = home.rstrip("/")
    if not home:
        return text
    pattern = r"(?<![^\s=:\"'])" + re.escape(home) + r"(?=[/\s\"']|\Z)"
    return re.sub(pattern, "~", text)


def _expiry(prompt: Mapping[str, Any]) -> datetime | None:
    value = prompt.get("expires_at")
    return parse_expiry(value) if isinstance(value, str) else None


def _row(prompt: Mapping[str, Any], left_s: float, home: str) -> tuple[Row, bool]:
    """Build one menu row; also return whether the prompt counts as T4."""
    tier = prompt.get("command_tier")
    tier_ok = isinstance(tier, int) and not isinstance(tier, bool)
    tier_text = f"T{tier}" if tier_ok else "T?"
    agent = prompt.get("agent_id")
    agent_text = clean(agent, MENU_CMD_LIMIT) if isinstance(agent, str) else "?"
    cmd = prompt.get("raw_cmd")
    cmd_text = (
        clean(substitute_home(clean(cmd), home), MENU_CMD_LIMIT) if isinstance(cmd, str) else ""
    )
    identity = (str(prompt.get("id")), tier_text, agent_text, cmd_text)
    text = SEP.join((tier_text, agent_text, cmd_text))
    return Row(identity, text, format_left(left_s)), tier_ok and tier == 4


def _live_rows(
    prompts: tuple[Mapping[str, Any], ...], now: datetime, home: str
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

    `now` must be a timezone-aware UTC datetime (naive values are not
    normalised; comparing one raises TypeError). Rows are re-filtered against
    it, so stale (unreachable) rows and rows that expired since the poll drop
    out. `max_items` is clamped to >= 0.
    """
    built = _live_rows(snapshot.prompts, now, home)
    shown = tuple(row for row, _is_t4 in built[: max(0, max_items)])
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
        return bool(self.glib.SOURCE_REMOVE)

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
