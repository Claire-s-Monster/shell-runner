"""FastAPI HTTP service for shell-runner.

Routes:
    POST /execute         — classify + execute or create pending prompt
    POST /classify        — dry-run classification only
    POST /approve_pending — approve or deny a pending prompt
    GET  /health          — service health stats
    *    /mcp             — FastMCP streamable-HTTP transport (MCP protocol)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Literal, cast

from fastapi import FastAPI, HTTPException

from .catalog import Tier, all_rules
from .classifier import classify
from .executor import execute
from .mcp_wrapper import mcp
from .models import (
    ApproveRequest,
    ApproveResponse,
    ClassifyRequest,
    ClassifyResponse,
    ExecutePromptInfo,
    ExecuteRequest,
    ExecuteResponse,
    HealthResponse,
)
from .persistence import DEFAULT_DB_PATH, Persistence

logger = logging.getLogger(__name__)

_db_path = os.environ.get("SHELL_RUNNER_DB", str(DEFAULT_DB_PATH))
db = Persistence(db_path=_db_path)

_cleanup_task: asyncio.Task[None] | None = None


async def _periodic_cleanup(interval_s: int = 60) -> None:
    """Delete expired pending prompts every *interval_s* seconds."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            removed = db.cleanup_expired_prompts()
            if removed:
                logger.debug("Cleaned up %d expired pending prompt(s)", removed)
        except Exception:
            logger.exception("Error during periodic prompt cleanup")


@asynccontextmanager
async def _lifespan(_application: FastAPI) -> AsyncGenerator[None, None]:
    global _cleanup_task
    _cleanup_task = asyncio.create_task(_periodic_cleanup())
    try:
        yield
    finally:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
        _cleanup_task = None


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


@app.post("/approve_pending", response_model=ApproveResponse)
async def approve_route(req: ApproveRequest) -> ApproveResponse:
    token = db.approve_prompt(prompt_id=req.prompt_id, decision=req.decision)
    return ApproveResponse(
        applied=True,  # approve_prompt always applies (approve or deny)
        approve_token=token,
        template_promoted=False,  # Phase 2
        catalog_entry_id=None,  # Phase 2
    )


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
# MCP HTTP transport — mounted after all REST routes
# ---------------------------------------------------------------------------
app.mount("/mcp", mcp.http_app())
