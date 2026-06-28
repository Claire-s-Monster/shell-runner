"""Unit tests for TelemetryWriter — non-blocking async telemetry queue."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from shell_runner.persistence import Persistence, TelemetryWriter

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def persistence(tmp_path: Path) -> Persistence:
    return Persistence(db_path=tmp_path / "test.sqlite3")


@pytest.fixture
async def writer(persistence: Persistence) -> TelemetryWriter:  # type: ignore[misc]
    tw = TelemetryWriter(persistence)
    await tw.start()
    yield tw
    await tw.stop()


def _call_kwargs(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = dict(
        agent_id="test-agent",
        cwd="/tmp",
        raw_cmd="echo hello",
        normalized_template="echo <word>",
        command_tier=1,
        final_tier=1,
        decision="observed_externally",
        matched_rule_pattern=None,
        matched_rule_category=None,
        exit_code=0,
        stdout_bytes=0,
        stderr_bytes=0,
        duration_ms=5,
        decision_path=["observe_endpoint"],
        normalizer_warnings=[],
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_submit_returns_immediately_even_with_slow_writer(
    persistence: Persistence, tmp_path: Path
) -> None:
    """submit() must not block the event loop even when record_call is slow."""
    blocked = asyncio.Event()

    def slow_record_call(**_kwargs: Any) -> str:
        blocked.wait(timeout=5)  # synchronous wait inside thread (safe — off event loop)
        return "fake-id"

    tw = TelemetryWriter(persistence)
    with patch.object(persistence, "record_call", side_effect=slow_record_call):
        await tw.start()
        try:
            # submit should return essentially instantly even though slow_record_call blocks
            submit_done = asyncio.Event()

            async def _do_submit() -> None:
                await tw.submit(**_call_kwargs())
                submit_done.set()

            await asyncio.wait_for(_do_submit(), timeout=1.0)
            assert submit_done.is_set(), "submit() did not return within 1 second"
        finally:
            blocked.set()  # unblock the drain thread so stop() can finish
            await tw.stop()


async def test_queue_full_drops_record_and_increments_counter(
    persistence: Persistence,
) -> None:
    """When the queue is full, submit() drops the record and increments dropped_count."""
    # Create a writer with a minimal queue (1 slot)
    tw = TelemetryWriter(persistence, queue_maxsize=1)

    # Patch record_call to never finish so the queue fills up
    slow_event = asyncio.Event()

    def slow_record_call(**_kwargs: Any) -> str:
        # Block until we signal — keeps the drain task occupied
        import threading

        ready = threading.Event()
        asyncio.get_event_loop().call_soon_threadsafe(ready.set)
        slow_event.wait(timeout=5)
        return "fake-id"

    with patch.object(persistence, "record_call", side_effect=slow_record_call):
        await tw.start()
        try:
            # Fill the queue (drain task grabs the first item immediately, so
            # we need two puts: one in-flight, one in queue, then third overflows)
            await tw.submit(**_call_kwargs())
            await asyncio.sleep(0.05)  # let drain task pick up first item
            await tw.submit(**_call_kwargs())  # fills the queue slot
            # This one should overflow
            await tw.submit(**_call_kwargs())

            assert tw.dropped_count >= 1, "Expected at least one dropped record"
        finally:
            slow_event.set()
            await tw.stop()


async def test_stop_drains_pending_records(persistence: Persistence) -> None:
    """stop() must wait until all queued records are written before returning."""
    written: list[dict[str, Any]] = []

    original_record_call = persistence.record_call

    def capturing_record_call(**kwargs: Any) -> str:
        written.append(kwargs)
        return original_record_call(**kwargs)

    tw = TelemetryWriter(persistence)
    with patch.object(persistence, "record_call", side_effect=capturing_record_call):
        await tw.start()
        for i in range(5):
            await tw.submit(**_call_kwargs(duration_ms=i))
        await tw.stop()

    assert len(written) == 5, f"Expected 5 written records, got {len(written)}"


async def test_drainer_survives_record_call_exception(persistence: Persistence) -> None:
    """A bad record must not kill the drain task; subsequent records still process."""
    call_count = 0

    def flaky_record_call(**_kwargs: Any) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated write error")
        return "ok"

    tw = TelemetryWriter(persistence)
    with patch.object(persistence, "record_call", side_effect=flaky_record_call):
        await tw.start()
        # First call will raise, second should still succeed
        await tw.submit(**_call_kwargs())
        await tw.submit(**_call_kwargs())
        await tw.stop()

    assert call_count == 2, f"Expected 2 record_call invocations, got {call_count}"


async def test_dropped_count_starts_at_zero(persistence: Persistence) -> None:
    tw = TelemetryWriter(persistence)
    assert tw.dropped_count == 0


async def test_submit_after_stop_does_not_raise(persistence: Persistence) -> None:
    """Submitting after stop() should be graceful (queue might still accept or be full)."""
    tw = TelemetryWriter(persistence)
    await tw.start()
    await tw.stop()
    # After stop, drain task is cancelled; queue.put_nowait should still work
    # (queue is empty after join), but we just check no exception is raised.
    try:
        await tw.submit(**_call_kwargs())
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"submit() after stop() raised: {exc}")
