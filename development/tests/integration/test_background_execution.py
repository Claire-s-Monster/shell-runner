"""Integration tests for background execution mode (run_in_background=True).

Uses httpx.AsyncClient + ASGITransport so that all requests share a single
asyncio event loop with the ASGI app.  This lets the background watcher tasks
(spawned via asyncio.create_task inside execute_background) actually progress
between awaits, which is not possible with the synchronous TestClient.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

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
