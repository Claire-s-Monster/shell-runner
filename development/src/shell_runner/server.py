"""FastAPI HTTP service for shell-runner.

Routes:
    POST /execute         — classify + execute or create pending prompt
    POST /classify        — dry-run classification only
    POST /observe         — record an externally-executed command for T3 promotion review
    POST /approve_pending — approve or deny a pending prompt
    GET  /health          — service health stats
    POST /mcp             — MCP JSON-RPC 2.0 endpoint (Claude Code "type": "http" transport)
    GET  /mcp             — SSE keepalive stream for MCP server-push notifications
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import executor
from .catalog import Tier, all_rules
from .classifier import PERMISSIVENESS, ClassificationResult, _cap_permissiveness, classify
from .executor import OUTPUT_TAIL_BYTES, execute, execute_background
from .mcp_wrapper import TOOLS
from .models import (
    ApproveRequest,
    ApproveResponse,
    ClassifyRequest,
    ClassifyResponse,
    ExecutePromptInfo,
    ExecuteRequest,
    ExecuteResponse,
    HealthResponse,
    ObserveRequest,
    ObserveResponse,
    ShellKillRequest,
    ShellKillResponse,
    ShellStatusRequest,
    ShellStatusResponse,
)
from .persistence import DEFAULT_DB_PATH, Persistence

logger = logging.getLogger(__name__)

_db_path = os.environ.get("SHELL_RUNNER_DB", str(DEFAULT_DB_PATH))
db = Persistence(db_path=_db_path)

_cleanup_task: asyncio.Task[None] | None = None

RETENTION_S = int(os.environ.get("SHELL_RUNNER_JOB_RETENTION_S", str(7 * 24 * 3600)))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _tail(path: str | None, max_bytes: int) -> str:
    """Read the last *max_bytes* bytes of a file, decoded as UTF-8."""
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    size = p.stat().st_size
    with p.open("rb") as fh:
        if size > max_bytes:
            fh.seek(-max_bytes, 2)
        raw = fh.read()
    return raw.decode("utf-8", errors="replace")


def _compute_duration_ms(job: dict) -> int | None:
    if job.get("finished_at") is None:
        return None
    try:
        start = datetime.fromisoformat(job["started_at"])
        end = datetime.fromisoformat(job["finished_at"])
        return int((end - start).total_seconds() * 1000)
    except (ValueError, TypeError):
        return None


def _apply_template_approval(cls: ClassificationResult, agent_id: str) -> ClassificationResult:
    """Override classification tier if a persisted template approval exists.

    Global approvals take precedence over agent-specific ones (handled by
    get_template_approved_tier). The override only applies when the approved
    tier is strictly more permissive than the classified command_tier.
    The agent cap is re-applied after the override so the cap still constrains.
    """
    approved_int = db.get_template_approved_tier(cls.template, agent_id)
    if approved_int is None:
        return cls

    approved_tier = Tier(approved_int)
    # Only override if approved tier is more permissive (higher PERMISSIVENESS value)
    if PERMISSIVENESS.get(approved_tier, 0) <= PERMISSIVENESS.get(cls.command_tier, 0):
        return cls

    # Re-apply agent cap with the new command_tier (same formula as classifier.py)
    new_cmd_perm = PERMISSIVENESS[approved_tier]
    cap_perm = _cap_permissiveness(cls.agent_cap)
    new_final_tier = approved_tier if new_cmd_perm <= cap_perm else cls.agent_cap

    new_path = list(cls.decision_path) + [
        f"approved_template_T{approved_int}: {cls.command_tier.name} -> {approved_tier.name}"
    ]
    if new_final_tier != approved_tier:
        new_path.append(f"agent cap applied: {approved_tier.name} -> {new_final_tier.name}")

    return ClassificationResult(
        tier=new_final_tier,
        command_tier=approved_tier,
        agent_cap=cls.agent_cap,
        matched_rule=cls.matched_rule,
        template=cls.template,
        segments=cls.segments,
        normalizer_warnings=cls.normalizer_warnings,
        decision_path=tuple(new_path),
    )


async def _periodic_cleanup(interval_s: int = 60) -> None:
    """Delete expired pending prompts and stale jobs every *interval_s* seconds."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            removed_prompts = db.cleanup_expired_prompts()
            if removed_prompts:
                logger.debug("Cleaned up %d expired pending prompt(s)", removed_prompts)
        except Exception:
            logger.exception("Error during periodic prompt cleanup")
        try:
            removed_jobs = db.cleanup_old_jobs(RETENTION_S)
            for job in removed_jobs:
                for file_path in (job.get("stdout_path"), job.get("stderr_path")):
                    if file_path and Path(file_path).exists():
                        try:
                            Path(file_path).unlink()
                        except OSError:
                            pass
                job_dir = Path(job["stdout_path"]).parent if job.get("stdout_path") else None
                if job_dir and job_dir.exists():
                    try:
                        job_dir.rmdir()
                    except OSError:
                        pass
            if removed_jobs:
                logger.debug("Cleaned up %d stale job(s)", len(removed_jobs))
        except Exception:
            logger.exception("Error during periodic job cleanup")


def _on_sighup() -> None:
    """Reload cwd roots on SIGHUP without restarting the server."""
    roots = executor.reload_cwd_roots()
    logger.info("reloaded cwd-roots: %s", [str(r) for r in roots])


@asynccontextmanager
async def _lifespan(_application: FastAPI) -> AsyncGenerator[None, None]:
    global _cleanup_task
    _cleanup_task = asyncio.create_task(_periodic_cleanup())

    # Register SIGHUP handler for live cwd-roots reload (Linux only).
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGHUP, _on_sighup)
    except (NotImplementedError, AttributeError):
        logger.debug("SIGHUP not supported on this platform; skipping signal handler")

    try:
        yield
    finally:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
        _cleanup_task = None

        try:
            loop.remove_signal_handler(signal.SIGHUP)
        except (NotImplementedError, AttributeError):
            pass


app = FastAPI(title="shell-runner", version="0.1.0", lifespan=_lifespan)


@app.post("/execute", response_model=ExecuteResponse)
async def execute_route(req: ExecuteRequest) -> ExecuteResponse:
    t0 = time.monotonic()

    # Path A: approve_token provided — skip classification, validate token
    if req.approve_token:
        prompt = db.consume_approve_token(token=req.approve_token)
        if prompt is None:
            raise HTTPException(status_code=403, detail="invalid or expired approve_token")
        if (
            prompt["raw_cmd"] != req.command
            or prompt["cwd"] != req.cwd
            or prompt["agent_id"] != req.agent_id
        ):
            raise HTTPException(
                status_code=403, detail="approve_token does not match command/cwd/agent"
            )
        if req.run_in_background:
            telemetry_id = db.record_call(
                agent_id=req.agent_id,
                cwd=req.cwd,
                raw_cmd=req.command,
                normalized_template=prompt["normalized_template"],
                command_tier=prompt["command_tier"],
                final_tier=prompt["command_tier"],
                decision="running",
                matched_rule_pattern=None,
                matched_rule_category=prompt["matched_rule_category"],
                exit_code=None,
                stdout_bytes=0,
                stderr_bytes=0,
                duration_ms=int((time.monotonic() - t0) * 1000),
                decision_path=["approve_token consumed", "background"],
                normalizer_warnings=[],
            )
            db.upsert_template(
                template=prompt["normalized_template"],
                agent_id=req.agent_id,
                current_tier=prompt["command_tier"],
                was_denied=False,
            )
            job_id = await execute_background(
                command=req.command,
                cwd=req.cwd,
                timeout_s=req.timeout_s,
                telemetry_id=telemetry_id,
                agent_id=req.agent_id,
                persistence=db,
            )
            return ExecuteResponse(
                decision="running",
                tier=prompt["command_tier"],
                matched_rule=None,
                exit_code=None,
                stdout="",
                stderr="",
                stdout_full_path=None,
                stderr_full_path=None,
                duration_ms=int((time.monotonic() - t0) * 1000),
                telemetry_id=telemetry_id,
                job_id=job_id,
            )
        exec_result = execute(command=req.command, cwd=req.cwd, timeout_s=req.timeout_s)
        duration_ms = int((time.monotonic() - t0) * 1000)
        telemetry_id = db.record_call(
            agent_id=req.agent_id,
            cwd=req.cwd,
            raw_cmd=req.command,
            normalized_template=prompt["normalized_template"],
            command_tier=prompt["command_tier"],
            final_tier=prompt["command_tier"],
            decision="executed",
            matched_rule_pattern=None,
            matched_rule_category=prompt["matched_rule_category"],
            exit_code=exec_result.exit_code,
            stdout_bytes=len(exec_result.stdout),
            stderr_bytes=len(exec_result.stderr),
            duration_ms=duration_ms,
            decision_path=["approve_token consumed"],
            normalizer_warnings=[],
        )
        db.upsert_template(
            template=prompt["normalized_template"],
            agent_id=req.agent_id,
            current_tier=prompt["command_tier"],
            was_denied=False,
        )
        return ExecuteResponse(
            decision="executed",
            tier=prompt["command_tier"],
            matched_rule=None,
            exit_code=exec_result.exit_code,
            stdout=exec_result.stdout,
            stderr=exec_result.stderr,
            stdout_full_path=exec_result.stdout_full_path,
            stderr_full_path=exec_result.stderr_full_path,
            duration_ms=duration_ms,
            telemetry_id=telemetry_id,
        )

    # Path B: no token — classify first
    cls = classify(req.command, req.cwd, req.agent_id)
    cls = _apply_template_approval(cls, req.agent_id)

    # DENY
    if cls.tier == Tier.DENY:
        duration_ms = int((time.monotonic() - t0) * 1000)
        telemetry_id = db.record_call(
            agent_id=req.agent_id,
            cwd=req.cwd,
            raw_cmd=req.command,
            normalized_template=cls.template,
            command_tier=int(cls.command_tier),
            final_tier=int(cls.tier),
            decision="denied",
            matched_rule_pattern=cls.matched_rule.pattern if cls.matched_rule else None,
            matched_rule_category=cls.matched_rule.category if cls.matched_rule else None,
            exit_code=None,
            stdout_bytes=0,
            stderr_bytes=0,
            duration_ms=duration_ms,
            decision_path=list(cls.decision_path),
            normalizer_warnings=list(cls.normalizer_warnings),
        )
        db.upsert_template(
            template=cls.template,
            agent_id=req.agent_id,
            current_tier=int(cls.tier),
            was_denied=True,
        )
        deny_reason = (
            f"DENIED by {cls.matched_rule.pattern!r}: {cls.matched_rule.reason}"
            if cls.matched_rule
            else "DENIED: agent has DENY cap"
        )
        return ExecuteResponse(
            decision="denied",
            tier=int(cls.tier),
            matched_rule=cls.matched_rule.pattern if cls.matched_rule else None,
            exit_code=None,
            stdout="",
            stderr=deny_reason,
            stdout_full_path=None,
            stderr_full_path=None,
            duration_ms=duration_ms,
            telemetry_id=telemetry_id,
        )

    # T1/T2 — auto-execute
    if cls.tier in (Tier.AUTO_LOG, Tier.AUTO_CAPPED):
        if req.run_in_background:
            telemetry_id = db.record_call(
                agent_id=req.agent_id,
                cwd=req.cwd,
                raw_cmd=req.command,
                normalized_template=cls.template,
                command_tier=int(cls.command_tier),
                final_tier=int(cls.tier),
                decision="running",
                matched_rule_pattern=cls.matched_rule.pattern if cls.matched_rule else None,
                matched_rule_category=cls.matched_rule.category if cls.matched_rule else None,
                exit_code=None,
                stdout_bytes=0,
                stderr_bytes=0,
                duration_ms=int((time.monotonic() - t0) * 1000),
                decision_path=list(cls.decision_path) + ["background"],
                normalizer_warnings=list(cls.normalizer_warnings),
            )
            db.upsert_template(
                template=cls.template,
                agent_id=req.agent_id,
                current_tier=int(cls.tier),
                was_denied=False,
            )
            job_id = await execute_background(
                command=req.command,
                cwd=req.cwd,
                timeout_s=req.timeout_s,
                telemetry_id=telemetry_id,
                agent_id=req.agent_id,
                persistence=db,
            )
            return ExecuteResponse(
                decision="running",
                tier=int(cls.tier),
                matched_rule=cls.matched_rule.pattern if cls.matched_rule else None,
                exit_code=None,
                stdout="",
                stderr="",
                stdout_full_path=None,
                stderr_full_path=None,
                duration_ms=int((time.monotonic() - t0) * 1000),
                telemetry_id=telemetry_id,
                job_id=job_id,
            )
        exec_result = execute(command=req.command, cwd=req.cwd, timeout_s=req.timeout_s)
        duration_ms = int((time.monotonic() - t0) * 1000)
        telemetry_id = db.record_call(
            agent_id=req.agent_id,
            cwd=req.cwd,
            raw_cmd=req.command,
            normalized_template=cls.template,
            command_tier=int(cls.command_tier),
            final_tier=int(cls.tier),
            decision="executed",
            matched_rule_pattern=cls.matched_rule.pattern if cls.matched_rule else None,
            matched_rule_category=cls.matched_rule.category if cls.matched_rule else None,
            exit_code=exec_result.exit_code,
            stdout_bytes=len(exec_result.stdout),
            stderr_bytes=len(exec_result.stderr),
            duration_ms=duration_ms,
            decision_path=list(cls.decision_path),
            normalizer_warnings=list(cls.normalizer_warnings),
        )
        db.upsert_template(
            template=cls.template,
            agent_id=req.agent_id,
            current_tier=int(cls.tier),
            was_denied=False,
        )
        return ExecuteResponse(
            decision="executed",
            tier=int(cls.tier),
            matched_rule=cls.matched_rule.pattern if cls.matched_rule else None,
            exit_code=exec_result.exit_code,
            stdout=exec_result.stdout,
            stderr=exec_result.stderr,
            stdout_full_path=exec_result.stdout_full_path,
            stderr_full_path=exec_result.stderr_full_path,
            duration_ms=duration_ms,
            telemetry_id=telemetry_id,
        )

    # T3/T4 — create pending prompt, return prompt_required
    prompt_id = db.create_pending_prompt(
        agent_id=req.agent_id,
        cwd=req.cwd,
        raw_cmd=req.command,
        normalized_template=cls.template,
        command_tier=int(cls.command_tier),
        matched_rule_category=cls.matched_rule.category if cls.matched_rule else None,
        decision_path=list(cls.decision_path),
    )
    duration_ms = int((time.monotonic() - t0) * 1000)
    telemetry_id = db.record_call(
        agent_id=req.agent_id,
        cwd=req.cwd,
        raw_cmd=req.command,
        normalized_template=cls.template,
        command_tier=int(cls.command_tier),
        final_tier=int(cls.tier),
        decision="prompt_required",
        matched_rule_pattern=cls.matched_rule.pattern if cls.matched_rule else None,
        matched_rule_category=cls.matched_rule.category if cls.matched_rule else None,
        exit_code=None,
        stdout_bytes=0,
        stderr_bytes=0,
        duration_ms=duration_ms,
        decision_path=list(cls.decision_path),
        normalizer_warnings=list(cls.normalizer_warnings),
    )
    why = (
        f"command tier={cls.command_tier.name} after agent_cap={cls.agent_cap.name}"
        f" → {cls.tier.name}; requires user approval"
    )
    return ExecuteResponse(
        decision="prompt_required",
        tier=int(cls.tier),
        matched_rule=cls.matched_rule.pattern if cls.matched_rule else None,
        exit_code=None,
        stdout="",
        stderr="",
        stdout_full_path=None,
        stderr_full_path=None,
        duration_ms=duration_ms,
        telemetry_id=telemetry_id,
        prompt=ExecutePromptInfo(
            id=prompt_id,
            raw_cmd=req.command,
            template=cls.template,
            category=cls.matched_rule.category if cls.matched_rule else None,
            tier=int(cls.tier),
            why=why,
        ),
    )


@app.post("/classify", response_model=ClassifyResponse)
async def classify_route(req: ClassifyRequest) -> ClassifyResponse:
    cls = classify(req.command, req.cwd, req.agent_id)
    cls = _apply_template_approval(cls, req.agent_id)
    _preview_map: dict[Tier, str] = {
        Tier.DENY: "would_deny",
        Tier.AUTO_LOG: "would_execute",
        Tier.AUTO_CAPPED: "would_execute",
        Tier.APPROVE_ONCE: "would_prompt",
        Tier.ALWAYS_APPROVE: "would_prompt",
    }
    decision_preview = cast(
        Literal["would_execute", "would_deny", "would_prompt", "unknown"],
        _preview_map.get(cls.tier, "unknown"),
    )
    return ClassifyResponse(
        command_tier=int(cls.command_tier),
        agent_cap=int(cls.agent_cap),
        effective_tier=int(cls.tier),
        decision_preview=decision_preview,
        template=cls.template,
        category=cls.matched_rule.category if cls.matched_rule else None,
        matched_rule=cls.matched_rule.pattern if cls.matched_rule else None,
        segments=[
            {"verb": s.segment.verb, "tier": int(s.tier), "step": s.decision_step}
            for s in cls.segments
        ],
    )


@app.post("/observe", response_model=ObserveResponse)
async def observe_route(req: ObserveRequest) -> ObserveResponse:
    cls = classify(req.command, req.cwd, req.agent_id)
    telemetry_id = db.record_call(
        agent_id=req.agent_id,
        cwd=req.cwd,
        raw_cmd=req.command,
        normalized_template=cls.template,
        command_tier=int(cls.command_tier),
        final_tier=int(cls.tier),
        decision="observed_externally",
        matched_rule_pattern=cls.matched_rule.pattern if cls.matched_rule else None,
        matched_rule_category=cls.matched_rule.category if cls.matched_rule else None,
        exit_code=req.exit_code,
        stdout_bytes=0,
        stderr_bytes=0,
        duration_ms=req.duration_ms,
        decision_path=["observe_endpoint"],
        normalizer_warnings=list(cls.normalizer_warnings),
    )
    db.upsert_template(
        template=cls.template,
        agent_id=req.agent_id,
        current_tier=int(cls.command_tier),
        was_denied=False,
    )
    return ObserveResponse(
        telemetry_id=telemetry_id,
        classified_tier=int(cls.command_tier),
        normalized_template=cls.template,
    )


@app.post("/approve_pending", response_model=ApproveResponse)
async def approve_route(req: ApproveRequest) -> ApproveResponse:
    token = db.approve_prompt(prompt_id=req.prompt_id, decision=req.decision)
    if token is None and req.decision != "deny":
        raise HTTPException(status_code=404, detail=f"prompt {req.prompt_id} not found")

    template_promoted = False
    catalog_entry_id: str | None = None

    if req.decision in ("approve_template", "approve_template_global"):
        prompt = db.get_pending_prompt(req.prompt_id)
        if prompt is None:
            raise HTTPException(status_code=404, detail=f"prompt {req.prompt_id} not found")

        # Default to T2 (AUTO_CAPPED) — auto-execute but still logged
        promoted_tier = req.promote_to_tier if req.promote_to_tier is not None else int(Tier.AUTO_CAPPED)

        # Must be a real promotion (more permissive than original)
        original_tier = int(prompt["command_tier"])
        if promoted_tier >= original_tier:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"promote_to_tier={promoted_tier} is not more permissive than "
                    f"original command_tier={original_tier}; promotions must lower the tier"
                ),
            )
        if promoted_tier <= int(Tier.DENY):
            raise HTTPException(
                status_code=400,
                detail=f"promote_to_tier={promoted_tier} (DENY) is invalid for approval",
            )

        agent_scope = None if req.decision == "approve_template_global" else prompt["agent_id"]
        entry_id = db.create_template_approval(
            template=prompt["normalized_template"],
            agent_id=agent_scope,
            approved_tier=promoted_tier,
            approved_via_prompt_id=req.prompt_id,
        )
        template_promoted = True
        catalog_entry_id = str(entry_id)

    return ApproveResponse(
        applied=True,
        approve_token=token,
        template_promoted=template_promoted,
        catalog_entry_id=catalog_entry_id,
    )


@app.post("/tools/shell_status", response_model=ShellStatusResponse)
async def shell_status_route(body: ShellStatusRequest) -> ShellStatusResponse:
    job = db.get_job(body.job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"job {body.job_id} not found")
    return ShellStatusResponse(
        job_id=job["job_id"],
        status=job["status"],
        started_at=job["started_at"],
        finished_at=job["finished_at"],
        exit_code=job["exit_code"],
        duration_ms=_compute_duration_ms(job),
        stdout_tail=_tail(job["stdout_path"], OUTPUT_TAIL_BYTES),
        stderr_tail=_tail(job["stderr_path"], OUTPUT_TAIL_BYTES),
        stdout_full_path=job["stdout_path"],
        stderr_full_path=job["stderr_path"],
    )


@app.post("/tools/shell_kill", response_model=ShellKillResponse)
async def shell_kill_route(body: ShellKillRequest) -> ShellKillResponse:
    job = db.get_job(body.job_id)
    if not job or job["status"] != "running":
        return ShellKillResponse(killed=False, reason="job not running or not found")
    sig = getattr(signal, body.sig, signal.SIGTERM)
    try:
        os.kill(job["pid"], sig)
        return ShellKillResponse(killed=True, sig=body.sig)
    except ProcessLookupError:
        db.update_job_status(body.job_id, status="killed", finished_at=_now_iso())
        return ShellKillResponse(killed=True, reason="process already gone, status updated")


@app.get("/health", response_model=HealthResponse)
async def health_route() -> HealthResponse:
    stats = db.health_stats()
    return HealthResponse(
        status="ok",
        total_calls_24h=stats["total_calls_24h"],
        denied_rate_24h=stats["denied_rate_24h"],
        prompt_rate_24h=stats["prompt_rate_24h"],
        p50_latency_ms=stats.get("p50_latency_ms", 0),
        p99_latency_ms=stats.get("p99_latency_ms", 0),
        catalog_size=len(all_rules()),
        pending_prompts=stats.get("pending_prompts_count", 0),
        last_error=stats.get("last_error"),
    )


# ---------------------------------------------------------------------------
# MCP JSON-RPC 2.0 endpoint — Claude Code "type": "http" transport
# ---------------------------------------------------------------------------

MCP_PROTOCOL_VERSION = "2024-11-05"


@app.post("/mcp")
async def handle_mcp_post(request: Request) -> JSONResponse:
    """Handle MCP JSON-RPC 2.0 requests."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse(
            status_code=400,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            },
        )

    method = body.get("method")
    params = body.get("params", {})
    req_id = body.get("id")

    if method == "initialize":
        response = JSONResponse(
            content={
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                        "prompts": {"listChanged": False},
                    },
                    "serverInfo": {"name": "shell-runner", "version": "0.1.0"},
                },
            }
        )
        response.headers["MCP-Protocol-Version"] = MCP_PROTOCOL_VERSION
        return response

    if method == "tools/list":
        tools_list = [
            {
                "name": name,
                "description": meta["description"],
                "inputSchema": meta["schema"],
            }
            for name, meta in TOOLS.items()
        ]
        return JSONResponse(
            content={"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools_list}}
        )

    if method == "tools/call":
        result = await _dispatch_tool_call(params)
        return JSONResponse(
            content={
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result)}]
                },
            }
        )

    if method in ("notifications/initialized", "ping"):
        return JSONResponse(content={"jsonrpc": "2.0", "id": req_id, "result": {}})

    if method == "resources/list":
        return JSONResponse(
            content={"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}}
        )

    if method == "resources/templates/list":
        return JSONResponse(
            content={"jsonrpc": "2.0", "id": req_id, "result": {"resourceTemplates": []}}
        )

    if method == "resources/read":
        return JSONResponse(
            content={
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": "shell-runner exposes no resources"},
            }
        )

    if method == "prompts/list":
        return JSONResponse(
            content={"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}
        )

    if method == "prompts/get":
        return JSONResponse(
            content={
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": "shell-runner exposes no prompts"},
            }
        )

    return JSONResponse(
        status_code=200,
        content={
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        },
    )


@app.get("/mcp")
async def handle_mcp_sse(request: Request) -> StreamingResponse:
    """SSE endpoint for server-pushed notifications.

    Claude Code's HTTP MCP client opens this stream after `initialize`. We have
    no notifications to push (Phase 1 MVP), so we keep the connection alive with
    periodic comments. Without this route, Claude Code receives 405 and hangs.
    """

    async def _stream() -> AsyncGenerator[bytes, None]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                yield b": keepalive\n\n"
                await asyncio.sleep(15)
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _dispatch_tool_call(params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a tools/call request to the appropriate internal handler."""
    tool_name = params.get("name")
    arguments = params.get("arguments", {})

    if tool_name == "shell_execute":
        try:
            exec_req = ExecuteRequest(**arguments)
        except Exception:
            logger.exception("Invalid arguments for shell_execute")
            return {"error": "invalid arguments"}
        return (await execute_route(exec_req)).model_dump()

    if tool_name == "shell_classify":
        try:
            cls_req = ClassifyRequest(**arguments)
        except Exception:
            logger.exception("Invalid arguments for shell_classify")
            return {"error": "invalid arguments"}
        return (await classify_route(cls_req)).model_dump()

    if tool_name == "shell_approve_pending":
        try:
            approve_req = ApproveRequest(**arguments)
        except Exception:
            logger.exception("Invalid arguments for shell_approve_pending")
            return {"error": "invalid arguments"}
        return (await approve_route(approve_req)).model_dump()

    if tool_name == "shell_health":
        return (await health_route()).model_dump()

    if tool_name == "shell_status":
        try:
            status_req = ShellStatusRequest(**arguments)
        except Exception:
            logger.exception("Invalid arguments for shell_status")
            return {"error": "invalid arguments"}
        return (await shell_status_route(status_req)).model_dump()

    if tool_name == "shell_kill":
        try:
            kill_req = ShellKillRequest(**arguments)
        except Exception:
            logger.exception("Invalid arguments for shell_kill")
            return {"error": "invalid arguments"}
        return (await shell_kill_route(kill_req)).model_dump()

    return {"error": f"unknown tool: {tool_name}", "available": list(TOOLS.keys())}
