"""Silent Cinnamon tray indicator for pending shell-runner approvals (issue #138).

The pure layer (render, plan_update, menu_entries, substitute_home, open_ui)
has no gi dependency and is fully unit-tested. This is the only module that
uses gi, and only inside functions, so importing it never needs gi.
Read-only: the only action is opening the UI page with an argv-list xdg-open.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
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
