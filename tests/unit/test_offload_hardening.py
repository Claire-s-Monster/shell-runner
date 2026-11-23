"""Hardening of the offload pools from the #132 review (issue #131)."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from shell_runner import offload


@pytest.fixture(autouse=True)
def _fresh() -> Iterator[None]:
    offload.shutdown_executor()
    offload._running = 0
    offload._queued = 0
    offload._health_busy = False
    yield
    offload.shutdown_executor()
    offload._running = 0
    offload._queued = 0
    offload._health_busy = False


@pytest.mark.parametrize("bad", ["-1", "0", "nan", "inf", "-inf", "abc"])
async def test_non_positive_or_non_finite_timeout_falls_back_to_default(
    bad: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHELL_RUNNER_CLASSIFY_TIMEOUT_S", bad)
    assert await offload.run_bounded(lambda: 42) == 42


async def test_health_busy_cleared_when_future_cancelled_before_start() -> None:
    blocker = threading.Event()
    offload._health_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sr-health")
    offload._health_executor.submit(blocker.wait, 10)  # occupy the only worker
    with pytest.raises(offload.OffloadTimeout):
        await offload.run_health_bounded(lambda: 1, 0.1)  # queued, then cancelled
    blocker.set()
    deadline = time.monotonic() + 2
    while offload._health_busy and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert offload._health_busy is False
    assert await offload.run_health_bounded(lambda: 7, 2) == 7


async def test_timed_out_queued_items_still_count_against_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelled work items linger in the pool queue; they must keep counting (#132 review)."""
    monkeypatch.setenv("SHELL_RUNNER_CLASSIFY_WORKERS", "1")
    monkeypatch.setenv("SHELL_RUNNER_CLASSIFY_MAX_QUEUED", "2")
    release = threading.Event()
    blocker = asyncio.create_task(offload.run_bounded(release.wait, 10, timeout_s=10))
    await asyncio.sleep(0.1)  # the single worker is now stuck
    for _ in range(2):
        with pytest.raises(offload.OffloadTimeout):
            await offload.run_bounded(lambda: 1, timeout_s=0.05)  # queued, then cancelled
    assert offload.classify_queued() == 0  # the live counter forgot them ...
    with pytest.raises(offload.OffloadOverloaded):  # ... but admission still sees them
        await offload.run_bounded(lambda: 1, timeout_s=0.05)
    release.set()
    await blocker


async def test_queue_admission_cap_rejects_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL_RUNNER_CLASSIFY_WORKERS", "1")
    monkeypatch.setenv("SHELL_RUNNER_CLASSIFY_MAX_QUEUED", "2")
    monkeypatch.setenv("SHELL_RUNNER_CLASSIFY_TIMEOUT_S", "10")
    release = threading.Event()
    tasks = [asyncio.create_task(offload.run_bounded(release.wait, 10)) for _ in range(3)]
    await asyncio.sleep(0.2)  # 1 running + 2 queued
    t0 = time.monotonic()
    with pytest.raises(offload.OffloadOverloaded):
        await offload.run_bounded(lambda: 1)
    assert time.monotonic() - t0 < 0.5
    assert issubclass(offload.OffloadOverloaded, offload.OffloadTimeout)  # fail closed
    release.set()
    await asyncio.gather(*tasks)
    assert offload.classify_queued() == 0
