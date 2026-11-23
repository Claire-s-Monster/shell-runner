"""A stalled classify() must never block the event loop (issue #131).

Uses httpx.AsyncClient + ASGITransport so requests share the app's event loop;
a loop-blocking classify would freeze every concurrent request in these tests.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import shell_runner.classifier as classifier_module
from shell_runner.persistence import Persistence
from shell_runner.server import app

STALL = "stall-marker"
OBSERVE_BODY = {"command": "ls -la", "cwd": "/tmp", "agent_id": "agent-x", "exit_code": 0}


@pytest.fixture(autouse=True)
def _fresh_db_per_test(fresh_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shell_runner.server.db", Persistence(db_path=fresh_db))


@pytest.fixture
def stall(monkeypatch: pytest.MonkeyPatch) -> Iterator[threading.Event]:
    """Make classify() block on commands containing STALL; released on teardown."""
    release = threading.Event()
    entered = threading.Event()
    real = classifier_module.classify

    def fake(command: str, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        if STALL in command:
            entered.set()
            release.wait(30)
        return real(command, *args, **kwargs)

    monkeypatch.setattr(classifier_module, "classify", fake)
    monkeypatch.setenv("SHELL_RUNNER_CLASSIFY_TIMEOUT_S", "0.2")
    release.entered = entered  # type: ignore[attr-defined]
    yield release
    release.set()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_stalled_observe_times_out_with_503(stall: threading.Event) -> None:
    async with _client() as client:
        t0 = time.monotonic()
        resp = await client.post("/observe", json={**OBSERVE_BODY, "command": f"echo {STALL}"})
    assert resp.status_code == 503
    assert resp.json() == {"error": "classify_timeout"}
    assert time.monotonic() - t0 < 1.5


async def test_health_responds_while_observe_is_stalled(
    stall: threading.Event, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHELL_RUNNER_CLASSIFY_TIMEOUT_S", "10")
    async with _client() as client:
        stalled = asyncio.create_task(
            client.post("/observe", json={**OBSERVE_BODY, "command": f"echo {STALL}"})
        )
        assert await asyncio.to_thread(stall.entered.wait, 2)  # type: ignore[attr-defined]
        t0 = time.monotonic()
        health = await client.get("/health")
        assert health.status_code == 200
        assert time.monotonic() - t0 < 1.0
        stall.set()
        assert (await stalled).status_code == 200


async def test_observe_inflight_cap_returns_503_overloaded(
    stall: threading.Event, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHELL_RUNNER_CLASSIFY_TIMEOUT_S", "10")
    monkeypatch.setenv("SHELL_RUNNER_OBSERVE_MAX_INFLIGHT", "1")
    async with _client() as client:
        stalled = asyncio.create_task(
            client.post("/observe", json={**OBSERVE_BODY, "command": f"echo {STALL}"})
        )
        assert await asyncio.to_thread(stall.entered.wait, 2)  # type: ignore[attr-defined]
        t0 = time.monotonic()
        second = await client.post("/observe", json=OBSERVE_BODY)
        assert second.status_code == 503
        assert second.json() == {"error": "overloaded"}
        assert time.monotonic() - t0 < 1.0
        stall.set()
        await stalled
        # The slot is released afterwards.
        assert (await client.post("/observe", json=OBSERVE_BODY)).status_code == 200


async def test_stalled_execute_fails_closed(stall: threading.Event, tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    async with _client() as client:
        resp = await client.post(
            "/execute",
            json={
                "command": f"touch {marker} {STALL}",
                "cwd": str(tmp_path),
                "agent_id": "agent-x",
            },
        )
    assert resp.status_code == 503
    assert "timed out" in resp.json()["detail"]
    assert not marker.exists()


async def test_stalled_mcp_shell_execute_returns_tool_error(
    stall: threading.Event, tmp_path: Path
) -> None:
    marker = tmp_path / "ran"
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "shell_execute",
            "arguments": {
                "command": f"touch {marker} {STALL}",
                "cwd": str(tmp_path),
                "agent_id": "agent-x",
            },
        },
    }
    async with _client() as client:
        resp = await client.post("/mcp", json=rpc)
    assert resp.status_code == 200
    result = json.loads(resp.json()["result"]["content"][0]["text"])
    assert "timed out" in result["error"]
    assert not marker.exists()


async def test_health_reports_lag_and_inflight() -> None:
    async with _client() as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "event_loop_lag_ms" in data
    assert data["event_loop_lag_ms"] is None or data["event_loop_lag_ms"] >= 0
    assert isinstance(data["classify_inflight"], int)


async def test_observe_fast_path_unchanged() -> None:
    async with _client() as client:
        resp = await client.post("/observe", json=OBSERVE_BODY)
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == {"telemetry_id", "classified_tier", "normalized_template"}
