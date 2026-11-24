"""Regression guard: Persistence writes must not run on the event loop.

shell-runner is one uvicorn process, so a slow synchronous SQLite write
(fsync, 5s busy timeout, the in-process write RLock) inside an ``async def``
stalls every agent's request. Writes must go through ``asyncio.to_thread``.
"""

from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path

import pytest

from shell_runner import server
from shell_runner.models import ExecuteRequest
from shell_runner.persistence import Persistence

SRC = Path(__file__).resolve().parents[2] / "src" / "shell_runner"

WRITE_METHODS = frozenset(
    {
        "record_call",
        "finalize_call",
        "upsert_template",
        "create_pending_prompt",
        "create_or_reattach_prompt",
        "approve_prompt",
        "consume_approve_token",
        "consume_approval_by_command",
        "create_template_approval",
        "seed_approvals",
        "create_verb_approval",
        "cleanup_expired_prompts",
        "create_job",
        "update_job_status",
        "cleanup_old_jobs",
        "cleanup_old_shell_calls",
    }
)


def _is_offload_call(node: ast.Call) -> bool:
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr in {"to_thread", "run_in_executor"}


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    found: list[str] = []

    def visit(node: ast.AST, in_async: bool) -> None:
        if isinstance(node, ast.AsyncFunctionDef):
            in_async = True
        elif isinstance(node, ast.FunctionDef | ast.Lambda):
            in_async = False
        if (
            in_async
            and isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in WRITE_METHODS
        ):
            found.append(f"{path.name}:{node.lineno} {node.func.attr}")
        for child in ast.iter_child_nodes(node):
            visit(child, in_async)

    visit(tree, False)
    return found


@pytest.mark.parametrize("module", ["server.py", "executor.py"])
def test_no_direct_persistence_writes_in_async_functions(module: str) -> None:
    violations = _violations(SRC / module)
    assert not violations, "sync DB writes on the event loop:\n" + "\n".join(violations)


# The prompt-wait path polls get_pending_prompt every WAIT_POLL_INTERVAL_S for up to
# MAX_WAIT_S, for up to MAX_CONCURRENT_WAITS waiters, so its reads must be offloaded
# too. Deliberately narrow: other sync reads in async handlers are tracked in #133.
WAIT_PATH_FUNCTIONS = frozenset(
    {"_wait_for_prompt_decision", "_bounded_prompt_wait", "_wait_on_prompt"}
)


def _sync_reads_in(path: Path, functions: frozenset[str], method: str) -> list[str]:
    found: list[str] = []
    for fn in ast.walk(ast.parse(path.read_text())):
        if not (isinstance(fn, ast.AsyncFunctionDef) and fn.name in functions):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == method
            ):
                found.append(f"{path.name}:{node.lineno} {fn.name} -> {method}")
    return found


def test_wait_path_functions_still_exist() -> None:
    tree = ast.parse((SRC / "server.py").read_text())
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    assert names >= WAIT_PATH_FUNCTIONS


def test_no_sync_get_pending_prompt_in_wait_path() -> None:
    violations = _sync_reads_in(SRC / "server.py", WAIT_PATH_FUNCTIONS, "get_pending_prompt")
    assert not violations, "sync prompt reads on the event loop:\n" + "\n".join(violations)


@pytest.mark.asyncio
async def test_event_loop_keeps_running_during_slow_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = Persistence(db_path=tmp_path / "t.sqlite3")
    monkeypatch.setattr(server, "db", fresh)

    def slow_record_call(**_kwargs: object) -> None:
        time.sleep(0.3)

    monkeypatch.setattr(fresh, "record_call", slow_record_call)

    ticks = 0
    stop = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    await server._prewrite_inline_budget_telemetry(
        req=ExecuteRequest(command="echo hi", cwd=str(tmp_path), agent_id="a"),
        telemetry_id="tid-1",
        telemetry_writer=None,
        catalog_writer=None,
        normalized_template="echo <arg>",
        command_tier=1,
        final_tier=1,
        catalog_tier=1,
        matched_rule_pattern=None,
        matched_rule_category=None,
        decision_path=[],
        duration_ms=0,
    )
    stop.set()
    await task

    assert ticks >= 15, f"event loop was blocked during the write (ticks={ticks})"
