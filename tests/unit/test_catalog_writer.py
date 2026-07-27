"""Unit tests for CatalogWriter — non-blocking async catalog (upsert_template) queue."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from shell_runner.persistence import CatalogWriter, Persistence

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def persistence(tmp_path: Path) -> Persistence:
    return Persistence(db_path=tmp_path / "test.sqlite3")


@pytest.fixture
async def writer(persistence: Persistence) -> CatalogWriter:  # type: ignore[misc]
    cw = CatalogWriter(persistence)
    await cw.start()
    yield cw
    await cw.stop()


def _call_kwargs(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = dict(
        template="echo <word>",
        agent_id="test-agent",
        current_tier=1,
        was_denied=False,
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_submit_returns_immediately_even_with_slow_writer(
    persistence: Persistence, tmp_path: Path
) -> None:
    """submit() must not block the event loop even when upsert_template is slow."""
    blocked = asyncio.Event()

    def slow_upsert_template(**_kwargs: Any) -> None:
        blocked.wait(timeout=5)  # synchronous wait inside thread (safe — off event loop)

    cw = CatalogWriter(persistence)
    with patch.object(persistence, "upsert_template", side_effect=slow_upsert_template):
        await cw.start()
        try:
            # submit should return essentially instantly even though slow_upsert_template blocks
            submit_done = asyncio.Event()

            async def _do_submit() -> None:
                await cw.submit(**_call_kwargs())
                submit_done.set()

            await asyncio.wait_for(_do_submit(), timeout=1.0)
            assert submit_done.is_set(), "submit() did not return within 1 second"
        finally:
            blocked.set()  # unblock the drain thread so stop() can finish
            await cw.stop()


async def test_queue_full_drops_record_and_increments_counter(
    persistence: Persistence,
) -> None:
    """When the queue is full, submit() drops the record and increments dropped_count."""
    # Create a writer with a minimal queue (1 slot)
    cw = CatalogWriter(persistence, queue_maxsize=1)

    # Patch upsert_template to never finish so the queue fills up
    slow_event = asyncio.Event()

    def slow_upsert_template(**_kwargs: Any) -> None:
        slow_event.wait(timeout=5)

    with patch.object(persistence, "upsert_template", side_effect=slow_upsert_template):
        await cw.start()
        try:
            # Fill the queue (drain task grabs the first item immediately, so
            # we need two puts: one in-flight, one in queue, then third overflows)
            await cw.submit(**_call_kwargs())
            await asyncio.sleep(0.05)  # let drain task pick up first item
            await cw.submit(**_call_kwargs())  # fills the queue slot
            # This one should overflow
            await cw.submit(**_call_kwargs())

            assert cw.dropped_count >= 1, "Expected at least one dropped record"
        finally:
            slow_event.set()
            await cw.stop()


async def test_stop_drains_pending_records(persistence: Persistence) -> None:
    """stop() must wait until all queued records are written before returning."""
    written: list[dict[str, Any]] = []

    original_upsert_template = persistence.upsert_template

    def capturing_upsert_template(**kwargs: Any) -> None:
        written.append(kwargs)
        original_upsert_template(**kwargs)

    cw = CatalogWriter(persistence)
    with patch.object(persistence, "upsert_template", side_effect=capturing_upsert_template):
        await cw.start()
        for i in range(5):
            await cw.submit(**_call_kwargs(template=f"echo <word{i}>"))
        await cw.stop()

    assert len(written) == 5, f"Expected 5 written records, got {len(written)}"


async def test_drainer_survives_upsert_template_exception(persistence: Persistence) -> None:
    """A bad record must not kill the drain task; subsequent records still process."""
    call_count = 0

    def flaky_upsert_template(**_kwargs: Any) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated write error")

    cw = CatalogWriter(persistence)
    with patch.object(persistence, "upsert_template", side_effect=flaky_upsert_template):
        await cw.start()
        # First call will raise, second should still succeed
        await cw.submit(**_call_kwargs())
        await cw.submit(**_call_kwargs())
        await cw.stop()

    assert call_count == 2, f"Expected 2 upsert_template invocations, got {call_count}"


async def test_dropped_count_starts_at_zero(persistence: Persistence) -> None:
    cw = CatalogWriter(persistence)
    assert cw.dropped_count == 0


async def test_submit_after_stop_does_not_raise(persistence: Persistence) -> None:
    """Submitting after stop() should be graceful (queue might still accept or be full)."""
    cw = CatalogWriter(persistence)
    await cw.start()
    await cw.stop()
    # After stop, drain task is cancelled; queue.put_nowait should still work
    # (queue is empty after join), but we just check no exception is raised.
    try:
        await cw.submit(**_call_kwargs())
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"submit() after stop() raised: {exc}")
