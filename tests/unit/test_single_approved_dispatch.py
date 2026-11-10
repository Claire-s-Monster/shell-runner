"""Every post-classification/approval spawn goes through _dispatch_approved.

Both the T1/T2 auto-execute path and the approved (approve_token / approve_once)
path must delegate to the single helper, so a future `detach` branch only needs
adding there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shell_runner import executor
from shell_runner import server as srv
from shell_runner.models import ExecuteResponse
from shell_runner.persistence import Persistence

AGENT = "focused-code-modifier"


@pytest.fixture
def calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    monkeypatch.setattr(srv, "db", Persistence(db_path=tmp_path / "test.sqlite3"))
    monkeypatch.setattr(executor, "_CWD_ROOTS", [tmp_path.resolve()])
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


def test_auto_execute_path_uses_dispatch_helper(
    calls: list[dict[str, Any]], tmp_path: Path
) -> None:
    resp = TestClient(srv.app).post(
        "/execute", json={"command": "echo hi", "cwd": str(tmp_path), "agent_id": AGENT}
    )
    assert resp.status_code == 200, resp.text
    assert len(calls) == 1
    assert calls[0]["kwargs"].get("approval_consumed", False) is False


def test_approve_token_path_uses_dispatch_helper(
    calls: list[dict[str, Any]], tmp_path: Path
) -> None:
    pid = srv.db.create_pending_prompt(
        agent_id=AGENT,
        cwd=str(tmp_path),
        raw_cmd="echo approved",
        normalized_template="echo <word>",
        command_tier=4,
        matched_rule_category=None,
        decision_path=["T4 match"],
    )
    client = TestClient(srv.app)
    approve = client.post(
        "/approve_pending",
        json={"prompt_id": pid, "decision": "approve_once", "approver_agent_id": "primary"},
    )
    assert approve.status_code == 200, approve.text
    resp = client.post(
        "/execute",
        json={
            "command": "echo approved",
            "cwd": str(tmp_path),
            "agent_id": AGENT,
            "approve_token": approve.json()["approve_token"],
        },
    )
    assert resp.status_code == 200, resp.text
    assert len(calls) == 1
    assert calls[0]["kwargs"]["approval_consumed"] is True
