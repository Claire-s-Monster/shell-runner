"""shell_status / shell_wait / shell_kill for kind='unit' jobs (issue #81, step 6).

A FakeUnitBackend stands in for systemd; nothing here spawns a unit.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shell_runner import executor
from shell_runner import server as srv
from shell_runner.persistence import Persistence
from shell_runner.unit_runner import SystemdBackend, UnitResult

AGENT = "focused-code-modifier"


class FakeUnitBackend:
    def __init__(self) -> None:
        self.results: dict[str, UnitResult] = {}
        self.killed: list[str] = []
        self.on_kill: Callable[[str], None] | None = None
        self.kill_error: Exception | None = None
        self.active: set[str] = set()

    async def start(self, argv: list[str]) -> None:
        return None

    async def kill(self, job_id: str) -> None:
        self.killed.append(job_id)
        if self.on_kill is not None:
            self.on_kill(job_id)
        if self.kill_error is not None:
            raise self.kill_error

    async def active_units(self) -> set[str]:
        return set(self.active)

    def read_result(self, job_dir: Path) -> UnitResult | None:
        return self.results.get(Path(job_dir).name)


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Persistence:
    fresh = Persistence(db_path=tmp_path / "test.sqlite3")
    monkeypatch.setattr(srv, "db", fresh)
    return fresh


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> FakeUnitBackend:
    fake = FakeUnitBackend()
    monkeypatch.setattr(executor, "_unit_backend", fake)
    return fake


@pytest.fixture
def client(db: Persistence, backend: FakeUnitBackend) -> TestClient:
    return TestClient(srv.app)


def _job(db: Persistence, tmp_path: Path, kind: str = "unit", pid: int | None = None) -> str:
    job_id = str(uuid.uuid4())
    telemetry_id = db.record_call(
        agent_id=AGENT,
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
    job_dir = tmp_path / "jobs" / job_id
    job_dir.mkdir(parents=True)
    db.create_job(
        job_id=job_id,
        telemetry_id=telemetry_id,
        raw_cmd="x",
        cwd="/tmp",
        agent_id=AGENT,
        timeout_s=10,
        stdout_path=str(job_dir / "stdout.log"),
        stderr_path=str(job_dir / "stderr.log"),
        pid=pid,
        kind=kind,
    )
    return job_id


def _call(db: Persistence, job_id: str) -> tuple[str, int | None]:
    job = db.get_job(job_id)
    assert job is not None
    with db._conn() as conn:
        row = conn.execute(
            "SELECT decision, exit_code FROM shell_calls WHERE id = ?", (job["telemetry_id"],)
        ).fetchone()
    return row[0], row[1]


def _status(client: TestClient, job_id: str) -> dict:
    resp = client.post("/tools/shell_status", json={"job_id": job_id})
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- lazy refresh from the result file -----------------------------------------


@pytest.mark.parametrize(
    ("result", "status", "exit_code"),
    [
        (UnitResult("0", "success"), "completed", 0),
        (UnitResult("3", "exit-code"), "failed", 3),
        (UnitResult("TERM", "signal"), "failed", None),
        (UnitResult("TERM", "timeout"), "timed_out", None),
    ],
)
def test_status_maps_result_file(
    client: TestClient,
    db: Persistence,
    backend: FakeUnitBackend,
    tmp_path: Path,
    result: UnitResult,
    status: str,
    exit_code: int | None,
) -> None:
    job_id = _job(db, tmp_path)
    backend.results[job_id] = result
    body = _status(client, job_id)
    assert body["status"] == status
    assert body["exit_code"] == exit_code
    assert body["finished_at"] is not None
    assert _call(db, job_id) == ("executed", exit_code)


def test_status_leaves_active_unit_running(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    job_id = _job(db, tmp_path)  # no result file yet
    body = _status(client, job_id)
    assert body["status"] == "running"
    assert body["finished_at"] is None
    assert _call(db, job_id)[0] == "running"


def test_status_does_not_consult_backend_for_process_rows(
    client: TestClient, db: Persistence, backend: FakeUnitBackend, tmp_path: Path
) -> None:
    job_id = _job(db, tmp_path, kind="process", pid=1)
    backend.results[job_id] = UnitResult("0", "success")
    assert _status(client, job_id)["status"] == "running"


# --- shell_wait -------------------------------------------------------------------


def test_wait_reaches_done_via_refresh(
    client: TestClient, db: Persistence, backend: FakeUnitBackend, tmp_path: Path
) -> None:
    job_id = _job(db, tmp_path)
    backend.results[job_id] = UnitResult("0", "success")
    resp = client.post("/tools/shell_wait", json={"job_id": job_id, "wait_s": 0})
    body = resp.json()
    assert body["state"] == "done"
    assert body["job_status"] == "completed"
    assert body["exit_code"] == 0


def test_wait_stays_waiting_while_unit_runs(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    job_id = _job(db, tmp_path)
    body = client.post("/tools/shell_wait", json={"job_id": job_id, "wait_s": 0}).json()
    assert body["state"] == "waiting"
    assert body["job_status"] == "running"


# --- shell_kill -------------------------------------------------------------------


def test_kill_unit_job(
    client: TestClient, db: Persistence, backend: FakeUnitBackend, tmp_path: Path
) -> None:
    job_id = _job(db, tmp_path)
    resp = client.post("/tools/shell_kill", json={"job_id": job_id})
    assert resp.json()["killed"] is True
    assert backend.killed == [job_id]
    job = db.get_job(job_id)
    assert job is not None
    assert job["status"] == "killed"
    assert job["finished_at"] is not None
    assert _call(db, job_id)[0] == "executed"


def test_kill_cannot_target_non_unit_row_through_unit_path(
    client: TestClient,
    db: Persistence,
    backend: FakeUnitBackend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(srv.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    job_id = _job(db, tmp_path, kind="process", pid=424242)
    client.post("/tools/shell_kill", json={"job_id": job_id})
    assert backend.killed == []
    assert [pid for pid, _ in sent] == [424242]


def test_kill_unknown_job_never_reaches_backend(
    client: TestClient, backend: FakeUnitBackend
) -> None:
    resp = client.post("/tools/shell_kill", json={"job_id": "sr-job-x*.service"})
    assert resp.json()["killed"] is False
    assert backend.killed == []


def test_killed_is_not_overwritten_by_later_refresh(
    client: TestClient, db: Persistence, backend: FakeUnitBackend, tmp_path: Path
) -> None:
    job_id = _job(db, tmp_path)
    client.post("/tools/shell_kill", json={"job_id": job_id})
    # systemd's ExecStopPost writes a result for the stopped unit afterwards.
    backend.results[job_id] = UnitResult("TERM", "signal")
    assert _status(client, job_id)["status"] == "killed"
    wait = client.post("/tools/shell_wait", json={"job_id": job_id, "wait_s": 0}).json()
    assert wait["job_status"] == "killed"


def test_stale_refresh_cannot_overwrite_killed(
    client: TestClient, db: Persistence, backend: FakeUnitBackend, tmp_path: Path
) -> None:
    """The race itself: refresh read the row while 'running', kill landed, refresh writes."""
    job_id = _job(db, tmp_path)
    stale = db.get_job(job_id)
    assert stale is not None
    client.post("/tools/shell_kill", json={"job_id": job_id})
    backend.results[job_id] = UnitResult("0", "success")
    executor.refresh_unit_job(db, stale, set(), backend)
    job = db.get_job(job_id)
    assert job is not None
    assert job["status"] == "killed"
    assert job["exit_code"] is None


def test_refresh_during_kill_cannot_record_failed(
    client: TestClient, db: Persistence, backend: FakeUnitBackend, tmp_path: Path
) -> None:
    """A refresh that runs inside backend.kill sees the dying unit's 'signal' result."""
    job_id = _job(db, tmp_path)
    stale = db.get_job(job_id)
    assert stale is not None

    def refresh_mid_kill(_: str) -> None:
        backend.results[job_id] = UnitResult("TERM", "signal")  # would map to 'failed'
        executor.refresh_unit_job(db, db.get_job(job_id) or stale, set(), backend)
        executor.refresh_unit_job(db, stale, set(), backend)

    backend.on_kill = refresh_mid_kill
    assert client.post("/tools/shell_kill", json={"job_id": job_id}).json()["killed"] is True
    job = db.get_job(job_id)
    assert job is not None
    assert job["status"] == "killed"
    assert _call(db, job_id)[0] == "executed"


def test_kill_error_with_unit_gone_keeps_killed(
    client: TestClient, db: Persistence, backend: FakeUnitBackend, tmp_path: Path
) -> None:
    job_id = _job(db, tmp_path)
    backend.kill_error = OSError("boom")  # unit not in backend.active
    assert client.post("/tools/shell_kill", json={"job_id": job_id}).json()["killed"] is True
    job = db.get_job(job_id)
    assert job is not None
    assert job["status"] == "killed"


def test_kill_error_with_unit_alive_reverts_to_running(
    client: TestClient, db: Persistence, backend: FakeUnitBackend, tmp_path: Path
) -> None:
    job_id = _job(db, tmp_path)
    backend.kill_error = OSError("boom")
    backend.active = {job_id}
    body = client.post("/tools/shell_kill", json={"job_id": job_id}).json()
    assert body["killed"] is False
    assert "boom" in body["reason"]
    job = db.get_job(job_id)
    assert job is not None
    assert job["status"] == "running"
    assert job["finished_at"] is None
    assert _call(db, job_id)[0] == "running"


def test_kill_does_not_clobber_already_finished_unit(
    client: TestClient, db: Persistence, backend: FakeUnitBackend, tmp_path: Path
) -> None:
    job_id = _job(db, tmp_path)
    backend.results[job_id] = UnitResult("0", "success")
    _status(client, job_id)  # refresh lands 'completed'
    resp = client.post("/tools/shell_kill", json={"job_id": job_id})
    assert resp.json()["killed"] is False
    assert backend.killed == []


# --- security hardening (review findings 1, 2, 3, 5) ------------------------------


def _write_result(tmp_path: Path, job_id: str, text: str) -> None:
    (tmp_path / "jobs" / job_id / "result").write_text(text)


@pytest.mark.parametrize("text", ["² success\n", "0 success" + " x" * 200, "0 SUCCESS\n", ""])
def test_unparseable_result_marks_failed_not_stuck(
    client: TestClient, db: Persistence, backend: FakeUnitBackend, tmp_path: Path, text: str
) -> None:
    job_id = _job(db, tmp_path)
    _write_result(tmp_path, job_id, text)
    backend.read_result = SystemdBackend().read_result  # type: ignore[method-assign]
    body = _status(client, job_id)
    assert body["status"] == "failed"
    assert body["exit_code"] is None
    assert db.count_running_units() == 0


async def test_reconcile_isolates_unparseable_row(
    db: Persistence, backend: FakeUnitBackend, tmp_path: Path
) -> None:
    bad = _job(db, tmp_path)
    good = _job(db, tmp_path)
    _write_result(tmp_path, bad, "² success\n")
    _write_result(tmp_path, good, "0 success\n")
    backend.read_result = SystemdBackend().read_result  # type: ignore[method-assign]
    assert await executor.reconcile_units(db, backend) == 2
    assert (db.get_job(bad) or {})["status"] == "failed"
    assert (db.get_job(good) or {})["status"] == "completed"


def test_spoofed_result_ignored_while_unit_active(
    client: TestClient, db: Persistence, backend: FakeUnitBackend, tmp_path: Path
) -> None:
    job_id = _job(db, tmp_path)
    backend.results[job_id] = UnitResult("0", "success")  # the job wrote it itself
    backend.active = {job_id}
    assert _status(client, job_id)["status"] == "running"
    wait = client.post("/tools/shell_wait", json={"job_id": job_id, "wait_s": 0}).json()
    assert wait["job_status"] == "running"
    assert db.count_running_units() == 1
    assert client.post("/tools/shell_kill", json={"job_id": job_id}).json()["killed"] is True


def test_refresh_leaves_row_untouched_when_backend_unqueryable(
    client: TestClient,
    db: Persistence,
    backend: FakeUnitBackend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = _job(db, tmp_path)
    backend.results[job_id] = UnitResult("0", "success")

    async def boom() -> set[str]:
        raise OSError("no bus")

    monkeypatch.setattr(backend, "active_units", boom)
    assert _status(client, job_id)["status"] == "running"
    assert db.count_running_units() == 1


class _Proc:
    def __init__(self, rc: int, out: bytes = b"", err: bytes = b"") -> None:
        self.returncode, self._out, self._err = rc, out, err

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._out, self._err


def _patch_systemctl(
    monkeypatch: pytest.MonkeyPatch, job_id: str, *, stop_rc: int, stop_err: bytes, alive: bool
) -> None:
    async def fake(*argv: str, **kwargs: object) -> _Proc:
        if "stop" in argv:
            return _Proc(stop_rc, err=stop_err)
        out = f"sr-job-{job_id}.service loaded active running x\n".encode() if alive else b""
        return _Proc(0, out=out)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)


def test_kill_reverts_when_systemctl_stop_fails_and_unit_alive(
    client: TestClient, db: Persistence, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = _job(db, tmp_path)
    monkeypatch.setattr(executor, "_unit_backend", SystemdBackend())
    _patch_systemctl(monkeypatch, job_id, stop_rc=1, stop_err=b"Access denied", alive=True)
    body = client.post("/tools/shell_kill", json={"job_id": job_id}).json()
    assert body["killed"] is False
    assert (db.get_job(job_id) or {})["status"] == "running"
    assert db.count_running_units() == 1


def test_kill_succeeds_when_unit_not_loaded(
    client: TestClient, db: Persistence, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = _job(db, tmp_path)
    monkeypatch.setattr(executor, "_unit_backend", SystemdBackend())
    err = f"Failed to stop sr-job-{job_id}.service: Unit sr-job-{job_id}.service not loaded."
    _patch_systemctl(monkeypatch, job_id, stop_rc=5, stop_err=err.encode(), alive=False)
    assert client.post("/tools/shell_kill", json={"job_id": job_id}).json()["killed"] is True
    assert (db.get_job(job_id) or {})["status"] == "killed"


async def test_kill_holds_detach_lock_during_mark_and_stop(
    db: Persistence, backend: FakeUnitBackend, tmp_path: Path
) -> None:
    job_id = _job(db, tmp_path)
    held_during_stop: list[bool] = []
    backend.on_kill = lambda _: held_during_stop.append(srv._detach_lock.locked())
    async with srv._detach_lock:  # a concurrent detach is mid-cap-check
        task = asyncio.create_task(srv._kill_unit_job(db.get_job(job_id) or {}))
        await asyncio.sleep(0.05)
        assert not task.done()
        assert db.count_running_units() == 1  # not yet marked killed
    assert (await task).killed is True
    assert held_during_stop == [True]
