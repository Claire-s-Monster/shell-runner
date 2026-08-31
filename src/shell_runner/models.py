"""Pydantic v2 request/response models for shell-runner HTTP API."""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field

# Maximum subprocess timeout in seconds. Configurable via env to support
# long-running commands (e.g. 30-min builds) while keeping a runaway-guard.
DEFAULT_MAX_TIMEOUT_S = 3600  # 1 hour
MAX_TIMEOUT_S = int(os.environ.get("SHELL_RUNNER_MAX_TIMEOUT_S", str(DEFAULT_MAX_TIMEOUT_S)))


class ExecuteRequest(BaseModel):
    command: str
    cwd: str
    timeout_s: int = Field(
        default=30,
        ge=1,
        le=MAX_TIMEOUT_S,
        description=(
            "Subprocess timeout in seconds. Max is configurable via "
            "SHELL_RUNNER_MAX_TIMEOUT_S (default 3600s = 1h)."
        ),
    )
    agent_id: str
    approve_token: str | None = None
    output_mode: Literal["inline", "file", "summarize"] = "inline"
    run_in_background: bool = Field(
        default=False,
        description=(
            "If True, spawn the subprocess and return immediately with a job_id. "
            "Use shell_status(job_id) to poll and shell_kill(job_id) to terminate. "
            "T3/T4 approval flow still applies before spawn."
        ),
    )


class ExecutePromptInfo(BaseModel):
    id: str
    raw_cmd: str
    template: str
    category: str | None
    tier: int
    why: str


class ExecuteResponse(BaseModel):
    decision: Literal["executed", "denied", "prompt_required", "reformulate_suggested", "running"]
    tier: int
    matched_rule: str | None
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_full_path: str | None
    stderr_full_path: str | None
    duration_ms: int
    telemetry_id: str
    prompt: ExecutePromptInfo | None = None
    job_id: str | None = None
    # issue #42 — a factual note about the fate of an already-consumed
    # one-shot approval (e.g. it was spent on a command that failed without
    # taking effect). This field NEVER proposes an alternative command or any
    # way around a gate — see commit 016a52c, which removed a `suggestions`
    # field for exactly that reason. It only reports what happened to an
    # approval the human already granted.
    approval_note: str | None = None


class ClassifyRequest(BaseModel):
    command: str
    cwd: str
    agent_id: str


class ClassifyResponse(BaseModel):
    command_tier: int
    agent_cap: int
    effective_tier: int
    decision_preview: Literal["would_execute", "would_deny", "would_prompt", "unknown"]
    template: str
    category: str | None
    matched_rule: str | None
    segments: list[dict]


class PendingPromptDetail(BaseModel):
    """Full detail of a pending prompt, so an approver can see what they are
    deciding on before and after approving (issue #37).

    `normalized_template` is the form that approve_template /
    approve_template_global / approve_verb actually promote — it may be
    materially broader than `raw_cmd`, because the normalizer collapses
    absolute paths into placeholders. `scope_warning` is set when that
    widening is present.
    """

    id: str
    agent_id: str
    cwd: str
    raw_cmd: str
    normalized_template: str
    command_tier: int
    matched_rule_category: str | None = None
    created_at: str | None = None
    expires_at: str | None = None
    approve_decision: str | None = None
    approved_at: str | None = None
    consumed_at: str | None = None
    scope_warning: str | None = None


class ApproveRequest(BaseModel):
    prompt_id: str
    decision: Literal[
        "approve_once", "approve_template", "approve_template_global", "approve_verb", "deny"
    ]
    promote_to_tier: int | None = None
    reason: str | None = None
    approver_agent_id: str | None = None


class ApproveResponse(BaseModel):
    applied: bool
    approve_token: str | None
    template_promoted: bool
    catalog_entry_id: str | None
    verb_promoted: bool = False
    promoted_tier: int | None = None
    # issue #37 — echo back what this approval actually acted on, so a wrong
    # approval (e.g. an agent silently rewrote the command before submitting)
    # is at least detectable after the fact.
    approved: PendingPromptDetail | None = None
    scope_warning: str | None = None


class GetPendingRequest(BaseModel):
    """Inspect pending prompts. Omit prompt_id to list all outstanding ones."""

    prompt_id: str | None = None
    include_resolved: bool = False


class GetPendingResponse(BaseModel):
    prompts: list[PendingPromptDetail]


class HealthResponse(BaseModel):
    """Service health snapshot.

    total_calls_24h: every shell_calls row in the last 24h, including
        passive 'observed_externally' /observe telemetry.
    observed_24h: the subset of total_calls_24h with decision =
        'observed_externally' (passive telemetry, not a gating decision).
    denied_rate_24h / prompt_rate_24h: fraction of *gated* calls (i.e.
        total_calls_24h - observed_24h) that were denied / required a
        prompt. Passive observations are excluded from the denominator so
        these rates reflect actual gating behavior.
    """

    status: Literal["ok", "degraded"]
    total_calls_24h: int
    observed_24h: int
    denied_rate_24h: float
    prompt_rate_24h: float
    p50_latency_ms: int
    p99_latency_ms: int
    catalog_size: int
    pending_prompts: int
    last_error: str | None = None


class ObserveRequest(BaseModel):
    command: str
    cwd: str
    agent_id: str
    exit_code: int | None = None
    duration_ms: int | None = None


class ObserveResponse(BaseModel):
    telemetry_id: str
    classified_tier: int
    normalized_template: str


# ---------------------------------------------------------------------------
# Background job models
# ---------------------------------------------------------------------------

JobStatus = Literal["running", "completed", "failed", "killed", "timed_out"]


class ShellStatusRequest(BaseModel):
    job_id: str


class ShellStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    started_at: str
    finished_at: str | None
    exit_code: int | None
    duration_ms: int | None
    stdout_tail: str
    stderr_tail: str
    stdout_full_path: str | None
    stderr_full_path: str | None


class ShellKillRequest(BaseModel):
    job_id: str
    sig: str = Field(
        default="SIGTERM",
        description="Signal name to send, e.g. SIGTERM or SIGKILL.",
    )


class ShellKillResponse(BaseModel):
    killed: bool
    sig: str | None = None
    reason: str | None = None
