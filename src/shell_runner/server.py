"""FastAPI HTTP service for shell-runner.

Routes:
    POST /execute         — classify + execute or create pending prompt
    POST /classify        — dry-run classification only
    POST /observe         — record an externally-executed command for T3 promotion review
    POST /approve_pending — approve or deny a pending prompt
    POST /pending         — read-only inspection of pending prompts
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
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import executor
from .catalog import Tier, all_rules
from .classifier import (
    PERMISSIVENESS,
    ClassificationResult,
    _cap_permissiveness,
    classify,
    is_primary_identity,
)
from .executor import OUTPUT_TAIL_BYTES, execute, execute_background, validate_cwd
from .mcp_wrapper import TOOLS
from .models import (
    ApproveRequest,
    ApproveResponse,
    ClassifyRequest,
    ClassifyResponse,
    ExecutePromptInfo,
    ExecuteRequest,
    ExecuteResponse,
    ExecuteSuggestion,
    GetPendingRequest,
    GetPendingResponse,
    HealthResponse,
    ObserveRequest,
    ObserveResponse,
    PendingPromptDetail,
    ShellKillRequest,
    ShellKillResponse,
    ShellStatusRequest,
    ShellStatusResponse,
)
from .normalizer import describe_template_scope
from .persistence import DEFAULT_DB_PATH, CatalogWriter, Persistence, TelemetryWriter
from .seeds import DEFAULT_SEED_APPROVALS

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


def _apply_template_approval(
    cls: ClassificationResult, agent_id: str, cwd: str
) -> ClassificationResult:
    """Override classification tier via persisted approvals (template or verb+cwd).

    PRECEDENCE (issue #26 P2 Part C) — evaluated in this exact order:
      1. DENY (T0) always wins. If cls.command_tier is DENY, no promotion of
         any kind may ever be applied — this is a security invariant enforced
         unconditionally below, before any DB lookup. Neither exact-template
         nor verb+cwd promotion may ever elevate a denied command.
      2. Exact-template promotion (db.get_template_approved_tier) is checked
         first. Global approvals take precedence over agent-specific ones
         (handled inside get_template_approved_tier).
      3. Verb + cwd-prefix promotion (db.get_verb_approved_tier) is checked
         ONLY when the exact-template lookup misses. This collapses
         command-variant re-escalation: once a verb is approved for a cwd
         subtree, later argument variants of that verb no longer re-prompt
         just because their exact normalized template differs from the one
         that was originally approved.
      4. Catalog rule (the base classification cls already carries) applies
         when neither promotion hits.
      5. Whichever promotion applies (if any), the agent_cap PERMISSIVENESS
         cap-min formula (same as classify()'s step 6 / _cap_permissiveness)
         is re-applied so a promotion can never exceed what the calling agent
         is trusted for — this is why an AUTO_CAPPED agent can still only get
         APPROVE_ONCE for a command whose promoted tier exceeds its cap.

    The override only applies when the approved tier is strictly more
    permissive than the classified command_tier.
    """
    # 1. DENY always wins — never consult or apply any promotion.
    if cls.command_tier == Tier.DENY:
        return cls

    # 2. Exact-template promotion first.
    approved_int = db.get_template_approved_tier(cls.template, agent_id)
    promotion_label = "approved_template"

    # 3. Verb + cwd-prefix promotion only when exact-template misses.
    if approved_int is None:
        verb = cls.segments[0].segment.verb if cls.segments else ""
        if verb:
            approved_int = db.get_verb_approved_tier(verb, cwd, agent_id)
            promotion_label = "approved_verb"

    if approved_int is None:
        return cls

    approved_tier = Tier(approved_int)
    # Only override if approved tier is more permissive (higher PERMISSIVENESS value)
    if PERMISSIVENESS.get(approved_tier, 0) <= PERMISSIVENESS.get(cls.command_tier, 0):
        return cls

    # 5. Re-apply agent cap with the new command_tier (same formula as classifier.py)
    new_cmd_perm = PERMISSIVENESS[approved_tier]
    cap_perm = _cap_permissiveness(cls.agent_cap)
    new_final_tier = approved_tier if new_cmd_perm <= cap_perm else cls.agent_cap

    new_path = list(cls.decision_path) + [
        f"{promotion_label}_T{approved_int}: {cls.command_tier.name} -> {approved_tier.name}"
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
async def _lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    global _cleanup_task
    _cleanup_task = asyncio.create_task(_periodic_cleanup())

    # Register SIGHUP handler for live cwd-roots reload (Linux only).
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGHUP, _on_sighup)
    except (NotImplementedError, AttributeError):
        logger.debug("SIGHUP not supported on this platform; skipping signal handler")

    if os.environ.get("SHELL_RUNNER_LOAD_SEED_APPROVALS", "1") != "0":
        n = db.seed_approvals(DEFAULT_SEED_APPROVALS)
        if n > 0:
            logger.info("Seeded %d approval template(s) into template_approvals", n)

    telemetry_writer = TelemetryWriter(db)
    await telemetry_writer.start()
    application.state.telemetry_writer = telemetry_writer

    catalog_writer = CatalogWriter(db)
    await catalog_writer.start()
    application.state.catalog_writer = catalog_writer

    try:
        yield
    finally:
        await telemetry_writer.stop()
        await catalog_writer.stop()

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


async def _execute_approved_command(
    req: ExecuteRequest,
    prompt: dict,
    telemetry_writer: TelemetryWriter | None,
    catalog_writer: CatalogWriter | None,
    t0: float,
    consumed_via: str,
) -> ExecuteResponse:
    """Execute a command whose approval has already been validated & consumed.

    Shared by execute_route's Path A (approve_token) and the token-less
    approve_once durability path (issue #26 P2 Part B): both have already
    atomically consumed a pending_prompts row via the DB and just need to run
    the command and record telemetry identically.

    Security hardening (issue #26 P2): an approval may have been consumed up
    to APPROVE_ONCE_DURABLE_TTL_S seconds after the prompt was created. If the
    catalog has since been updated to hard-DENY this command, the stale tier
    captured at prompt-creation must not be trusted -- re-classify against the
    CURRENT catalog and refuse execution on a hard DENY.
    """
    recheck_cls = classify(req.command, req.cwd, req.agent_id)
    if recheck_cls.command_tier == Tier.DENY:
        duration_ms = int((time.monotonic() - t0) * 1000)
        telemetry_id = str(uuid.uuid4())
        deny_reason = (
            f"DENIED by {recheck_cls.matched_rule.pattern!r}: {recheck_cls.matched_rule.reason}"
            if recheck_cls.matched_rule
            else "DENIED: agent has DENY cap"
        )
        if telemetry_writer is not None:
            await telemetry_writer.submit(
                call_id=telemetry_id,
                agent_id=req.agent_id,
                cwd=req.cwd,
                raw_cmd=req.command,
                normalized_template=recheck_cls.template,
                command_tier=int(recheck_cls.command_tier),
                final_tier=int(recheck_cls.tier),
                decision="denied",
                matched_rule_pattern=(
                    recheck_cls.matched_rule.pattern if recheck_cls.matched_rule else None
                ),
                matched_rule_category=(
                    recheck_cls.matched_rule.category if recheck_cls.matched_rule else None
                ),
                exit_code=None,
                stdout_bytes=0,
                stderr_bytes=0,
                duration_ms=duration_ms,
                decision_path=[consumed_via, "execution_recheck_deny"],
                normalizer_warnings=list(recheck_cls.normalizer_warnings),
            )
        else:
            db.record_call(
                call_id=telemetry_id,
                agent_id=req.agent_id,
                cwd=req.cwd,
                raw_cmd=req.command,
                normalized_template=recheck_cls.template,
                command_tier=int(recheck_cls.command_tier),
                final_tier=int(recheck_cls.tier),
                decision="denied",
                matched_rule_pattern=(
                    recheck_cls.matched_rule.pattern if recheck_cls.matched_rule else None
                ),
                matched_rule_category=(
                    recheck_cls.matched_rule.category if recheck_cls.matched_rule else None
                ),
                exit_code=None,
                stdout_bytes=0,
                stderr_bytes=0,
                duration_ms=duration_ms,
                decision_path=[consumed_via, "execution_recheck_deny"],
                normalizer_warnings=list(recheck_cls.normalizer_warnings),
            )
        if catalog_writer is not None:
            await catalog_writer.submit(
                template=recheck_cls.template,
                agent_id=req.agent_id,
                current_tier=int(recheck_cls.tier),
                was_denied=True,
            )
        else:
            await asyncio.to_thread(
                db.upsert_template,
                template=recheck_cls.template,
                agent_id=req.agent_id,
                current_tier=int(recheck_cls.tier),
                was_denied=True,
            )
        return ExecuteResponse(
            decision="denied",
            tier=int(recheck_cls.tier),
            matched_rule=recheck_cls.matched_rule.pattern if recheck_cls.matched_rule else None,
            exit_code=None,
            stdout="",
            stderr=deny_reason,
            stdout_full_path=None,
            stderr_full_path=None,
            duration_ms=duration_ms,
            telemetry_id=telemetry_id,
            suggestions=_compute_suggestions(recheck_cls.template, req.agent_id),
        )

    if req.run_in_background or req.output_mode == "file":
        telemetry_id = str(uuid.uuid4())
        if telemetry_writer is not None:
            await telemetry_writer.submit(
                call_id=telemetry_id,
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
                decision_path=[consumed_via, "background"],
                normalizer_warnings=[],
                wait=True,
            )
        else:
            db.record_call(
                call_id=telemetry_id,
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
                decision_path=[consumed_via, "background"],
                normalizer_warnings=[],
            )
        if catalog_writer is not None:
            await catalog_writer.submit(
                template=prompt["normalized_template"],
                agent_id=req.agent_id,
                current_tier=prompt["command_tier"],
                was_denied=False,
            )
        else:
            await asyncio.to_thread(
                db.upsert_template,
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
    exec_result = await asyncio.to_thread(
        execute, command=req.command, cwd=req.cwd, timeout_s=req.timeout_s
    )
    duration_ms = int((time.monotonic() - t0) * 1000)
    telemetry_id = str(uuid.uuid4())
    if telemetry_writer is not None:
        await telemetry_writer.submit(
            call_id=telemetry_id,
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
            decision_path=[consumed_via],
            normalizer_warnings=[],
        )
    else:
        db.record_call(
            call_id=telemetry_id,
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
            decision_path=[consumed_via],
            normalizer_warnings=[],
        )
    if catalog_writer is not None:
        await catalog_writer.submit(
            template=prompt["normalized_template"],
            agent_id=req.agent_id,
            current_tier=prompt["command_tier"],
            was_denied=False,
        )
    else:
        await asyncio.to_thread(
            db.upsert_template,
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


def _compute_suggestions(template: str, agent_id: str) -> list[ExecuteSuggestion] | None:
    min_sim_env = os.environ.get("SHELL_RUNNER_SUGGESTION_MIN_SIMILARITY")
    try:
        min_similarity = float(min_sim_env) if min_sim_env is not None else 0.5
    except ValueError:
        min_similarity = 0.5
    rows = db.find_similar_approved_templates(template, agent_id, min_similarity=min_similarity)
    if not rows:
        return None
    return [
        ExecuteSuggestion(
            template=t,
            tier=tier,
            category=cat or "unknown",
            example=ex,
        )
        for (t, tier, cat, ex) in rows
    ]


@app.post("/execute", response_model=ExecuteResponse)
async def execute_route(request: Request, req: ExecuteRequest) -> ExecuteResponse:
    t0 = time.monotonic()
    telemetry_writer = getattr(request.app.state, "telemetry_writer", None)
    catalog_writer = getattr(request.app.state, "catalog_writer", None)

    # Path A: approve_token provided — skip classification, validate token
    if req.approve_token:
        # Peek (non-consuming) and validate the token's binding BEFORE
        # consuming it: consuming first meant a caller who retried with a
        # corrected cwd had already burned the approval (issue #36,
        # secondary observation 1).
        peek = db.peek_approve_token(token=req.approve_token)
        if peek is None:
            raise HTTPException(
                status_code=403,
                detail=(
                    "invalid or expired approve_token (tokens are single-use, are bound "
                    "to the exact command/cwd/agent of the original prompt, and expire; "
                    "request a fresh prompt)"
                ),
            )
        mismatches: list[str] = []
        if peek["raw_cmd"] != req.command:
            mismatches.append(
                f"command is bound to {peek['raw_cmd'][:200]!r} but request used "
                f"{req.command[:200]!r}"
            )
        if peek["cwd"] != req.cwd:
            mismatches.append(f"cwd is bound to '{peek['cwd']}' but request used '{req.cwd}'")
        if peek["agent_id"] != req.agent_id:
            mismatches.append(
                f"agent_id is bound to '{peek['agent_id']}' but request used '{req.agent_id}'"
            )
        if mismatches:
            raise HTTPException(
                status_code=403,
                detail="approve_token does not match: " + "; ".join(mismatches),
            )
        prompt = db.consume_approve_token(token=req.approve_token)
        if prompt is None:
            raise HTTPException(
                status_code=403,
                detail="approve_token was already consumed or expired",
            )
        return await _execute_approved_command(
            req, prompt, telemetry_writer, catalog_writer, t0, "approve_token consumed"
        )

    # Path B: no token — classify first
    cls = classify(req.command, req.cwd, req.agent_id)
    cls = _apply_template_approval(cls, req.agent_id, req.cwd)

    # DENY
    if cls.tier == Tier.DENY:
        duration_ms = int((time.monotonic() - t0) * 1000)
        telemetry_id = str(uuid.uuid4())
        if telemetry_writer is not None:
            await telemetry_writer.submit(
                call_id=telemetry_id,
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
        else:
            db.record_call(
                call_id=telemetry_id,
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
        if catalog_writer is not None:
            await catalog_writer.submit(
                template=cls.template,
                agent_id=req.agent_id,
                current_tier=int(cls.tier),
                was_denied=True,
            )
        else:
            await asyncio.to_thread(
                db.upsert_template,
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
            suggestions=_compute_suggestions(cls.template, req.agent_id),
        )

    # T1/T2 — auto-execute
    if cls.tier in (Tier.AUTO_LOG, Tier.AUTO_CAPPED):
        if req.run_in_background or req.output_mode == "file":
            telemetry_id = str(uuid.uuid4())
            if telemetry_writer is not None:
                await telemetry_writer.submit(
                    call_id=telemetry_id,
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
                    wait=True,
                )
            else:
                db.record_call(
                    call_id=telemetry_id,
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
            if catalog_writer is not None:
                await catalog_writer.submit(
                    template=cls.template,
                    agent_id=req.agent_id,
                    current_tier=int(cls.tier),
                    was_denied=False,
                )
            else:
                await asyncio.to_thread(
                    db.upsert_template,
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
        exec_result = await asyncio.to_thread(
            execute, command=req.command, cwd=req.cwd, timeout_s=req.timeout_s
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
        telemetry_id = str(uuid.uuid4())
        if telemetry_writer is not None:
            await telemetry_writer.submit(
                call_id=telemetry_id,
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
        else:
            db.record_call(
                call_id=telemetry_id,
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
        if catalog_writer is not None:
            await catalog_writer.submit(
                template=cls.template,
                agent_id=req.agent_id,
                current_tier=int(cls.tier),
                was_denied=False,
            )
        else:
            await asyncio.to_thread(
                db.upsert_template,
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

    # Durability (issue #26 P2 Part B): a prior approve_once approval survives
    # the subagent that generated the original prompt (extended TTL — see
    # Persistence.APPROVE_ONCE_DURABLE_TTL_S). A token-less re-submit matching
    # (raw_cmd, cwd, agent_id) consumes it once instead of re-prompting.
    # Single-use + TTL-bounded + exact (cmd, cwd, agent) match = same trust
    # boundary as consuming an approve_token, just without requiring the token.
    consumed = db.consume_approval_by_command(
        raw_cmd=req.command, cwd=req.cwd, agent_id=req.agent_id
    )
    if consumed is not None:
        return await _execute_approved_command(
            req,
            consumed,
            telemetry_writer,
            catalog_writer,
            t0,
            "approve_once consumed (token-less)",
        )

    # T3/T4 — reject an unusable cwd before creating a pending prompt: this
    # prevents an approval from being spent on a command whose cwd can never
    # be honoured (issue #36, secondary observation 2).
    cwd_error = validate_cwd(req.cwd)
    if cwd_error is not None:
        duration_ms = int((time.monotonic() - t0) * 1000)
        telemetry_id = str(uuid.uuid4())
        if telemetry_writer is not None:
            await telemetry_writer.submit(
                call_id=telemetry_id,
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
                decision_path=list(cls.decision_path) + ["invalid_cwd"],
                normalizer_warnings=list(cls.normalizer_warnings),
            )
        else:
            db.record_call(
                call_id=telemetry_id,
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
                decision_path=list(cls.decision_path) + ["invalid_cwd"],
                normalizer_warnings=list(cls.normalizer_warnings),
            )
        return ExecuteResponse(
            decision="denied",
            tier=int(cls.tier),
            matched_rule=cls.matched_rule.pattern if cls.matched_rule else None,
            exit_code=None,
            stdout="",
            stderr=cwd_error,
            stdout_full_path=None,
            stderr_full_path=None,
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
    telemetry_id = str(uuid.uuid4())
    if telemetry_writer is not None:
        await telemetry_writer.submit(
            call_id=telemetry_id,
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
    else:
        db.record_call(
            call_id=telemetry_id,
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
        suggestions=_compute_suggestions(cls.template, req.agent_id),
    )


@app.post("/classify", response_model=ClassifyResponse)
async def classify_route(req: ClassifyRequest) -> ClassifyResponse:
    cls = classify(req.command, req.cwd, req.agent_id)
    cls = _apply_template_approval(cls, req.agent_id, req.cwd)
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
async def observe_route(request: Request, req: ObserveRequest) -> ObserveResponse:
    cls = classify(req.command, req.cwd, req.agent_id)
    telemetry_id = str(uuid.uuid4())
    telemetry_writer = getattr(request.app.state, "telemetry_writer", None)
    catalog_writer = getattr(request.app.state, "catalog_writer", None)
    if telemetry_writer is not None:
        await telemetry_writer.submit(
            call_id=telemetry_id,          # ← fixes UUID mismatch
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
    if catalog_writer is not None:
        await catalog_writer.submit(
            template=cls.template,
            agent_id=req.agent_id,
            current_tier=int(cls.command_tier),
            was_denied=False,
        )
    else:
        await asyncio.to_thread(
            db.upsert_template,
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


def _validate_promotion_tier(promote_to_tier: int | None, original_tier: int) -> int:
    """Resolve and validate a promotion tier against the prompt's original command_tier.

    Defaults to T2 (AUTO_CAPPED) when not supplied. Raises HTTPException(400) if
    the tier is not a real promotion (not strictly more permissive), is DENY, or
    exceeds the AUTO_CAPPED permissiveness floor (issue #26 P2 hardening: no
    promotion -- verb or template -- may reach AUTO_LOG, the most permissive tier).
    Shared by the approve_template(_global) and approve_verb branches below.
    """
    promoted_tier = promote_to_tier if promote_to_tier is not None else int(Tier.AUTO_CAPPED)
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
    if PERMISSIVENESS[Tier(promoted_tier)] > PERMISSIVENESS[Tier.AUTO_CAPPED]:
        raise HTTPException(
            status_code=400,
            detail=(
                f"promote_to_tier={promoted_tier} is too permissive: "
                "promotion may not exceed AUTO_CAPPED (tier 2)"
            ),
        )
    return promoted_tier


def _prompt_detail(row: dict) -> PendingPromptDetail:
    """Build a PendingPromptDetail from a pending_prompts row dict, so an
    approver can see what they are deciding on before and after approving
    (issue #37).

    Uses `row.get` for the optional columns because not every query against
    pending_prompts selects every column (e.g. get_pending_prompt does not
    select created_at, but list_pending_prompts does).
    """
    return PendingPromptDetail(
        id=row["id"],
        agent_id=row["agent_id"],
        cwd=row["cwd"],
        raw_cmd=row["raw_cmd"],
        normalized_template=row["normalized_template"],
        command_tier=row["command_tier"],
        matched_rule_category=row.get("matched_rule_category"),
        created_at=row.get("created_at"),
        expires_at=row.get("expires_at"),
        approve_decision=row.get("approve_decision"),
        approved_at=row.get("approved_at"),
        consumed_at=row.get("consumed_at"),
        scope_warning=describe_template_scope(row["normalized_template"]),
    )


@app.post("/approve_pending", response_model=ApproveResponse)
async def approve_route(req: ApproveRequest) -> ApproveResponse:
    # Fetch the pending prompt (if any) before applying the decision, so the
    # approver-identity guard below can inspect the executing agent's id.
    prompt_for_guard = db.get_pending_prompt(req.prompt_id)
    if prompt_for_guard is None and req.decision != "deny":
        raise HTTPException(status_code=404, detail=f"prompt {req.prompt_id} not found")

    # --- Layer-1 defense-in-depth approver guard (issue #29) ---------------
    # NOTE: agent_id / approver_agent_id are self-asserted by the caller, not
    # cryptographically authenticated. This is a MITIGATION, not a hard
    # security boundary — it raises the bar against accidental or careless
    # self-approval, but a malicious caller can still lie about its identity.
    # Full authenticated-identity enforcement is tracked in issue #29.
    #
    # approver_agent_id is now REQUIRED (issue #36): omitting it used to only
    # log a warning and skip the self-approval and capability checks below
    # entirely, making the omitted path strictly more permissive than
    # supplying one.
    if not req.approver_agent_id:
        raise HTTPException(
            status_code=403,
            detail=(
                "approver_agent_id is required: supply the primary session's identity "
                "(e.g. 'primary' or 'primary-session-<id>'). Approving without an "
                "approver identity is no longer permitted — the omitted path previously "
                "skipped the self-approval and capability checks entirely, making it "
                "strictly more permissive than supplying one (issue #36)."
            ),
        )

    executor_agent_id = prompt_for_guard["agent_id"] if prompt_for_guard else None
    if req.approver_agent_id == executor_agent_id:
        raise HTTPException(
            status_code=403,
            detail="self-approval not allowed: approver must differ from the executing agent",
        )
    # Defense in depth: unreachable in normal operation because primary
    # identities now carry a DENY execute cap and cannot create prompts at
    # all — it exists so the guard does not depend on that invariant holding.
    if is_primary_identity(req.approver_agent_id) and is_primary_identity(executor_agent_id):
        raise HTTPException(
            status_code=403,
            detail=(
                "self-approval not allowed: a primary identity may not approve a prompt "
                "created by another primary identity"
            ),
        )
    if not is_primary_identity(req.approver_agent_id):
        raise HTTPException(
            status_code=403,
            detail=(
                "approver lacks approval capability "
                "(a DENY-capability/primary identity is required)"
            ),
        )

    token = db.approve_prompt(prompt_id=req.prompt_id, decision=req.decision)
    if token is None and req.decision != "deny":
        raise HTTPException(status_code=404, detail=f"prompt {req.prompt_id} not found")

    template_promoted = False
    verb_promoted = False
    catalog_entry_id: str | None = None
    promoted_tier_out: int | None = None

    if req.decision in ("approve_template", "approve_template_global"):
        prompt = db.get_pending_prompt(req.prompt_id)
        if prompt is None:
            raise HTTPException(status_code=404, detail=f"prompt {req.prompt_id} not found")

        promoted_tier = _validate_promotion_tier(req.promote_to_tier, int(prompt["command_tier"]))

        agent_scope = None if req.decision == "approve_template_global" else prompt["agent_id"]
        entry_id = db.create_template_approval(
            template=prompt["normalized_template"],
            agent_id=agent_scope,
            approved_tier=promoted_tier,
            approved_via_prompt_id=req.prompt_id,
        )
        template_promoted = True
        promoted_tier_out = promoted_tier
        catalog_entry_id = str(entry_id)

    elif req.decision == "approve_verb":
        prompt = db.get_pending_prompt(req.prompt_id)
        if prompt is None:
            raise HTTPException(status_code=404, detail=f"prompt {req.prompt_id} not found")

        promoted_tier = _validate_promotion_tier(req.promote_to_tier, int(prompt["command_tier"]))

        # Reuse the existing normalizer/classify path to extract the verb of
        # the pending prompt's raw command (first pipeline segment's verb).
        prompt_cls = classify(prompt["raw_cmd"], prompt["cwd"], prompt["agent_id"])
        verb = prompt_cls.segments[0].segment.verb if prompt_cls.segments else ""
        cwd_prefix = os.path.realpath(prompt["cwd"])
        entry_id = db.create_verb_approval(
            verb=verb,
            cwd_prefix=cwd_prefix,
            agent_id=prompt["agent_id"],
            approved_tier=promoted_tier,
            approved_via_prompt_id=req.prompt_id,
        )
        verb_promoted = True
        promoted_tier_out = promoted_tier
        catalog_entry_id = str(entry_id)

    # issue #37 — echo back the post-approval state of the prompt so the
    # caller can verify what was actually acted on.
    row = db.get_pending_prompt(req.prompt_id)
    detail = _prompt_detail(row) if row is not None else None

    return ApproveResponse(
        applied=True,
        approve_token=token,
        template_promoted=template_promoted,
        catalog_entry_id=catalog_entry_id,
        verb_promoted=verb_promoted,
        promoted_tier=promoted_tier_out,
        approved=detail,
        scope_warning=detail.scope_warning if detail is not None else None,
    )


@app.post("/pending", response_model=GetPendingResponse)
async def get_pending_route(body: GetPendingRequest) -> GetPendingResponse:
    """Inspect pending prompts without mutating them.

    Exists so the approver is not deciding blind (issue #37): it lets a
    caller see the full detail of a specific pending prompt, or list all
    outstanding (or resolved) ones, before calling /approve_pending.
    """
    if body.prompt_id is not None:
        row = db.get_pending_prompt(body.prompt_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"prompt {body.prompt_id} not found")
        return GetPendingResponse(prompts=[_prompt_detail(row)])
    rows = db.list_pending_prompts(include_resolved=body.include_resolved)
    return GetPendingResponse(prompts=[_prompt_detail(r) for r in rows])


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
        result = await _dispatch_tool_call(request, params)
        return JSONResponse(
            content={
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(result)}]},
            }
        )

    if method in ("notifications/initialized", "ping"):
        return JSONResponse(content={"jsonrpc": "2.0", "id": req_id, "result": {}})

    if method == "resources/list":
        return JSONResponse(content={"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}})

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
        return JSONResponse(content={"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}})

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


async def _dispatch_tool_call(request: Request, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a tools/call request to the appropriate internal handler."""
    tool_name = params.get("name")
    arguments = params.get("arguments", {})

    if tool_name == "shell_execute":
        try:
            exec_req = ExecuteRequest(**arguments)
        except Exception:
            logger.exception("Invalid arguments for shell_execute")
            return {"error": "invalid arguments"}
        try:
            return (await execute_route(request, exec_req)).model_dump()
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("shell_execute failed")
            return {"error": f"execution failed: {exc}"}

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

    if tool_name == "shell_get_pending":
        try:
            pending_req = GetPendingRequest(**arguments)
        except Exception:
            logger.exception("Invalid arguments for shell_get_pending")
            return {"error": "invalid arguments"}
        return (await get_pending_route(pending_req)).model_dump()

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
