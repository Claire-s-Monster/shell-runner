"""Interactive CLI for reviewing T3 command templates for promotion to T2."""

from __future__ import annotations

import datetime
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

_MOCK_CANDIDATES: list[dict[str, Any]] = [
    {
        "template": "git status --short",
        "observed_count": 47,
        "distinct_agents": ["focused-ci-analyzer", "META-worker", "focused-quality-resolver"],
        "days_observed": 12,
        "denial_count": 0,
        "samples": ["git status --short", "git status --short --branch", "git status --short -uno"],
        "suggested_category": "git/read",
    },
    {
        "template": "ls -la {path}",
        "observed_count": 31,
        "distinct_agents": [
            "focused-code-modifier",
            "focused-unit-resolver",
            "META-master",
            "focused-ci-analyzer",
        ],
        "days_observed": 19,
        "denial_count": 0,
        "samples": ["ls -la /tmp", "ls -la /home/user/projects", "ls -la ."],
        "suggested_category": "fs/list",
    },
    {
        "template": "cat {file}",
        "observed_count": 10,
        "distinct_agents": ["focused-code-modifier", "focused-unit-resolver"],
        "days_observed": 7,
        "denial_count": 0,
        "samples": [
            "cat pyproject.toml",
            "cat src/shell_runner/server.py",
            "cat README.md",
        ],
        "suggested_category": "fs/read",
    },
    {
        "template": "grep -r {pattern} {path}",
        "observed_count": 23,
        "distinct_agents": ["focused-ci-analyzer", "focused-quality-resolver"],
        "days_observed": 9,
        "denial_count": 1,
        "samples": [
            "grep -r 'TODO' src/",
            "grep -r 'import' src/shell_runner/",
            "grep -r 'def test' tests/",
        ],
        "suggested_category": "fs/search",
    },
    {
        "template": "find {path} -name {pattern} -type f",
        "observed_count": 14,
        "distinct_agents": ["META-worker", "focused-unit-resolver"],
        "days_observed": 8,
        "denial_count": 0,
        "samples": [
            "find . -name '*.py' -type f",
            "find src/ -name '__init__.py' -type f",
            "find tests/ -name 'test_*.py' -type f",
        ],
        "suggested_category": "fs/search",
    },
    {
        "template": "python -m pytest {args}",
        "observed_count": 62,
        "distinct_agents": [
            "focused-unit-resolver",
            "comprehensive-test-resolver",
            "focused-ci-analyzer",
        ],
        "days_observed": 21,
        "denial_count": 0,
        "samples": [
            "python -m pytest tests/unit -v",
            "python -m pytest tests/ -v --tb=short",
            "python -m pytest tests/unit/test_server.py",
        ],
        "suggested_category": "dev/test",
    },
]

console = Console()


def _is_borderline(candidate: dict) -> bool:
    return (
        candidate["observed_count"] <= 12
        or len(candidate["distinct_agents"]) == 2
        or candidate["days_observed"] <= 9
    )


def _render_panel(candidate: dict[str, Any], position: int, total: int) -> Panel:
    denial_color = "red" if candidate["denial_count"] > 0 else "green"
    agent_list = ", ".join(candidate["distinct_agents"])

    lines = [
        f"[bold cyan]{candidate['template']}[/bold cyan]",
        "",
        (
            f"[dim]Observed:[/dim]  {candidate['observed_count']} times  ·  "
            f"{len(candidate['distinct_agents'])} agents  ·  {candidate['days_observed']} days"
        ),
        f"[dim]Denials:[/dim]   [{denial_color}]{candidate['denial_count']}[/{denial_color}]",
        f"[dim]Agents:[/dim]    {agent_list}",
        f"[dim]Category:[/dim]  {candidate['suggested_category']}",
        "",
        "[dim]Samples (recent first):[/dim]",
    ]
    for sample in candidate["samples"][:3]:
        lines.append(f"  [dim]·[/dim] {sample}")

    title = f"[bold]T3 Candidate  [{position} / {total}][/bold]"
    border_style = "white"
    if _is_borderline(candidate):
        title += "  · [BORDERLINE]"
        border_style = "yellow"

    return Panel(
        "\n".join(lines),
        title=title,
        border_style=border_style,
        padding=(1, 2),
    )


def _prompt_action() -> str:
    while True:
        raw = console.input(
            "\n[bold]\\[a]pprove / \\[d]eny / \\[s]kip / \\[q]uit[/bold]: "
        ).strip().lower()
        if raw in ("a", "d", "s", "q"):
            return raw
        console.print("[yellow]Enter a, d, s, or q.[/yellow]")


def _print_summary(results: list[dict[str, Any]]) -> None:
    approved = [r for r in results if r["action"] == "approve"]
    denied = [r for r in results if r["action"] == "deny"]
    skipped = [r for r in results if r["action"] == "skip"]

    counts = Table(show_header=True, header_style="bold")
    counts.add_column("Action")
    counts.add_column("Count", justify="right")
    counts.add_row("[green]Approved[/green]", str(len(approved)))
    counts.add_row("[red]Denied[/red]", str(len(denied)))
    counts.add_row("[dim]Skipped[/dim]", str(len(skipped)))
    console.print("\n[bold]Session Summary[/bold]")
    console.print(counts)

    recorded = [r for r in results if r["action"] in ("approve", "deny")]
    if not recorded:
        return

    detail = Table(show_header=True, header_style="bold", show_lines=True)
    detail.add_column("Template")
    detail.add_column("Action")
    detail.add_column("Rationale")
    for r in recorded:
        action_label = (
            "[green]approve[/green]" if r["action"] == "approve" else "[red]deny[/red]"
        )
        detail.add_row(r["template"], action_label, r["rationale"] or "[dim]—[/dim]")
    console.print("\n[bold]Decisions[/bold]")
    console.print(detail)


@click.command()
def main() -> None:
    candidates = [c for c in _MOCK_CANDIDATES if c["denial_count"] == 0]
    total = len(candidates)
    console.print(
        f"\n[bold green]{total} T3 candidates eligible for promotion review[/bold green]\n"
    )

    results: list[dict[str, Any]] = []

    for idx, candidate in enumerate(candidates, start=1):
        console.print(_render_panel(candidate, idx, total))
        action = _prompt_action()

        if action == "q":
            break

        rationale = ""
        if action in ("a", "d"):
            rationale = console.input("Rationale (enter to skip): ").strip()
            action_name = "approve" if action == "a" else "deny"
        else:
            action_name = "skip"

        results.append(
            {
                "template": candidate["template"],
                "action": action_name,
                "rationale": rationale,
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            }
        )

    _print_summary(results)
