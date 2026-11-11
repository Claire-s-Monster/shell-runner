"""executor.execute_detached: sandbox reuse, kind='unit' row, start-failure cleanup.

A FakeUnitBackend stands in for systemd; nothing here spawns systemd-run.
"""

from __future__ import annotations

import importlib
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from shell_runner import executor
from shell_runner.persistence import Persistence
from shell_runner.unit_runner import UnitResult, UnitStartError


class FakeUnitBackend:
    def __init__(self, *, fail_start: bool = False) -> None:
        self.fail_start = fail_start
        self.started: list[list[str]] = []
        self.killed: list[str] = []

    async def start(self, argv: list[str]) -> None:
        if self.fail_start:
            raise UnitStartError("boom")
        self.started.append(argv)

    async def kill(self, job_id: str) -> None:
        self.killed.append(job_id)

    async def active_units(self) -> set[str]:
        return set()

    def read_result(self, job_dir: Path) -> UnitResult | None:
        return None


@pytest.fixture(autouse=True)
def _allow_all_cwd_roots(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Same single-root jail override as tests/unit/test_executor.py."""
    monkeypatch.setenv("SHELL_RUNNER_CWD_ROOT", "/")
    monkeypatch.delenv("SHELL_RUNNER_CWD_ROOTS", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    importlib.reload(executor)
    yield
    importlib.reload(executor)


@pytest.fixture
def db(tmp_path: Path) -> Persistence:
    return Persistence(db_path=tmp_path / "t.sqlite3")


@pytest.fixture(autouse=True)
def job_dir_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "jobs"
    monkeypatch.setenv("SHELL_RUNNER_JOB_DIR", str(base))
    return base


def _telemetry_id(db: Persistence) -> str:
    return db.record_call(
        agent_id="agent",
        cwd="/tmp",
        raw_cmd="x",
        normalized_template="x",
        command_tier=1,
        final_tier=1,
        decision="running",
        matched_rule_pattern=None,
        matched_rule_category=None,
        exit_code=None,
        stdout_bytes=0,
        stderr_bytes=0,
        duration_ms=0,
        decision_path=["detached"],
        normalizer_warnings=[],
    )


async def _run(
    db: Persistence,
    backend: FakeUnitBackend,
    *,
    cwd: str,
    command: str = "echo hi",
    env_passthrough: list[str] | None = None,
    job_id: str | None = None,
) -> str:
    job_id = job_id or str(uuid.uuid4())
    await executor.execute_detached(
        db,
        job_id,
        command,
        cwd,
        env_passthrough,
        backend,
        telemetry_id=_telemetry_id(db),
        agent_id="agent",
    )
    return job_id


async def test_cwd_error_raises_and_touches_nothing(db: Persistence, job_dir_base: Path) -> None:
    backend = FakeUnitBackend()
    with pytest.raises(ValueError, match="cwd"):
        await _run(db, backend, cwd="/nonexistent/dir/xyz")
    assert backend.started == []
    assert not job_dir_base.exists() or list(job_dir_base.iterdir()) == []
    assert db.list_running_jobs() == []


async def test_env_is_exactly_build_env_output(
    db: Persistence, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str] | None] = []
    sandbox_env = {"ONLY": "this", "PATH": "/p"}

    def fake_build_env(passthrough: list[str] | None) -> dict[str, str]:
        seen.append(passthrough)
        return sandbox_env

    monkeypatch.setattr(executor, "_build_env", fake_build_env)
    monkeypatch.setenv("LEAKY_SECRET", "nope")
    backend = FakeUnitBackend()

    await _run(db, backend, cwd=str(tmp_path), env_passthrough=["FOO"])

    assert seen == [["FOO"]]
    argv = backend.started[0]
    tail = argv[argv.index("--") + 1 :]
    assert tail == ["env", "-i", "ONLY=this", "PATH=/p", "/bin/bash", "-c", "echo hi"]
    assert "LEAKY_SECRET" not in " ".join(argv)


async def test_row_written_with_kind_unit_and_no_pid(
    db: Persistence, tmp_path: Path, job_dir_base: Path
) -> None:
    backend = FakeUnitBackend()
    job_id = await _run(db, backend, cwd=str(tmp_path), command="sleep 1")

    row = db.get_job(job_id)
    assert row is not None
    assert row["kind"] == "unit"
    assert row["pid"] is None
    assert row["status"] == "running"
    assert row["raw_cmd"] == "sleep 1"
    job_dir = job_dir_base / job_id
    assert job_dir.is_dir()
    assert row["stdout_path"] == str(job_dir / "stdout.log")
    assert row["stderr_path"] == str(job_dir / "stderr.log")
    assert any(a == f"--unit=sr-job-{job_id}" for a in backend.started[0])
    assert f"WorkingDirectory={tmp_path.resolve()}" in backend.started[0]


async def test_start_failure_leaves_no_row_and_cleans_job_dir(
    db: Persistence, tmp_path: Path, job_dir_base: Path
) -> None:
    backend = FakeUnitBackend(fail_start=True)
    job_id = str(uuid.uuid4())
    with pytest.raises(UnitStartError):
        await _run(db, backend, cwd=str(tmp_path), job_id=job_id)

    assert db.get_job(job_id) is None
    assert db.list_running_jobs() == []
    assert not (job_dir_base / job_id).exists()


async def test_percent_in_cwd_rejected_before_anything_starts(
    db: Persistence, tmp_path: Path, job_dir_base: Path
) -> None:
    odd = tmp_path / "a%h"
    odd.mkdir()
    backend = FakeUnitBackend()
    with pytest.raises(ValueError, match="cwd"):
        await _run(db, backend, cwd=str(odd))
    assert backend.started == []
    assert not job_dir_base.exists() or list(job_dir_base.iterdir()) == []
    assert db.list_running_jobs() == []


async def test_dollar_in_job_dir_rejected_before_anything_starts(
    db: Persistence, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "j$dir"
    monkeypatch.setenv("SHELL_RUNNER_JOB_DIR", str(base))
    backend = FakeUnitBackend()
    with pytest.raises(ValueError, match="job_dir"):
        await _run(db, backend, cwd=str(tmp_path))
    assert backend.started == []
    assert not base.exists()
    assert db.list_running_jobs() == []


async def test_bad_job_id_rejected_before_mkdir(
    db: Persistence, tmp_path: Path, job_dir_base: Path
) -> None:
    backend = FakeUnitBackend()
    with pytest.raises(ValueError, match="job_id"):
        await _run(db, backend, cwd=str(tmp_path), job_id="../escape")
    assert backend.started == []
    assert not (job_dir_base.parent / "escape").exists()


async def test_persist_failure_kills_started_unit_and_cleans_up(
    db: Persistence,
    tmp_path: Path,
    job_dir_base: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(**kwargs: object) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "create_job", boom)
    backend = FakeUnitBackend()
    job_id = str(uuid.uuid4())
    with pytest.raises(RuntimeError, match="db down"):
        await _run(db, backend, cwd=str(tmp_path), job_id=job_id)

    assert backend.killed == [job_id]
    assert not (job_dir_base / job_id).exists()


async def test_default_backend_is_module_seam(
    db: Persistence, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeUnitBackend()
    monkeypatch.setattr(executor, "_unit_backend", backend)
    await executor.execute_detached(
        db,
        str(uuid.uuid4()),
        "true",
        str(tmp_path),
        None,
        telemetry_id=_telemetry_id(db),
        agent_id="agent",
    )
    assert len(backend.started) == 1
