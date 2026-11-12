"""`detach` on shell_execute: approval binding and dispatch (issue #81, step 4).

An approval is bound to the detach value it was granted for: tokens, token-less
approve_once and reusable (template / verb) approvals never carry across a
different detach value. Nothing here spawns systemd-run: the real dispatch tests
swap executor._unit_backend for a fake.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shell_runner import executor
from shell_runner import server as srv
from shell_runner.classifier import classify
from shell_runner.models import ExecuteResponse
from shell_runner.persistence import Persistence
from shell_runner.unit_runner import UnitResult

AGENT = "focused-code-modifier"  # AUTO_CAPPED cap
T4_CMD = "kill -9 12345"  # promotable T4
T3_CMD = "mytool_unknown --flag"  # unknown: T3 prompt


class FakeUnitBackend:
    def __init__(self) -> None:
        self.started: list[list[str]] = []

    async def start(self, argv: list[str]) -> None:
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
    return fresh


@pytest.fixture
def client(db: Persistence) -> TestClient:
    return TestClient(srv.app)


@pytest.fixture
def calls(db: Persistence, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []

    async def _spy(req: Any, ctx: Any, *args: Any, **kwargs: Any) -> ExecuteResponse:
        recorded.append({"req": req, "ctx": ctx, "kwargs": kwargs})
        return ExecuteResponse(
            decision="executed",
            tier=ctx.final_tier,
            matched_rule=None,
            exit_code=0,
            stdout="",
            stderr="",
            stdout_full_path=None,
            stderr_full_path=None,
            duration_ms=0,
            telemetry_id="spy",
        )

    monkeypatch.setattr(srv, "_dispatch_approved", _spy)
    return recorded


@pytest.fixture
def backend(db: Persistence, monkeypatch: pytest.MonkeyPatch) -> FakeUnitBackend:
    fake = FakeUnitBackend()
    monkeypatch.setattr(executor, "_unit_backend", fake)
    return fake


def _post(client: TestClient, cmd: str, cwd: Path, **extra: Any) -> Any:
    body = {"command": cmd, "cwd": str(cwd), "agent_id": AGENT, **extra}
    return client.post("/execute", json=body)


def _prompt(client: TestClient, cmd: str, cwd: Path, **extra: Any) -> dict:
    resp = _post(client, cmd, cwd, **extra)
    assert resp.status_code == 200, resp.text
    assert resp.json()["decision"] == "prompt_required"
    return resp.json()["prompt"]


def _approve(client: TestClient, prompt_id: str, decision: str = "approve_once") -> dict:
    resp = client.post(
        "/approve_pending",
        json={"prompt_id": prompt_id, "decision": decision, "approver_agent_id": "primary"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- auto-exec / T3 / DENY unchanged by detach --------------------------------


def test_auto_exec_reaches_helper_with_same_tier(
    client: TestClient, calls: list[dict[str, Any]], tmp_path: Path
) -> None:
    assert _post(client, "echo hi", tmp_path).status_code == 200
    assert _post(client, "echo hi", tmp_path, detach=True).status_code == 200
    assert len(calls) == 2
    assert [c["req"].detach for c in calls] == [False, True]
    assert calls[0]["ctx"].final_tier == calls[1]["ctx"].final_tier  # no tier bump


def test_t3_detach_still_prompts_and_records_detach(
    client: TestClient, calls: list[dict[str, Any]], db: Persistence, tmp_path: Path
) -> None:
    prompt = _prompt(client, T3_CMD, tmp_path, detach=True)
    assert prompt["detach"] is True
    assert "detach=true" in prompt["why"]
    assert "detach=true" in prompt["next_steps"]
    assert calls == []
    assert db.get_pending_prompt(prompt["id"])["detach"] == 1
    listed = client.post("/pending", json={"prompt_id": prompt["id"]}).json()["prompts"][0]
    assert listed["detach"] is True
    assert _prompt(client, T3_CMD, tmp_path)["detach"] is False


def test_deny_still_denied(client: TestClient, calls: list[dict[str, Any]], tmp_path: Path) -> None:
    resp = _post(client, "rm -rf /", tmp_path, detach=True)
    assert resp.json()["decision"] == "denied"
    assert calls == []


# --- approved paths reach the helper exactly once with detach ----------------


def test_approve_token_path_reaches_helper_once_with_detach(
    client: TestClient, calls: list[dict[str, Any]], tmp_path: Path
) -> None:
    prompt = _prompt(client, T3_CMD, tmp_path, detach=True)
    token = _approve(client, prompt["id"])["approve_token"]
    resp = _post(client, T3_CMD, tmp_path, detach=True, approve_token=token)
    assert resp.status_code == 200, resp.text
    assert len(calls) == 1
    assert calls[0]["req"].detach is True
    assert calls[0]["kwargs"]["approval_consumed"] is True
    assert _post(client, T3_CMD, tmp_path, detach=True, approve_token=token).status_code == 403
    assert len(calls) == 1


def test_approve_once_tokenless_reaches_helper_once_with_detach(
    client: TestClient, calls: list[dict[str, Any]], tmp_path: Path
) -> None:
    prompt = _prompt(client, T3_CMD, tmp_path, detach=True)
    _approve(client, prompt["id"])
    assert _post(client, T3_CMD, tmp_path, detach=True).json()["decision"] == "executed"
    assert len(calls) == 1
    assert calls[0]["req"].detach is True
    assert _post(client, T3_CMD, tmp_path, detach=True).json()["decision"] == "prompt_required"
    assert len(calls) == 1


# --- approval binding (b) ------------------------------------------------------


@pytest.mark.parametrize(("granted", "reused"), [(False, True), (True, False)])
def test_token_refused_on_detach_mismatch(
    client: TestClient,
    calls: list[dict[str, Any]],
    tmp_path: Path,
    granted: bool,
    reused: bool,
) -> None:
    prompt = _prompt(client, T3_CMD, tmp_path, detach=granted)
    token = _approve(client, prompt["id"])["approve_token"]
    bad = _post(client, T3_CMD, tmp_path, detach=reused, approve_token=token)
    assert bad.status_code == 403
    assert "detach" in bad.json()["detail"]
    assert calls == []
    # the refusal did not burn the token: the matching value still works
    good = _post(client, T3_CMD, tmp_path, detach=granted, approve_token=token)
    assert good.status_code == 200, good.text
    assert len(calls) == 1
    assert calls[0]["req"].detach is granted


@pytest.mark.parametrize(("granted", "reused"), [(False, True), (True, False)])
def test_tokenless_approve_once_not_reused_across_detach(
    client: TestClient,
    calls: list[dict[str, Any]],
    tmp_path: Path,
    granted: bool,
    reused: bool,
) -> None:
    prompt = _prompt(client, T3_CMD, tmp_path, detach=granted)
    _approve(client, prompt["id"])
    assert _post(client, T3_CMD, tmp_path, detach=reused).json()["decision"] == "prompt_required"
    assert calls == []
    assert _post(client, T3_CMD, tmp_path, detach=granted).json()["decision"] == "executed"
    assert len(calls) == 1


# --- reusable approvals never apply to detach (c) ----------------------------


def test_existing_template_and_verb_rows_do_not_auto_approve_detach(
    client: TestClient, calls: list[dict[str, Any]], db: Persistence, tmp_path: Path
) -> None:
    template = classify(T4_CMD, str(tmp_path), AGENT).template
    db.create_template_approval(template=template, agent_id=AGENT, approved_tier=2)
    db.create_template_approval(template=template, agent_id=None, approved_tier=2)
    db.create_verb_approval(verb="kill", cwd_prefix=str(tmp_path), agent_id=AGENT, approved_tier=2)

    assert _post(client, T4_CMD, tmp_path).json()["decision"] == "executed"  # rows still work
    assert len(calls) == 1
    detached = _post(client, T4_CMD, tmp_path, detach=True)
    assert detached.json()["decision"] == "prompt_required"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "decision", ["approve_template", "approve_template_global", "approve_verb"]
)
def test_reusable_approval_on_detach_prompt_is_one_shot(
    client: TestClient,
    calls: list[dict[str, Any]],
    db: Persistence,
    tmp_path: Path,
    decision: str,
) -> None:
    prompt = _prompt(client, T4_CMD, tmp_path, detach=True)
    approved = _approve(client, prompt["id"], decision)
    assert approved["template_promoted"] is False
    assert approved["verb_promoted"] is False
    assert approved["catalog_entry_id"] is None
    assert approved["promoted_tier"] is None
    assert "detach" in (approved["scope_warning"] or "")
    template = classify(T4_CMD, str(tmp_path), AGENT).template
    assert db.get_template_approved_tier(template, AGENT) is None
    assert db.get_verb_approved_tier("kill", str(tmp_path), AGENT) is None

    done = _post(client, T4_CMD, tmp_path, detach=True, approve_token=approved["approve_token"])
    assert done.status_code == 200
    assert len(calls) == 1
    assert _post(client, T4_CMD, tmp_path).json()["decision"] == "prompt_required"


def test_reusable_approval_on_non_detach_prompt_still_promotes(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    prompt = _prompt(client, T4_CMD, tmp_path)
    assert _approve(client, prompt["id"], "approve_template")["template_promoted"] is True


# --- real dispatch with a fake backend ---------------------------------------


def test_detach_dispatch_spawns_unit_and_ignores_timeout(
    client: TestClient, db: Persistence, backend: FakeUnitBackend, tmp_path: Path
) -> None:
    resp = _post(client, "echo hi", tmp_path, detach=True, timeout_s=5)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decision"] == "running"
    assert body["job_id"]
    assert len(backend.started) == 1
    assert f"RuntimeMaxSec={executor.resolve_runtime_max_s()}" in backend.started[0]
    assert not any(a == "RuntimeMaxSec=5" for a in backend.started[0])
    job = db.get_job(body["job_id"])
    assert job is not None
    assert job["kind"] == "unit"
    assert job["timeout_s"] == executor.resolve_runtime_max_s()
    assert job["telemetry_id"] == body["telemetry_id"]


def test_detach_approved_token_dispatches_once(
    client: TestClient, db: Persistence, backend: FakeUnitBackend, tmp_path: Path
) -> None:
    prompt = _prompt(client, T3_CMD, tmp_path, detach=True)
    token = _approve(client, prompt["id"])["approve_token"]
    resp = _post(client, T3_CMD, tmp_path, detach=True, approve_token=token)
    assert resp.json()["decision"] == "running"
    assert len(backend.started) == 1
