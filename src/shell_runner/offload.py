"""Keep slow synchronous work off the FastAPI event loop (issue #131).

classify() is CPU/IO-bound synchronous code. Run directly inside an ``async def``
route, one stalled call (e.g. under memory thrash) freezes /observe, /health and the
JSON-RPC /mcp endpoint, which all share one loop.

Work runs in a dedicated ThreadPoolExecutor, separate from asyncio's default
executor so stuck classifications cannot starve the ``asyncio.to_thread`` DB
writers. Each call is bounded by a timeout. A timed-out call raises OffloadTimeout,
but Python cannot kill a thread: the worker may keep running until fn returns, and
its pool slot stays occupied meanwhile. That is accepted; ``classify_inflight()``
exposes how many are running so health can show it.

Env vars (read at call time, so tests can monkeypatch):
  SHELL_RUNNER_CLASSIFY_WORKERS         pool size, read when the pool is created (4)
  SHELL_RUNNER_CLASSIFY_TIMEOUT_S       per-call timeout in seconds (5.0)
  SHELL_RUNNER_OBSERVE_MAX_INFLIGHT     concurrent /observe cap (32)
"""

from __future__ import annotations

import asyncio
import functools
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from . import classifier
from .classifier import ClassificationResult

T = TypeVar("T")

LAG_INTERVAL_S = 0.5
LAG_WINDOW_S = 60.0

_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None
_running = 0  # worker calls actually executing (a timed-out thread still counts)
_observe_inflight = 0
# (monotonic timestamp, lag_ms); ~120 samples cover the 60 s window.
_lag_samples: deque[tuple[float, float]] = deque(maxlen=int(LAG_WINDOW_S / LAG_INTERVAL_S) + 8)


class OffloadTimeout(Exception):  # noqa: N818 - name fixed by the #131 design
    """The offloaded call did not finish within its timeout (its thread may still run)."""


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=_env_int("SHELL_RUNNER_CLASSIFY_WORKERS", 4),
                thread_name_prefix="sr-classify",
            )
        return _executor


def shutdown_executor() -> None:
    """Release the pool without waiting for stuck threads."""
    global _executor
    with _lock:
        pool, _executor = _executor, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


def _tracked(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    global _running
    with _lock:
        _running += 1
    try:
        return fn(*args, **kwargs)
    finally:
        with _lock:
            _running -= 1


def classify_inflight() -> int:
    """Number of offloaded calls currently executing, including timed-out ones."""
    with _lock:
        return _running


async def run_bounded(
    fn: Callable[..., T], *args: Any, timeout_s: float | None = None, **kwargs: Any
) -> T:
    """Run ``fn`` in the dedicated pool; raise OffloadTimeout after ``timeout_s``."""
    if timeout_s is None:
        timeout_s = _env_float("SHELL_RUNNER_CLASSIFY_TIMEOUT_S", 5.0)
    fut = asyncio.get_running_loop().run_in_executor(
        _get_executor(), functools.partial(_tracked, fn, *args, **kwargs)
    )
    try:
        return await asyncio.wait_for(fut, timeout_s)
    except TimeoutError:
        raise OffloadTimeout(f"timed out after {timeout_s:g}s") from None


async def classify_async(command: str, cwd: str, agent_id: str, **kw: Any) -> ClassificationResult:
    """classifier.classify off the loop. Resolved via the module so tests can patch it."""
    return await run_bounded(classifier.classify, command, cwd, agent_id, **kw)


def observe_try_acquire() -> bool:
    """Reserve an /observe slot; False when SHELL_RUNNER_OBSERVE_MAX_INFLIGHT is reached."""
    global _observe_inflight
    cap = _env_int("SHELL_RUNNER_OBSERVE_MAX_INFLIGHT", 32)
    with _lock:
        if _observe_inflight >= cap:
            return False
        _observe_inflight += 1
        return True


def observe_release() -> None:
    global _observe_inflight
    with _lock:
        _observe_inflight -= 1


async def lag_monitor() -> None:
    """Record how late each 0.5 s sleep wakes up: a direct measure of loop stalls."""
    while True:
        start = time.monotonic()
        await asyncio.sleep(LAG_INTERVAL_S)
        now = time.monotonic()
        _lag_samples.append((now, max(0.0, (now - start - LAG_INTERVAL_S) * 1000)))


def event_loop_lag_ms() -> float | None:
    """Max loop lag over the last ~60 s; None if the monitor is not running."""
    cutoff = time.monotonic() - LAG_WINDOW_S
    recent = [lag for ts, lag in tuple(_lag_samples) if ts >= cutoff]
    return round(max(recent), 1) if recent else None
