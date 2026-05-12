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


class ExecutePromptInfo(BaseModel):
    id: str
    raw_cmd: str
    template: str
    category: str | None
    tier: int
    why: str


class ExecuteSuggestion(BaseModel):
    template: str
    tier: int
    category: str
    example: str | None = None


class ExecuteResponse(BaseModel):
    decision: Literal["executed", "denied", "prompt_required", "reformulate_suggested"]
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
    suggestions: list[ExecuteSuggestion] | None = None


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


class ApproveRequest(BaseModel):
    prompt_id: str
    decision: Literal["approve_once", "approve_template", "approve_template_global", "deny"]
    promote_to_tier: int | None = None
    reason: str | None = None


class ApproveResponse(BaseModel):
    applied: bool
    approve_token: str | None
    template_promoted: bool
    catalog_entry_id: str | None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    total_calls_24h: int
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
