"""Integration tests for background execution mode (run_in_background=True).

Uses httpx.AsyncClient + ASGITransport so that all requests share a single
asyncio event loop with the ASGI app.  This lets the background watcher tasks
(spawned via asyncio.create_task inside execute_background) actually progress
between awaits, which is not possible with the synchronous TestClient.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import shell_runner.server as server_module
from shell_runner.persistence import Persistence
from shell_runner.server import app


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


# ---------------------------------------------------------------------------
# Async helper
# ---------------------------------------------------------------------------


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


def _shell_calls_row(telemetry_id: str) -> dict:
    rows = server_module.db.telemetry_query(limit=100)
    return next(r for r in rows if r["id"] == telemetry_id)


async def _wait_for_reconciled_shell_calls_row(
    telemetry_id: str, *, timeout_s: float = 5.0, poll_interval: float = 0.1
) -> dict:
    """Poll shell_calls until telemetry_id's row leaves decision="running".

    shell_kill_route writes the jobs row's terminal status="killed"
    synchronously and independently of the background watcher task, so a
    caller observing status="killed" via shell_status is NOT guaranteed the
    watcher's own shell_calls reconciliation (issue #49) — which only runs
    once the watcher's own proc.wait() resolves — has completed yet. Must
    poll rather than assume synchronity with the jobs-row transition.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        row = _shell_calls_row(telemetry_id)
        if row["decision"] != "running":
            return row
        await asyncio.sleep(poll_interval)
    pytest.fail(
        f"shell_calls row for telemetry_id={telemetry_id} still 'running' after {timeout_s}s"
    )


# ---------------------------------------------------------------------------
# Tests — fast command (echo hello)
# ---------------------------------------------------------------------------


async def test_background_execute_returns_running(job_dir: Path) -> None:
    """POST /execute with run_in_background=True returns decision=running + job_id."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/execute",
            json={
                "command": "echo hello",
                "cwd": "/tmp",
                "agent_id": "focused-ghc-ci-analyzer",
                "run_in_background": True,
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "running"
    assert data["job_id"] is not None
    assert len(data["job_id"]) == 36  # UUID4


async def test_background_execute_completes_with_output(job_dir: Path) -> None:
    """Poll shell_status until completed; verify exit_code and stdout_tail."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/execute",
            json={
                "command": "echo hello",
                "cwd": "/tmp",
                "agent_id": "focused-ghc-ci-analyzer",
                "run_in_background": True,
            },
        )
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        status = await _wait_for_status(
            client, job_id, terminal={"completed", "failed", "timed_out"}
        )

    assert status["status"] == "completed"
    assert status["exit_code"] == 0
    assert "hello" in status["stdout_tail"]
    assert status["stderr_tail"] == ""


async def test_background_execute_reconciles_shell_calls_row(job_dir: Path) -> None:
    """issue #49: an explicit run_in_background=True job's shell_calls row is
    pre-written as decision="running" and must be reconciled once the job
    finishes — decision="executed", the real exit_code, a non-zero stdout
    byte count, and decision_path still containing "background" (preserved,
    not overwritten, by the watcher's reconciliation)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/execute",
            json={
                "command": "echo hello",
                "cwd": "/tmp",
                "agent_id": "focused-ghc-ci-analyzer",
                "run_in_background": True,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        job_id = data["job_id"]
        telemetry_id = data["telemetry_id"]

        await _wait_for_status(client, job_id, terminal={"completed", "failed", "timed_out"})

    row = _shell_calls_row(telemetry_id)
    assert row["decision"] == "executed"
    assert row["exit_code"] == 0
    assert row["output_bytes_stdout"] > 0
    assert "background" in row["decision_path_json"]


async def test_background_execute_uses_shell_semantics(job_dir: Path) -> None:
    """execute_background must run via /bin/bash -c, not raw exec (issue #26 fix).

    `echo one && echo two` requires shell operator handling; under the old
    shlex.split(command) + create_subprocess_exec path this would fail
    (`&&` is not a valid argv token for a raw exec call).
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/execute",
            json={
                "command": "echo one && echo two",
                "cwd": "/tmp",
                "agent_id": "focused-ghc-ci-analyzer",
                "run_in_background": True,
            },
        )
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        status = await _wait_for_status(
            client, job_id, terminal={"completed", "failed", "timed_out"}
        )

    assert status["status"] == "completed"
    assert status["exit_code"] == 0
    stdout_path = Path(status["stdout_full_path"])
    stdout_contents = stdout_path.read_text()
    assert "one" in stdout_contents
    assert "two" in stdout_contents


async def test_output_mode_file_returns_running(job_dir: Path) -> None:
    """output_mode='file' alone (no run_in_background) must route to the
    background path, returning decision=running + job_id instead of running
    inline and blocking until the MCP client's ~26s SIGTERM (issue #26 P1).
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/execute",
            json={
                "command": "echo hi",
                "cwd": "/tmp",
                "agent_id": "focused-ghc-ci-analyzer",
                "output_mode": "file",
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "running"
    assert data["job_id"] is not None


async def test_output_mode_file_completes_with_output_in_file(job_dir: Path) -> None:
    """Poll shell_status until completed; verify the output file contains stdout."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/execute",
            json={
                "command": "echo hi",
                "cwd": "/tmp",
                "agent_id": "focused-ghc-ci-analyzer",
                "output_mode": "file",
            },
        )
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        status = await _wait_for_status(
            client, job_id, terminal={"completed", "failed", "timed_out"}
        )

    assert status["status"] == "completed"
    stdout_path = Path(status["stdout_full_path"])
    assert "hi" in stdout_path.read_text()


async def test_background_job_is_session_leader(job_dir: Path) -> None:
    """execute_background spawns with start_new_session=True (issue #26 P1) so the
    child survives the caller's lifecycle. Uses a short sleep so the process is
    still alive at check time (checked immediately after spawn to avoid a race
    against fast process exit).
    """
    import shell_runner.server as server_module

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/execute",
            json={
                "command": "sleep 2",
                "cwd": "/tmp",
                "agent_id": "focused-ghc-ci-analyzer",
                "run_in_background": True,
            },
        )
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        job = server_module.db.get_job(job_id)
        pid = job["pid"]
        assert os.getpgid(pid) == pid

        await _wait_for_status(client, job_id, terminal={"completed", "failed", "timed_out"})


async def test_shell_status_404_for_unknown_job() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/tools/shell_status",
            json={"job_id": "00000000-0000-0000-0000-000000000000"},
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test — shell_kill
# ---------------------------------------------------------------------------


async def test_shell_kill_terminates_running_job(job_dir: Path) -> None:
    """Spawn sleep 30, kill it via shell_kill, verify status transitions."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/execute",
            json={
                "command": "sleep 30",
                "cwd": "/tmp",
                "agent_id": "focused-ghc-ci-analyzer",
                "run_in_background": True,
                "timeout_s": 60,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["decision"] == "running", f"expected background, got {data}"
        job_id = data["job_id"]

        # Give subprocess a moment to start
        await asyncio.sleep(0.3)

        kill_resp = await client.post(
            "/tools/shell_kill", json={"job_id": job_id, "sig": "SIGTERM"}
        )
        assert kill_resp.status_code == 200
        assert kill_resp.json()["killed"] is True

        # Wait for watcher to write final status
        final = await _wait_for_status(
            client,
            job_id,
            terminal={"killed", "failed", "completed", "timed_out"},
            timeout_s=5.0,
        )

    assert final["status"] in {"killed", "failed"}


async def test_shell_kill_reconciles_shell_calls_row(job_dir: Path) -> None:
    """issue #49: killing a background job must reconcile its shell_calls row
    (decision="executed") without disturbing the jobs row's terminal
    status="killed" written by shell_kill_route (the #48 terminal-status
    guard in _watch()'s _job_is_terminal check still holds)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/execute",
            json={
                "command": "sleep 30",
                "cwd": "/tmp",
                "agent_id": "focused-ghc-ci-analyzer",
                "run_in_background": True,
                "timeout_s": 60,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        job_id = data["job_id"]
        telemetry_id = data["telemetry_id"]

        await asyncio.sleep(0.3)

        kill_resp = await client.post(
            "/tools/shell_kill", json={"job_id": job_id, "sig": "SIGTERM"}
        )
        assert kill_resp.status_code == 200
        assert kill_resp.json()["killed"] is True

        final = await _wait_for_status(
            client,
            job_id,
            terminal={"killed", "failed", "completed", "timed_out"},
            timeout_s=5.0,
        )

    assert final["status"] == "killed"
    row = await _wait_for_reconciled_shell_calls_row(telemetry_id)
    assert row["decision"] == "executed"


async def test_shell_kill_returns_false_for_completed_job(job_dir: Path) -> None:
    """Killing an already-completed job returns killed=False."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/execute",
            json={
                "command": "echo done",
                "cwd": "/tmp",
                "agent_id": "focused-ghc-ci-analyzer",
                "run_in_background": True,
            },
        )
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        await _wait_for_status(client, job_id, terminal={"completed", "failed", "timed_out"})

        kill_resp = await client.post("/tools/shell_kill", json={"job_id": job_id})

    assert kill_resp.status_code == 200
    assert kill_resp.json()["killed"] is False


# ---------------------------------------------------------------------------
# Test — timeout_s honored
# ---------------------------------------------------------------------------


async def test_background_timeout_marks_timed_out(job_dir: Path) -> None:
    """Spawn sleep 10 with timeout_s=2; watcher must mark the job timed_out."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/execute",
            json={
                "command": "sleep 10",
                "cwd": "/tmp",
                "agent_id": "focused-ghc-ci-analyzer",
                "run_in_background": True,
                "timeout_s": 2,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["decision"] == "running", f"expected background, got {data}"
        job_id = data["job_id"]

        final = await _wait_for_status(
            client,
            job_id,
            terminal={"timed_out", "completed", "failed", "killed"},
            timeout_s=8.0,
        )

    assert final["status"] == "timed_out"


async def test_background_timeout_reconciles_shell_calls_row(job_dir: Path) -> None:
    """issue #49: a timed-out background job's shell_calls row must be
    reconciled to decision="executed" (the command DID run, it just outlived
    its timeout) with the timeout exit code — specifically not left stuck at
    decision="running" forever."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/execute",
            json={
                "command": "sleep 10",
                "cwd": "/tmp",
                "agent_id": "focused-ghc-ci-analyzer",
                "run_in_background": True,
                "timeout_s": 1,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        job_id = data["job_id"]
        telemetry_id = data["telemetry_id"]

        final = await _wait_for_status(
            client,
            job_id,
            terminal={"timed_out", "completed", "failed", "killed"},
            timeout_s=8.0,
        )

    assert final["status"] == "timed_out"
    row = _shell_calls_row(telemetry_id)
    assert row["decision"] == "executed"
    assert row["decision"] != "running"
    assert row["exit_code"] == final["exit_code"]


async def test_background_timeout_sends_sigterm_before_sigkill(
    job_dir: Path, tmp_path: Path
) -> None:
    """The background watcher's timeout path must also send a catchable
    SIGTERM first: the trap writes a marker file that only a caught TERM
    (not a SIGKILL) can produce. `sleep 30 & wait` (rather than a foreground
    `sleep 30`) is required because bash does not run trap handlers while
    blocked in a foreground `sleep`.

    The `trap` builtin is an unrecognised verb, so it classifies as T3
    (prompt_required) rather than auto-executing. Same two-step
    prompt -> approve_verb -> retry dance as
    test_approve_verb_promotion_auto_executes_variant_in_subdir in
    test_verb_approval_flow.py: promote the verb to T2 (AUTO_CAPPED) so the
    resubmitted command actually reaches the background execution path.
    """
    marker = tmp_path / "termed"
    command = f"trap 'touch {marker}; exit 42' TERM; sleep 30 & wait"
    agent_id = "focused-ghc-ci-analyzer"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        prompt_resp = await client.post(
            "/execute",
            json={"command": command, "cwd": str(tmp_path), "agent_id": agent_id},
        )
        assert prompt_resp.status_code == 200
        prompt_data = prompt_resp.json()
        assert prompt_data["decision"] == "prompt_required", f"expected a prompt, got {prompt_data}"
        prompt_id = prompt_data["prompt"]["id"]

        approve_resp = await client.post(
            "/approve_pending",
            json={
                "prompt_id": prompt_id,
                "decision": "approve_verb",
                "promote_to_tier": 2,
                "approver_agent_id": "primary",
            },
        )
        assert approve_resp.status_code == 200
        assert approve_resp.json()["verb_promoted"] is True

        resp = await client.post(
            "/execute",
            json={
                "command": command,
                "cwd": str(tmp_path),
                "agent_id": agent_id,
                "run_in_background": True,
                "timeout_s": 1,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["decision"] == "running", f"expected background, got {data}"
        job_id = data["job_id"]

        final = await _wait_for_status(
            client,
            job_id,
            terminal={"timed_out", "completed", "failed", "killed"},
            timeout_s=8.0,
        )

    assert final["status"] == "timed_out"
    assert marker.exists()
