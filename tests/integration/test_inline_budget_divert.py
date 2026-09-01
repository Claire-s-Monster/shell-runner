"""Integration tests for issue #46 "wait-then-divert" inline execution.

output_mode="inline" (the default) used to block for the command's full
duration, exposing it to the MCP client's ~26s cutoff (see the docstring in
test_background_execution.py and issue #26). The fix waits up to an
inline budget (SHELL_RUNNER_INLINE_BUDGET_S) and, only if the command is
still running at that point, diverts it to the background job path instead
of letting it be killed mid-write.

Uses httpx.AsyncClient + ASGITransport (like test_background_execution.py)
so the background watcher task actually progresses between awaits — not
possible with the synchronous TestClient. The budget is always monkeypatched
to 1s; tests never rely on the 20s default for timing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import shell_runner.server as server_module
from shell_runner.persistence import Persistence
from shell_runner.server import app

AGENT_ID = "focused-ghc-ci-analyzer"


@pytest.fixture(autouse=True)
def _fresh_db_per_test(fresh_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the module-level `db` singleton for a fresh instance per test."""
    fresh = Persistence(db_path=fresh_db)
    monkeypatch.setattr("shell_runner.server.db", fresh)


@pytest.fixture
def job_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect background job output files to a controlled temp dir."""
    d = tmp_path / "jobs"
    d.mkdir()
    monkeypatch.setenv("SHELL_RUNNER_JOB_DIR", str(d))
    return d


async def _wait_for_status(
    client: AsyncClient,
    job_id: str,
    *,
    terminal: set[str],
    timeout_s: float = 5.0,
    poll_interval: float = 0.1,
) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        resp = await client.post("/tools/shell_status", json={"job_id": job_id})
        assert resp.status_code == 200
        data = resp.json()
        if data["status"] in terminal:
            return data
        await asyncio.sleep(poll_interval)
    pytest.fail(f"job {job_id} did not reach {terminal} within {timeout_s}s")


def _jobs_row_count() -> int:
    with server_module.db._conn() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"]


def _shell_calls_row(telemetry_id: str) -> dict:
    rows = server_module.db.telemetry_query(limit=100)
    return next(r for r in rows if r["id"] == telemetry_id)


# ---------------------------------------------------------------------------
# 6) No-regression: within budget takes the plain inline path
# ---------------------------------------------------------------------------


async def test_within_budget_takes_plain_inline_path(
    job_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHELL_RUNNER_INLINE_BUDGET_S", "1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/execute",
            json={
                "command": "echo hello",
                "cwd": "/tmp",
                "agent_id": AGENT_ID,
                "timeout_s": 1,
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "executed"
    assert "hello" in data["stdout"]
    assert data["execution_note"] is None
    assert data["job_id"] is None
    assert _jobs_row_count() == 0


# ---------------------------------------------------------------------------
# 7) SHELL_RUNNER_INLINE_BUDGET_S=0 disables diverting entirely
# ---------------------------------------------------------------------------


async def test_budget_zero_disables_diverting(
    job_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHELL_RUNNER_INLINE_BUDGET_S", "0")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/execute",
            json={
                "command": "echo hello",
                "cwd": "/tmp",
                "agent_id": AGENT_ID,
                "timeout_s": 3600,  # far above any budget
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "executed"
    assert "hello" in data["stdout"]
    assert data["job_id"] is None
    assert _jobs_row_count() == 0


# ---------------------------------------------------------------------------
# 8) Over-budget but FAST command: still inline, and the pre-written
#    shell_calls row is reconciled rather than left "running".
# ---------------------------------------------------------------------------


async def test_over_budget_but_fast_reconciles_shell_calls_row(
    job_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHELL_RUNNER_INLINE_BUDGET_S", "1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/execute",
            json={
                "command": "echo fast",
                "cwd": "/tmp",
                "agent_id": AGENT_ID,
                "timeout_s": 30,  # > budget, but the command finishes in well under 1s
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "executed"
    assert "fast" in data["stdout"]
    assert data["job_id"] is None
    assert data["execution_note"] is None

    row = _shell_calls_row(data["telemetry_id"])
    assert row["decision"] == "executed"
    assert row["exit_code"] == 0
    assert row["output_bytes_stdout"] > 0


# ---------------------------------------------------------------------------
# 9) The issue #46 regression test: over-budget SLOW command diverts and
#    still runs to completion instead of being killed at the divert point.
# ---------------------------------------------------------------------------


async def test_over_budget_slow_command_diverts_and_completes(
    job_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHELL_RUNNER_INLINE_BUDGET_S", "1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        start = time.monotonic()
        resp = await client.post(
            "/execute",
            json={
                "command": "sleep 3 && echo DONE_MARKER",
                "cwd": "/tmp",
                "agent_id": AGENT_ID,
                "timeout_s": 30,
            },
        )
        elapsed = time.monotonic() - start
        assert resp.status_code == 200
        data = resp.json()
        assert elapsed < 3.0, (
            f"response must return around the 1s budget, not the command's 3s runtime "
            f"(took {elapsed}s)"
        )
        assert data["decision"] == "running"
        assert data["job_id"] is not None
        assert data["execution_note"] is not None
        assert "budget" in data["execution_note"].lower()
        assert "1s" in data["execution_note"]  # budget value mentioned
        assert data["job_id"] in data["execution_note"]

        status = await _wait_for_status(
            client,
            data["job_id"],
            terminal={"completed", "failed", "timed_out"},
            timeout_s=8.0,
        )

    assert status["status"] == "completed"
    assert status["exit_code"] == 0
    assert "DONE_MARKER" in status["stdout_tail"]


# ---------------------------------------------------------------------------
# 9b) issue #49: a genuinely diverted job's shell_calls row is reconciled
#     from decision="running" to "executed" once it reaches terminal state,
#     with decision_path still containing "inline_budget_divert" (preserved,
#     not overwritten, by the watcher's reconciliation).
# ---------------------------------------------------------------------------


async def test_over_budget_slow_command_reconciles_shell_calls_row(
    job_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHELL_RUNNER_INLINE_BUDGET_S", "1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/execute",
            json={
                "command": "sleep 3 && echo DONE_MARKER",
                "cwd": "/tmp",
                "agent_id": AGENT_ID,
                "timeout_s": 30,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["decision"] == "running"
        job_id = data["job_id"]
        telemetry_id = data["telemetry_id"]

        await _wait_for_status(
            client, job_id, terminal={"completed", "failed", "timed_out"}, timeout_s=8.0
        )

    row = _shell_calls_row(telemetry_id)
    assert row["decision"] == "executed"
    assert row["exit_code"] == 0
    assert "inline_budget_divert" in row["decision_path_json"]


# ---------------------------------------------------------------------------
# 9c) issue #49 race regression: an over-budget but FAST command must be
#     reconciled by the server's own unconditional finalize_call, not by the
#     background watcher's conditional one — proven by decision_path NOT
#     containing "inline_budget_divert" (the watcher's reconciliation is a
#     no-op here because the server's write already flipped decision away
#     from "running" first).
# ---------------------------------------------------------------------------


async def test_over_budget_but_fast_command_wins_race_against_watcher(
    job_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHELL_RUNNER_INLINE_BUDGET_S", "1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/execute",
            json={
                "command": "echo fast",
                "cwd": "/tmp",
                "agent_id": AGENT_ID,
                "timeout_s": 30,  # > budget, but the command finishes in well under 1s
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "executed"
    assert data["job_id"] is None

    row = _shell_calls_row(data["telemetry_id"])
    assert row["decision"] == "executed"
    assert "inline_budget_divert" not in row["decision_path_json"]


# ---------------------------------------------------------------------------
# 10) Same regression through the T3/T4 approval path
#     (_execute_approved_command).
# ---------------------------------------------------------------------------


async def test_over_budget_slow_command_diverts_via_approval_path(
    job_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHELL_RUNNER_INLINE_BUDGET_S", "1")
    agent_id = "focused-code-modifier"
    raw_cmd = "sleep 3 && echo DONE_MARKER"
    cwd = "/tmp"

    prompt_id = server_module.db.create_pending_prompt(
        agent_id=agent_id,
        cwd=cwd,
        raw_cmd=raw_cmd,
        normalized_template=raw_cmd,
        command_tier=4,
        matched_rule_category=None,
        decision_path=["test setup"],
    )
    token = server_module.db.approve_prompt(prompt_id=prompt_id, decision="approve_once")
    assert token is not None

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        start = time.monotonic()
        resp = await client.post(
            "/execute",
            json={
                "command": raw_cmd,
                "cwd": cwd,
                "agent_id": agent_id,
                "timeout_s": 30,
                "approve_token": token,
            },
        )
        elapsed = time.monotonic() - start
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert elapsed < 3.0, f"response must return around the 1s budget (took {elapsed}s)"
        assert data["decision"] == "running"
        assert data["job_id"] is not None
        assert data["execution_note"] is not None
        assert "budget" in data["execution_note"].lower()
        assert data["job_id"] in data["execution_note"]
        # issue #42's sqlite-busy-noop check must be skipped when there is no
        # exit code yet (the command is still running, diverted).
        assert data["approval_note"] is None

        status = await _wait_for_status(
            client,
            data["job_id"],
            terminal={"completed", "failed", "timed_out"},
            timeout_s=8.0,
        )

    assert status["status"] == "completed"
    assert status["exit_code"] == 0
    assert "DONE_MARKER" in status["stdout_tail"]


# ---------------------------------------------------------------------------
# 11) shell_kill persists "killed" and the watcher never clobbers it.
# ---------------------------------------------------------------------------


async def test_shell_kill_status_not_overwritten_by_watcher(job_dir: Path) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/execute",
            json={
                "command": "sleep 30",
                "cwd": "/tmp",
                "agent_id": AGENT_ID,
                "run_in_background": True,
                "timeout_s": 60,
            },
        )
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        # Give the subprocess a moment to actually start.
        await asyncio.sleep(0.3)

        kill_resp = await client.post(
            "/tools/shell_kill", json={"job_id": job_id, "sig": "SIGTERM"}
        )
        assert kill_resp.status_code == 200
        assert kill_resp.json()["killed"] is True

        status = await _wait_for_status(
            client,
            job_id,
            terminal={"killed", "failed", "completed", "timed_out"},
            timeout_s=5.0,
        )
        assert status["status"] == "killed"

        # Poll again after the process has fully died to catch a watcher race
        # that would clobber "killed" with its own "failed" outcome.
        await asyncio.sleep(1.0)
        final_resp = await client.post("/tools/shell_status", json={"job_id": job_id})
        assert final_resp.status_code == 200

    assert final_resp.json()["status"] == "killed"


# ---------------------------------------------------------------------------
# 12) Termination-path logging: an executor timeout and an external
#     shell_kill must be distinguishable in the logs.
# ---------------------------------------------------------------------------


async def test_termination_paths_are_distinguishable_in_logs(
    job_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # (a) a job that hits its own executor timeout
        timeout_resp = await client.post(
            "/execute",
            json={
                "command": "sleep 10",
                "cwd": "/tmp",
                "agent_id": AGENT_ID,
                "run_in_background": True,
                "timeout_s": 1,
            },
        )
        timeout_job_id = timeout_resp.json()["job_id"]
        await _wait_for_status(
            client,
            timeout_job_id,
            terminal={"timed_out", "completed", "failed", "killed"},
            timeout_s=8.0,
        )

        # (b) a job terminated externally via shell_kill
        kill_start_resp = await client.post(
            "/execute",
            json={
                "command": "sleep 30",
                "cwd": "/tmp",
                "agent_id": AGENT_ID,
                "run_in_background": True,
                "timeout_s": 60,
            },
        )
        kill_job_id = kill_start_resp.json()["job_id"]
        await asyncio.sleep(0.3)
        await client.post("/tools/shell_kill", json={"job_id": kill_job_id, "sig": "SIGTERM"})
        await _wait_for_status(
            client,
            kill_job_id,
            terminal={"killed", "failed", "completed", "timed_out"},
            timeout_s=5.0,
        )

    messages = [r.getMessage() for r in caplog.records]
    timeout_messages = [m for m in messages if "executor timeout" in m]
    kill_messages = [m for m in messages if "shell_kill" in m]

    assert any(timeout_job_id in m for m in timeout_messages), (
        "expected a log line identifying the executor-timeout path for the timed-out job"
    )
    assert any(kill_job_id in m for m in kill_messages), (
        "expected a log line identifying the shell_kill path for the killed job"
    )
    assert not any(kill_job_id in m for m in timeout_messages), (
        "the externally-killed job must not be logged via the executor-timeout path"
    )
