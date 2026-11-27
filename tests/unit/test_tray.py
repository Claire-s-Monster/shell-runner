"""Unit tests for the approval tray (issue #138).

The pure layer (render, plan_update, menu_entries, substitute_home, open_ui)
needs no gi; nothing here touches the desktop, the network or real time.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
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


@pytest.mark.parametrize("bad", ["garbage", "", 12345, None, MISSING])
def test_row_with_unparseable_expiry_is_dropped(bad: Any) -> None:
    view = view_of(ok(make_prompt("bad", expires_at=bad), make_prompt("p2")))
    assert row_ids(view) == ["p2"]


def test_render_does_not_mutate_the_snapshot() -> None:
    snapshot = ok(make_prompt("p1", raw_cmd="cat /home/me/x"), make_prompt("p2", tier="x"))
    before = tuple(dict(p) for p in snapshot.prompts)
    view_of(snapshot)
    assert tuple(dict(p) for p in snapshot.prompts) == before


def test_render_handles_read_only_mapping_rows_as_the_notifier_produces() -> None:
    rows = tuple(
        MappingProxyType(dict(p))
        for p in (make_prompt("p1", tier=4, raw_cmd="cat /home/me/x"), make_prompt("p2"))
    )
    view = view_of(Snapshot(prompts=rows, status="ok"))
    assert row_ids(view) == ["p1", "p2"]
    assert view.rows[0].identity[3] == "cat ~/x"
    assert view.tooltip == "shell-runner: 2 pending (1 × T4)"


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
    assert view.rows[0].text_without_left == "T? · agent-1 · ls ~/proj"
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
    assert view.chrome == ("dialog-warning-symbolic", "1", "shell-runner: 1 pending")
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


@pytest.mark.parametrize(
    "text", ["ls /mnt/home/me/x", "ls /srv/home/me/x", "ls foo/home/me", "/mnt/home/me"]
)
def test_substitute_home_needs_a_left_boundary(text: str) -> None:
    assert substitute_home(text, HOME) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("cd /home/me/x", "cd ~/x"),
        ("/home/me/x", "~/x"),
        ("--path=/home/me/x", "--path=~/x"),
        ("PATH=/a:/home/me/bin", "PATH=/a:~/bin"),
        ('"/home/me"', '"~"'),
        ("'/home/me/x'", "'~/x'"),
        ("ls\t/home/me", "ls\t~"),
    ],
)
def test_substitute_home_at_a_boundary(text: str, expected: str) -> None:
    assert substitute_home(text, HOME) == expected


def test_max_items_zero_sends_every_row_to_overflow() -> None:
    view = view_of(ok(make_prompt("a"), make_prompt("b")), max_items=0)
    assert view.rows == ()
    assert view.overflow == 2
    assert view.label == "2"


def test_unreachable_with_no_rows_shown_uses_the_empty_header() -> None:
    # Live prompts exist but max_items=0 shows none, so "showing last known" would be false.
    view = view_of(down(make_prompt("a"), make_prompt("b")), max_items=0)
    assert view.rows == ()
    assert view.overflow == 2
    assert view.header == "Server unreachable"
    assert menu_entries(view)[:2] == [
        ("header", "Server unreachable"),
        ("overflow", "… and 2 more"),
    ]


def test_negative_max_items_is_clamped_to_zero() -> None:
    view = view_of(ok(make_prompt("a"), make_prompt("b")), max_items=-1)
    assert view.rows == ()
    assert view.overflow == 2


def test_max_items_far_above_the_row_count_shows_everything() -> None:
    view = view_of(ok(make_prompt("a"), make_prompt("b")), max_items=10**9)
    assert row_ids(view) == ["a", "b"]
    assert view.overflow == 0


def test_substitute_home_strips_a_trailing_slash_on_home() -> None:
    assert substitute_home("cat /home/me/x", "/home/me/") == "cat ~/x"


@pytest.mark.parametrize("home", ["", "/"])
def test_substitute_home_with_empty_home_is_a_no_op(home: str) -> None:
    assert substitute_home("cat /home/me/x", home) == "cat /home/me/x"


@pytest.mark.parametrize(
    ("home", "text", "expected"),
    [
        ("/home/alice", "cat /home/alice/x", "cat ~/x"),
        ("/root", "ls /root /tmp", "ls ~ /tmp"),
        ("/Users/b.c", "cd /Users/b.c/src", "cd ~/src"),
        ("/home/alice", "cat /home/me/x", "cat /home/me/x"),
    ],
)
def test_substitute_home_uses_the_given_home_not_a_fixed_one(
    home: str, text: str, expected: str
) -> None:
    assert substitute_home(text, home) == expected


def test_render_shortens_the_home_it_is_given() -> None:
    view = render(ok(make_prompt("p1", raw_cmd="cat /srv/u/x")), NOW, 10, "/srv/u")
    assert view.rows[0].identity[3] == "cat ~/x"


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


@pytest.mark.parametrize(("menu_open", "rebuild"), [(False, True), (True, "deferred")])
def test_plan_structural_and_chrome_change_together(menu_open: bool, rebuild: bool | str) -> None:
    new = dataclasses.replace(BASE, overflow=1, label="11")
    assert plan_update(BASE, new, menu_open, BASE.chrome) == {
        "set_chrome": True,
        "relabel": False,
        "rebuild": rebuild,
    }


def test_plan_first_update_sets_chrome_even_if_it_equals_the_applied_chrome() -> None:
    assert plan_update(None, BASE, False, BASE.chrome) == {
        "set_chrome": True,
        "relabel": False,
        "rebuild": True,
    }


def test_starting_chrome_literal() -> None:
    assert tray.STARTING_CHROME == ("security-high-symbolic", "", "shell-runner: starting…")


def test_t4_tooltip_counts_every_live_prompt_not_just_shown_rows() -> None:
    prompts = [make_prompt("a"), make_prompt("b", tier=4), make_prompt("c", tier=4)]
    prompts.append(make_prompt("d", tier=4))
    view = view_of(ok(*prompts), max_items=1)
    assert row_ids(view) == ["a"]
    assert view.tooltip == "shell-runner: 4 pending (3 × T4)"


# --- server totals (issue #140) -----------------------------------------------------


def with_totals(snapshot: Snapshot, total: int | None, total_t4: int | None = None) -> Snapshot:
    return dataclasses.replace(snapshot, total=total, total_t4=total_t4)


def test_server_total_beats_the_capped_row_count() -> None:
    rows = [make_prompt(f"p{i}") for i in range(100)]
    view = view_of(with_totals(ok(*rows), total=150), max_items=10)
    assert view.label == "99+"
    assert view.tooltip == "shell-runner: 150 pending"
    assert len(view.rows) == 10
    assert view.overflow == 140
    assert menu_entries(view)[10] == ("overflow", "… and 140 more")


def test_server_total_t4_shows_in_the_tooltip() -> None:
    rows = [make_prompt(f"p{i}", tier=4 if i < 3 else 3) for i in range(100)]
    view = view_of(with_totals(ok(*rows), total=150, total_t4=12))
    assert view.tooltip == "shell-runner: 150 pending (12 × T4)"


def test_smaller_server_totals_never_undercount_the_built_rows() -> None:
    rows = [make_prompt("a", tier=4), make_prompt("b", tier=4), make_prompt("c")]
    view = view_of(with_totals(ok(*rows), total=1, total_t4=1))
    assert view.tooltip == "shell-runner: 3 pending (2 × T4)"
    assert view.overflow == 0


def test_totals_none_keeps_the_row_derived_counts() -> None:
    rows = [make_prompt(f"p{i}", tier=4) for i in range(100)]
    view = view_of(with_totals(ok(*rows), total=None, total_t4=None), max_items=10)
    assert view.label == "99+"
    assert view.tooltip == "shell-runner: 100 pending (100 × T4)"
    assert view.overflow == 90


def test_server_total_is_ignored_when_unreachable() -> None:
    view = view_of(with_totals(down(make_prompt("p1")), total=150))
    assert (view.label, view.tooltip) == ("?", tray.TOOLTIP_OFFLINE)


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
