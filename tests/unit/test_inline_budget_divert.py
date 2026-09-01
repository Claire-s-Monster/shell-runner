"""Unit tests for issue #46 "wait-then-divert" primitives in executor.py.

Covers inline_budget_s() env parsing, job_result() (including truncation
parity with execute()), and the wait_for_job()/discard_job_tracking()
completion-tracking helpers. Deliberately does NOT exercise the server-side
divert wiring (see tests/integration/test_inline_budget_divert.py for that).
"""

from __future__ import annotations

import asyncio
import importlib
import uuid
from pathlib import Path

import pytest

import shell_runner.executor as executor_module
from shell_runner.executor import (
    DEFAULT_INLINE_BUDGET_S,
    OUTPUT_HEAD_BYTES,
    OUTPUT_TAIL_BYTES,
    discard_job_tracking,
    execute,
    inline_budget_s,
    job_result,
    wait_for_job,
)
from shell_runner.persistence import Persistence


@pytest.fixture(autouse=True)
def _allow_all_cwd_roots(monkeypatch: pytest.MonkeyPatch):
    """Same jail-relaxation convention as tests/unit/test_executor.py, so this
    file's use of tmp_path as a command cwd (in the truncation-parity test)
    does not depend on whatever cwd-roots state another test file left
    behind."""
    monkeypatch.setenv("SHELL_RUNNER_CWD_ROOT", "/")
    monkeypatch.delenv("SHELL_RUNNER_CWD_ROOTS", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    importlib.reload(executor_module)
    yield
    importlib.reload(executor_module)


# ---------------------------------------------------------------------------
# inline_budget_s()
# ---------------------------------------------------------------------------


def test_inline_budget_default_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHELL_RUNNER_INLINE_BUDGET_S", raising=False)
    assert inline_budget_s() == DEFAULT_INLINE_BUDGET_S


def test_inline_budget_honours_set_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL_RUNNER_INLINE_BUDGET_S", "5")
    assert inline_budget_s() == 5


def test_inline_budget_zero_disables_diverting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL_RUNNER_INLINE_BUDGET_S", "0")
    assert inline_budget_s() == 0


def test_inline_budget_invalid_value_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHELL_RUNNER_INLINE_BUDGET_S", "not-an-int")
    assert inline_budget_s() == DEFAULT_INLINE_BUDGET_S


# ---------------------------------------------------------------------------
# job_result()
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Persistence:
    return Persistence(db_path=tmp_path / "test.sqlite3")


def _create_running_job(db: Persistence, tmp_path: Path, job_id: str) -> tuple[Path, Path]:
    """Insert a running jobs row, plus its required parent shell_calls row.

    jobs.telemetry_id has an immediate (non-deferred) FK on shell_calls(id),
    and Persistence opens every connection with PRAGMA foreign_keys=ON, so
    create_job() raises IntegrityError without a pre-existing shell_calls
    row for telemetry_id (see _prewrite_inline_budget_telemetry's docstring
    in server.py for the same constraint on the production path).
    """
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
    stdout_path = tmp_path / f"{job_id}.out"
    stderr_path = tmp_path / f"{job_id}.err"
    db.create_job(
        job_id=job_id,
        telemetry_id=telemetry_id,
        raw_cmd="echo hi",
        cwd=str(tmp_path),
        agent_id="test-agent",
        timeout_s=30,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        pid=1,
    )
    return stdout_path, stderr_path


def test_job_result_returns_none_for_unknown_job(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    assert job_result(job_id=str(uuid.uuid4()), persistence=db) is None


def test_job_result_returns_none_for_still_running_job(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    job_id = str(uuid.uuid4())
    _create_running_job(db, tmp_path, job_id)
    assert job_result(job_id=job_id, persistence=db) is None


def test_job_result_returns_real_exit_code_and_output_for_finished_job(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    job_id = str(uuid.uuid4())
    stdout_path, stderr_path = _create_running_job(db, tmp_path, job_id)
    stdout_path.write_bytes(b"hello output\n")
    stderr_path.write_bytes(b"")
    db.update_job_status(
        job_id, status="completed", exit_code=0, finished_at="2026-01-01T00:00:00+00:00"
    )

    result = job_result(job_id=job_id, persistence=db)
    assert result is not None
    assert result.exit_code == 0
    assert "hello output" in result.stdout
    assert result.timed_out is False


def test_job_result_timed_out_true_when_status_timed_out(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    job_id = str(uuid.uuid4())
    stdout_path, stderr_path = _create_running_job(db, tmp_path, job_id)
    stdout_path.write_bytes(b"")
    stderr_path.write_bytes(b"")
    db.update_job_status(
        job_id, status="timed_out", exit_code=-1, finished_at="2026-01-01T00:00:00+00:00"
    )

    result = job_result(job_id=job_id, persistence=db)
    assert result is not None
    assert result.timed_out is True


# ---------------------------------------------------------------------------
# job_result() truncation parity with execute()
# ---------------------------------------------------------------------------


def test_job_result_truncation_matches_execute_marker_text(tmp_path: Path) -> None:
    """For output exceeding the truncation threshold, job_result() must
    produce the SAME marker text as execute() for identical raw bytes, and
    point stdout_full_path at the existing job log (not a fresh overflow
    file). Parity is asserted against execute()'s own output rather than a
    hardcoded marker string.
    """
    overflow_dir = tmp_path / "overflow"
    exec_result = execute(
        command="python3 -c \"print('x' * 9000, end='')\"",
        cwd=str(tmp_path),
        overflow_dir=str(overflow_dir),
    )
    assert exec_result.exit_code == 0
    assert exec_result.stdout_full_path is not None, "sanity: execute() must have truncated this"
    raw_bytes = Path(exec_result.stdout_full_path).read_bytes()
    assert len(raw_bytes) > OUTPUT_HEAD_BYTES + OUTPUT_TAIL_BYTES

    db = _make_db(tmp_path)
    job_id = str(uuid.uuid4())
    stdout_path, stderr_path = _create_running_job(db, tmp_path, job_id)
    stdout_path.write_bytes(raw_bytes)
    stderr_path.write_bytes(b"")
    db.update_job_status(
        job_id, status="completed", exit_code=0, finished_at="2026-01-01T00:00:00+00:00"
    )

    result = job_result(job_id=job_id, persistence=db)
    assert result is not None
    assert result.stdout == exec_result.stdout, "marker text must match execute()'s exactly"
    assert result.stdout_full_path == str(stdout_path), (
        "must point at the existing job log, not a fresh overflow file"
    )


def test_job_result_full_path_none_for_small_output(tmp_path: Path) -> None:
    """For output under the truncation threshold, both execute() and
    job_result() must leave stdout_full_path unset."""
    small = b"small output\n"
    exec_result = execute(
        command="printf 'small output\\n'",
        cwd=str(tmp_path),
        overflow_dir=str(tmp_path / "overflow"),
    )
    assert exec_result.stdout_full_path is None

    db = _make_db(tmp_path)
    job_id = str(uuid.uuid4())
    stdout_path, stderr_path = _create_running_job(db, tmp_path, job_id)
    stdout_path.write_bytes(small)
    stderr_path.write_bytes(b"")
    db.update_job_status(
        job_id, status="completed", exit_code=0, finished_at="2026-01-01T00:00:00+00:00"
    )

    result = job_result(job_id=job_id, persistence=db)
    assert result is not None
    assert result.stdout_full_path is None
    assert result.stdout == exec_result.stdout


# ---------------------------------------------------------------------------
# wait_for_job() / discard_job_tracking()
# ---------------------------------------------------------------------------


async def test_wait_for_job_returns_false_for_untracked_job() -> None:
    assert await wait_for_job(str(uuid.uuid4()), 0.1) is False


def test_discard_job_tracking_removes_and_is_idempotent() -> None:
    job_id = str(uuid.uuid4())
    executor_module._job_completion[job_id] = asyncio.Event()
    discard_job_tracking(job_id)
    assert job_id not in executor_module._job_completion
    discard_job_tracking(job_id)  # second call must not raise
