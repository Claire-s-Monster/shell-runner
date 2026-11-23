"""A stalled classify() must never block the event loop (issue #131).

Uses httpx.AsyncClient + ASGITransport so requests share the app's event loop;
a loop-blocking classify would freeze every concurrent request in these tests.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

import shell_runner.classifier as classifier_module
import shell_runner.server as server_module
from shell_runner import executor, offload, path_guard
from shell_runner.persistence import Persistence
from shell_runner.server import app

STALL = "stall-marker"
OBSERVE_BODY = {"command": "ls -la", "cwd": "/tmp", "agent_id": "agent-x", "exit_code": 0}
AGENT = "focused-code-modifier"  # AUTO_CAPPED cap: `echo x > <outside>` prompts


def _reset_offload_state() -> None:
    offload._running = 0
    offload._queued = 0
    offload._observe_inflight = 0
    offload._health_busy = False
    offload._lag_samples.clear()


@pytest.fixture(autouse=True)
def _reset_offload() -> Iterator[None]:
    """Each test gets a fresh pool and counters; stuck worker threads are not leaked on."""
    offload.shutdown_executor()
    _reset_offload_state()
    yield
    deadline = time.monotonic() + 2
    while offload.classify_inflight() and time.monotonic() < deadline:
        time.sleep(0.02)
    offload.shutdown_executor()
    _reset_offload_state()


@pytest.fixture(autouse=True)
def db(fresh_db: Path, monkeypatch: pytest.MonkeyPatch) -> Persistence:
    fresh = Persistence(db_path=fresh_db)
    monkeypatch.setattr("shell_runner.server.db", fresh)
    return fresh


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """classify() blocks while ``gate.on`` is true; released on teardown."""
    g = SimpleNamespace(on=False, release=threading.Event())
    real = classifier_module.classify

    def fake(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        if g.on:
            g.release.wait(30)
        return real(*args, **kwargs)

    monkeypatch.setattr(classifier_module, "classify", fake)
    monkeypatch.setenv("SHELL_RUNNER_CLASSIFY_TIMEOUT_S", "0.2")
    yield g
    g.release.set()


def _blocker(release: threading.Event):  # noqa: ANN202
    def blocked(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        release.wait(30)

    return blocked


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
    stall: threading.Event, tmp_path: Path, caplog: pytest.LogCaptureFixture
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
    assert "classification timed out" in result["error"]
    assert not marker.exists()
    assert "shell_execute failed" not in caplog.text


async def test_health_reports_lag_and_inflight() -> None:
    async with _client() as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "event_loop_lag_ms" in data
    assert data["event_loop_lag_ms"] is None or data["event_loop_lag_ms"] >= 0
    assert isinstance(data["classify_inflight"], int)
    assert data["classify_queued"] == 0


async def test_observe_fast_path_unchanged() -> None:
    async with _client() as client:
        resp = await client.post("/observe", json=OBSERVE_BODY)
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == {"telemetry_id", "classified_tier", "normalized_template"}


async def test_recheck_timeout_restores_approval_and_retry_succeeds(
    gate: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor, "_CWD_ROOTS", [tmp_path.resolve()])
    outside = Path("/var/tmp") / f"sr131_{uuid.uuid4().hex}"
    body = {"command": f"echo x > {outside}", "cwd": str(tmp_path), "agent_id": AGENT}
    try:
        async with _client() as client:
            first = await client.post("/execute", json=body)
            assert first.json()["decision"] == "prompt_required"
            approved = await client.post(
                "/approve_pending",
                json={
                    "prompt_id": first.json()["prompt"]["id"],
                    "decision": "approve_once",
                    "approver_agent_id": "primary",
                },
            )
            token = approved.json()["approve_token"]

            gate.on = True  # the execution-time recheck stalls
            stalled = await client.post("/execute", json={**body, "approve_token": token})
            assert stalled.status_code == 503
            assert "timed out" in stalled.json()["detail"]
            assert not outside.exists()

            gate.on = False
            retry = await client.post("/execute", json={**body, "approve_token": token})
            assert retry.status_code == 200, retry.text
            assert retry.json()["decision"] == "executed"
            assert outside.read_text() == "x\n"
    finally:
        outside.unlink(missing_ok=True)


async def test_classify_timeout_fails_closed(
    stall: threading.Event, tmp_path: Path, db: Persistence
) -> None:
    db.create_verb_approval(
        verb="echo", cwd_prefix=str(tmp_path), agent_id="agent-x", approved_tier=2
    )
    async with _client() as client:
        resp = await client.post(
            "/classify",
            json={"command": f"echo {STALL}", "cwd": str(tmp_path), "agent_id": "agent-x"},
        )
    assert resp.status_code == 503
    assert "timed out" in resp.json()["detail"]


async def test_template_approval_timeout_fails_closed_on_execute_and_classify(
    gate: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "ran"
    monkeypatch.setattr(server_module, "_apply_template_approval", _blocker(gate.release))
    async with _client() as client:
        execute = await client.post(
            "/execute",
            json={"command": f"touch {marker}", "cwd": str(tmp_path), "agent_id": "agent-x"},
        )
        classify = await client.post(
            "/classify",
            json={"command": f"touch {marker}", "cwd": str(tmp_path), "agent_id": "agent-x"},
        )
    assert execute.status_code == 503
    assert classify.status_code == 503
    assert not marker.exists()


async def test_approve_pending_path_guard_timeout_fails_closed(
    gate: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    db: Persistence,
) -> None:
    monkeypatch.setattr(executor, "_CWD_ROOTS", [tmp_path.resolve()])
    async with _client() as client:
        first = await client.post(
            "/execute",
            json={
                "command": "echo x > /var/tmp/sr131_nope",
                "cwd": str(tmp_path),
                "agent_id": AGENT,
            },
        )
        prompt_id = first.json()["prompt"]["id"]
        monkeypatch.setattr(path_guard, "evaluate", _blocker(gate.release))
        resp = await client.post(
            "/approve_pending",
            json={
                "prompt_id": prompt_id,
                "decision": "approve_template",
                "approver_agent_id": "primary",
            },
        )
    assert resp.status_code == 503
    pending = db.get_pending_prompt(prompt_id)
    assert pending is not None
    assert pending["approved_at"] is None
    assert pending["approve_token"] is None
    assert db.get_template_approved_tier("echo x > <file_arg>", AGENT) is None


async def test_saturated_pool_reports_degraded_health_fast(
    stall: threading.Event, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHELL_RUNNER_CLASSIFY_WORKERS", "1")
    monkeypatch.setenv("SHELL_RUNNER_CLASSIFY_TIMEOUT_S", "10")
    async with _client() as client:
        first = asyncio.create_task(
            client.post("/observe", json={**OBSERVE_BODY, "command": f"echo {STALL}"})
        )
        assert await asyncio.to_thread(stall.entered.wait, 2)  # type: ignore[attr-defined]
        second = asyncio.create_task(client.post("/observe", json=OBSERVE_BODY))
        await asyncio.sleep(0.2)
        t0 = time.monotonic()
        health = (await client.get("/health")).json()
        assert time.monotonic() - t0 < 1.0
        assert health["status"] == "degraded"
        assert health["classify_inflight"] == 1
        assert health["classify_queued"] == 1
        stall.set()
        assert (await first).status_code == 200
        assert (await second).status_code == 200
        final = (await client.get("/health")).json()
    assert final["classify_queued"] == 0


async def test_health_stats_stuck_degrades_without_stacking_calls(
    monkeypatch: pytest.MonkeyPatch, db: Persistence
) -> None:
    release = threading.Event()
    calls: list[int] = []

    def stuck() -> dict:
        calls.append(1)
        release.wait(30)
        return {}

    monkeypatch.setattr(db, "health_stats", stuck)
    monkeypatch.setattr(server_module, "HEALTH_STATS_TIMEOUT_S", 0.2)
    try:
        async with _client() as client:
            first = (await client.get("/health")).json()
            t0 = time.monotonic()
            second = (await client.get("/health")).json()
            assert time.monotonic() - t0 < 0.15  # no new submit, no second wait
    finally:
        release.set()
    assert first["status"] == "degraded"
    assert second["status"] == "degraded"
    assert second["total_calls_24h"] == 0
    assert calls == [1]


async def test_lag_monitor_measures_blocked_loop() -> None:
    task = asyncio.create_task(offload.lag_monitor())
    try:
        while not offload._lag_samples:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.4)  # next wake is due 0.5 s after the sample
        time.sleep(0.3)  # noqa: ASYNC251 - deliberately block the loop past that wake
        await asyncio.sleep(0.1)
        lag = offload.event_loop_lag_ms()
    finally:
        task.cancel()
    assert lag is not None
    assert lag >= 150


def test_event_loop_lag_is_none_when_monitor_samples_are_stale() -> None:
    offload._lag_samples.append((time.monotonic() - 5 * offload.LAG_INTERVAL_S, 900.0))
    assert offload.event_loop_lag_ms() is None
