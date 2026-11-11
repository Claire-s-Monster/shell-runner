"""Fan-out cap on detached units (issue #81, step 5).

SHELL_RUNNER_MAX_DETACHED (positive int, default 4) bounds the number of running
kind='unit' jobs. The check and the spawn share one asyncio.Lock. Nothing here
spawns systemd-run: executor._unit_backend is a fake.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from shell_runner import executor
from shell_runner import server as srv
from shell_runner.persistence import Persistence
from shell_runner.unit_runner import DEFAULT_MAX_DETACHED, UnitResult, resolve_max_detached

AGENT = "focused-code-modifier"
T3_CMD = "mytool_unknown --flag"


class FakeUnitBackend:
    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.started: list[list[str]] = []

    async def start(self, argv: list[str]) -> None:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        self.started.append(argv)

    async def kill(self, job_id: str) -> None:
        return None

    async def active_units(self) -> set[str]:
        return set()

    def read_result(self, job_dir: Path) -> UnitResult | None:
        return None


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Persistence:
    fresh = Persistence(db_path=tmp_path / "test.sqlite3")
    monkeypatch.setattr(srv, "db", fresh)
    monkeypatch.setattr(executor, "_CWD_ROOTS", [tmp_path.resolve()])
    monkeypatch.setenv("SHELL_RUNNER_JOB_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(srv, "_detach_lock", asyncio.Lock())
    return fresh


@pytest.fixture
def backend(db: Persistence, monkeypatch: pytest.MonkeyPatch) -> FakeUnitBackend:
    fake = FakeUnitBackend()
    monkeypatch.setattr(executor, "_unit_backend", fake)
    return fake


@pytest.fixture
def cap2(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setenv("SHELL_RUNNER_MAX_DETACHED", "2")
    return 2


def _body(cwd: Path, command: str = "echo hi", **extra: object) -> dict:
    return {"command": command, "cwd": str(cwd), "agent_id": AGENT, "detach": True, **extra}


def _job(db: Persistence, job_id: str, kind: str, status: str = "running") -> None:
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
    db.create_job(
        job_id=job_id,
        telemetry_id=telemetry_id,
        raw_cmd="x",
        cwd="/tmp",
        agent_id=AGENT,
        timeout_s=1,
        stdout_path="/tmp/o",
        stderr_path="/tmp/e",
        pid=None,
        kind=kind,
    )
    if status != "running":
        db.update_job_status(job_id, status=status, exit_code=0, finished_at="2026-01-01")


# --- Persistence.count_running_units / env parsing ---------------------------


def test_count_running_units_counts_only_running_unit_jobs(db: Persistence) -> None:
    assert db.count_running_units() == 0
    _job(db, "a", "unit")
    _job(db, "b", "unit")
    _job(db, "c", "unit", status="killed")
    _job(db, "d", "process")
    _job(db, "e", "monitor")
    assert db.count_running_units() == 2


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_MAX_DETACHED),
        ("7", 7),
        ("1", 1),
        ("0", DEFAULT_MAX_DETACHED),
        ("-3", DEFAULT_MAX_DETACHED),
        ("abc", DEFAULT_MAX_DETACHED),
        ("", DEFAULT_MAX_DETACHED),
        ("2.5", DEFAULT_MAX_DETACHED),
    ],
)
def test_resolve_max_detached(raw: str | None, expected: int) -> None:
    environ = {} if raw is None else {"SHELL_RUNNER_MAX_DETACHED": raw}
    assert resolve_max_detached(environ) == expected


def test_default_cap_is_four() -> None:
    assert DEFAULT_MAX_DETACHED == 4


# --- cap enforcement ---------------------------------------------------------


def test_nth_succeeds_and_next_is_refused(
    db: Persistence, backend: FakeUnitBackend, cap2: int, tmp_path: Path
) -> None:
    client = TestClient(srv.app)
    for _ in range(cap2):
        resp = client.post("/execute", json=_body(tmp_path))
        assert resp.status_code == 200, resp.text
        assert resp.json()["decision"] == "running"
    refused = client.post("/execute", json=_body(tmp_path))
    assert refused.status_code == 429
    assert "SHELL_RUNNER_MAX_DETACHED" in refused.json()["detail"]
    assert len(backend.started) == cap2
    assert db.count_running_units() == cap2

    # attached runs are not counted against the cap
    plain = client.post("/execute", json={**_body(tmp_path), "detach": False})
    assert plain.status_code == 200


def test_finished_unit_frees_a_slot(
    db: Persistence, backend: FakeUnitBackend, cap2: int, tmp_path: Path
) -> None:
    client = TestClient(srv.app)
    ids = [client.post("/execute", json=_body(tmp_path)).json()["job_id"] for _ in range(cap2)]
    assert client.post("/execute", json=_body(tmp_path)).status_code == 429
    db.update_job_status(ids[0], status="killed", exit_code=None, finished_at="2026-01-01")
    assert client.post("/execute", json=_body(tmp_path)).status_code == 200
    assert len(backend.started) == cap2 + 1


def test_prompted_command_checks_cap_at_dispatch_after_approval(
    db: Persistence, backend: FakeUnitBackend, cap2: int, tmp_path: Path
) -> None:
    client = TestClient(srv.app)
    first = client.post("/execute", json=_body(tmp_path, T3_CMD)).json()
    assert first["decision"] == "prompt_required"  # the cap does not pre-empt the prompt
    approved = client.post(
        "/approve_pending",
        json={
            "prompt_id": first["prompt"]["id"],
            "decision": "approve_once",
            "approver_agent_id": "primary",
        },
    ).json()
    for _ in range(cap2):  # the slots fill up while the approval is pending
        assert client.post("/execute", json=_body(tmp_path)).status_code == 200
    resp = client.post(
        "/execute", json=_body(tmp_path, T3_CMD, approve_token=approved["approve_token"])
    )
    assert resp.status_code == 429
    assert len(backend.started) == cap2


async def test_concurrent_dispatches_cannot_exceed_cap(
    db: Persistence, monkeypatch: pytest.MonkeyPatch, cap2: int, tmp_path: Path
) -> None:
    slow = FakeUnitBackend(delay_s=0.05)
    monkeypatch.setattr(executor, "_unit_backend", slow)
    transport = httpx.ASGITransport(app=srv.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        responses = await asyncio.gather(
            *(client.post("/execute", json=_body(tmp_path)) for _ in range(6))
        )
    codes = sorted(r.status_code for r in responses)
    assert codes == [200] * cap2 + [429] * (6 - cap2)
    assert len(slow.started) == cap2
    assert db.count_running_units() == cap2
