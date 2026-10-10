"""Unit tests for the shell_wait route (issue #51, job mode).

Calls shell_wait_route directly against a fresh Persistence with hand-made
jobs rows, so no real process is involved.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

import shell_runner.server as server_module
from shell_runner.models import ShellWaitRequest
from shell_runner.persistence import Persistence
from shell_runner.server import MAX_WAIT_S, shell_wait_route


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Persistence:
    fresh = Persistence(db_path=tmp_path / "test.sqlite3")
    monkeypatch.setattr("shell_runner.server.db", fresh)
    return fresh


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shell_runner.server.WAIT_POLL_INTERVAL_S", 0.01)


def _make_job(db: Persistence, tmp_path: Path, stdout: str = "", stderr: str = "") -> str:
    job_id = str(uuid.uuid4())
    telemetry_id = str(uuid.uuid4())
    db.record_call(
        call_id=telemetry_id,
        agent_id="test-agent",
        cwd=str(tmp_path),
        raw_cmd="echo hi",
        normalized_template="echo <word>",
        command_tier=1,
        final_tier=1,
        decision="running",
        matched_rule_pattern=None,
        matched_rule_category=None,
        exit_code=None,
        stdout_bytes=0,
        stderr_bytes=0,
        duration_ms=0,
        decision_path=[],
        normalizer_warnings=[],
    )
    out = tmp_path / f"{job_id}.out"
    err = tmp_path / f"{job_id}.err"
    out.write_text(stdout)
    err.write_text(stderr)
    db.create_job(
        job_id=job_id,
        telemetry_id=telemetry_id,
        raw_cmd="echo hi",
        cwd=str(tmp_path),
        agent_id="test-agent",
        timeout_s=30,
        stdout_path=str(out),
        stderr_path=str(err),
        pid=1,
    )
    return job_id


async def test_unknown_job_is_error(db: Persistence) -> None:
    resp = await shell_wait_route(ShellWaitRequest(job_id="nope", wait_s=0))
    assert resp.state == "error"
    assert resp.job_status is None
    assert "not found" in (resp.error or "")


async def test_invalid_regex_is_error(db: Persistence, tmp_path: Path) -> None:
    job_id = _make_job(db, tmp_path)
    resp = await shell_wait_route(ShellWaitRequest(job_id=job_id, wait_s=0, result_regex="("))
    assert resp.state == "error"
    assert "invalid result_regex" in (resp.error or "")


async def test_overlong_regex_is_error(db: Persistence, tmp_path: Path) -> None:
    job_id = _make_job(db, tmp_path)
    resp = await shell_wait_route(ShellWaitRequest(job_id=job_id, wait_s=0, result_regex="a" * 257))
    assert resp.state == "error"
    assert "256" in (resp.error or "")


async def test_regex_at_limit_is_accepted(db: Persistence, tmp_path: Path) -> None:
    job_id = _make_job(db, tmp_path)
    resp = await shell_wait_route(ShellWaitRequest(job_id=job_id, wait_s=0, result_regex="a" * 256))
    assert resp.state == "waiting"


async def test_running_job_gives_waiting(db: Persistence, tmp_path: Path) -> None:
    job_id = _make_job(db, tmp_path)
    resp = await shell_wait_route(ShellWaitRequest(job_id=job_id, wait_s=0.1))
    assert resp.state == "waiting"
    assert resp.job_status == "running"
    assert resp.elapsed_s >= 0.1
    assert resp.matched_lines == []


async def test_wait_zero_checks_once_without_waiting(db: Persistence, tmp_path: Path) -> None:
    job_id = _make_job(db, tmp_path)
    resp = await shell_wait_route(ShellWaitRequest(job_id=job_id, wait_s=0))
    assert resp.state == "waiting"
    assert resp.elapsed_s < 0.1


async def test_job_finishing_mid_wait_gives_done(db: Persistence, tmp_path: Path) -> None:
    job_id = _make_job(db, tmp_path)

    async def _finish() -> None:
        await asyncio.sleep(0.1)
        db.update_job_status(job_id, status="completed", exit_code=0, finished_at="2026-01-01")

    finisher = asyncio.create_task(_finish())
    resp = await shell_wait_route(ShellWaitRequest(job_id=job_id, wait_s=5))
    await finisher
    assert resp.state == "done"
    assert resp.job_status == "completed"
    assert resp.exit_code == 0
    assert resp.elapsed_s < 2
    assert resp.stdout_full_path is not None


async def test_failed_job_is_done_with_failed_status(db: Persistence, tmp_path: Path) -> None:
    job_id = _make_job(db, tmp_path)
    db.update_job_status(job_id, status="failed", exit_code=2, finished_at="2026-01-01")
    resp = await shell_wait_route(ShellWaitRequest(job_id=job_id, wait_s=0))
    assert resp.state == "done"
    assert resp.job_status == "failed"
    assert resp.exit_code == 2


async def test_wait_s_is_clamped_to_max(
    db: Persistence, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = _make_job(db, tmp_path)
    seen: list[float] = []

    async def _fake(_job_id: str, wait_s: float) -> dict | None:
        seen.append(wait_s)
        return db.get_job(_job_id)

    monkeypatch.setattr("shell_runner.server._wait_for_terminal", _fake)
    await shell_wait_route(ShellWaitRequest(job_id=job_id, wait_s=9999))
    await shell_wait_route(ShellWaitRequest(job_id=job_id, wait_s=-5))
    assert seen == [float(MAX_WAIT_S), 0.0]


async def test_matched_lines_filter_stdout_then_stderr(db: Persistence, tmp_path: Path) -> None:
    job_id = _make_job(db, tmp_path, stdout="ok\nERROR one\nfine\n", stderr="ERROR two\nwarn\n")
    db.update_job_status(job_id, status="completed", exit_code=0, finished_at="2026-01-01")
    resp = await shell_wait_route(ShellWaitRequest(job_id=job_id, wait_s=0, result_regex=r"^ERROR"))
    assert resp.matched_lines == ["ERROR one", "ERROR two"]


async def test_no_regex_returns_no_lines(db: Persistence, tmp_path: Path) -> None:
    job_id = _make_job(db, tmp_path, stdout="secret output\n")
    db.update_job_status(job_id, status="completed", exit_code=0, finished_at="2026-01-01")
    resp = await shell_wait_route(ShellWaitRequest(job_id=job_id, wait_s=0))
    assert resp.matched_lines == []


async def test_no_matched_lines_while_waiting(db: Persistence, tmp_path: Path) -> None:
    job_id = _make_job(db, tmp_path, stdout="hit\n")
    resp = await shell_wait_route(ShellWaitRequest(job_id=job_id, wait_s=0, result_regex="hit"))
    assert resp.state == "waiting"
    assert resp.matched_lines == []


async def test_max_result_lines_caps_and_clamps(db: Persistence, tmp_path: Path) -> None:
    job_id = _make_job(db, tmp_path, stdout="".join(f"hit {i}\n" for i in range(50)))
    db.update_job_status(job_id, status="completed", exit_code=0, finished_at="2026-01-01")
    capped = await shell_wait_route(
        ShellWaitRequest(job_id=job_id, wait_s=0, result_regex="hit", max_result_lines=5)
    )
    assert capped.matched_lines == [f"hit {i}" for i in range(5)]
    low = await shell_wait_route(
        ShellWaitRequest(job_id=job_id, wait_s=0, result_regex="hit", max_result_lines=0)
    )
    assert len(low.matched_lines) == 1
    high = await shell_wait_route(
        ShellWaitRequest(job_id=job_id, wait_s=0, result_regex="hit", max_result_lines=10_000)
    )
    assert len(high.matched_lines) == 50


async def test_saturated_semaphore_does_single_check(
    db: Persistence, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_module, "_wait_semaphore", asyncio.Semaphore(0))
    job_id = _make_job(db, tmp_path)
    resp = await shell_wait_route(ShellWaitRequest(job_id=job_id, wait_s=5))
    assert resp.state == "waiting"
    assert resp.elapsed_s < 1


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_wait_s_rejected(bad: float) -> None:
    with pytest.raises(ValidationError):
        ShellWaitRequest(job_id="j", wait_s=bad)


@pytest.mark.parametrize("bad", ["NaN", "Infinity"])
async def test_dispatch_non_finite_wait_s_is_invalid_arguments(bad: str) -> None:
    result = await server_module._dispatch_tool_call(
        MagicMock(), {"name": "shell_wait", "arguments": {"job_id": "j", "wait_s": float(bad)}}
    )
    assert result == {"error": "invalid arguments"}


async def test_semaphore_released_after_wait(db: Persistence, tmp_path: Path) -> None:
    job_id = _make_job(db, tmp_path)
    db.update_job_status(job_id, status="completed", exit_code=0, finished_at="2026-01-01")
    before = server_module._wait_semaphore._value
    await shell_wait_route(ShellWaitRequest(job_id=job_id, wait_s=0))
    assert server_module._wait_semaphore._value == before
