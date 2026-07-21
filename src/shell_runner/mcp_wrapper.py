"""MCP tool registry for shell-runner.

Defines the TOOLS dict used by server.py's /mcp JSON-RPC endpoint to serve
tools/list responses and route tools/call dispatches.  The actual HTTP routing
(method + path) metadata is preserved for reference but is no longer used at
runtime — dispatch goes directly to the FastAPI route handlers in-process.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Tool registry — name → schema metadata
# ---------------------------------------------------------------------------

TOOLS: dict[str, dict[str, Any]] = {
    "shell_execute": {
        "description": (
            "Execute a shell command with sandboxing. Returns decision "
            "(executed|denied|prompt_required|running), exit_code, stdout, stderr, telemetry_id. "
            "For T3/T4 commands, returns prompt_required with prompt_id; user must call "
            "shell_approve_pending then re-call shell_execute with approve_token. "
            "Set run_in_background=true to spawn asynchronously and get a job_id."
        ),
        "schema": {
            "type": "object",
            "required": ["command", "cwd", "agent_id"],
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "cwd": {"type": "string", "description": "Working directory (must exist)"},
                "agent_id": {"type": "string", "description": "Caller agent identifier"},
                "timeout_s": {
                    "type": "integer",
                    "default": 30,
                    "minimum": 1,
                    "maximum": 3600,
                },
                "approve_token": {
                    "type": ["string", "null"],
                    "description": (
                        "Token from shell_approve_pending; required to execute T3/T4 commands"
                    ),
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["inline", "file", "summarize"],
                    "default": "inline",
                    "description": (
                        "'file' runs the command detached and returns a job_id "
                        "(poll via shell_status); use for long-running commands."
                    ),
                },
                "run_in_background": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "If true, spawn asynchronously. Returns decision=running and job_id. "
                        "Use shell_status(job_id) to poll, shell_kill(job_id) to terminate."
                    ),
                },
            },
        },
    },
    "shell_classify": {
        "description": (
            "Preview the classification of a command without executing it. "
            "Useful for agents to self-check before commit."
        ),
        "schema": {
            "type": "object",
            "required": ["command", "cwd", "agent_id"],
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "agent_id": {"type": "string"},
            },
        },
    },
    "shell_approve_pending": {
        "description": (
            "Approve or deny a pending shell command prompt. On approval, returns "
            "approve_token to pass back to shell_execute. "
            "PRIMARY SESSION ONLY — agents cannot call this themselves."
        ),
        "schema": {
            "type": "object",
            "required": ["prompt_id", "decision"],
            "properties": {
                "prompt_id": {"type": "string"},
                "decision": {
                    "type": "string",
                    "enum": [
                        "approve_once",
                        "approve_template",
                        "approve_template_global",
                        "deny",
                    ],
                },
                "promote_to_tier": {"type": ["integer", "null"]},
                "reason": {"type": ["string", "null"]},
            },
        },
    },
    "shell_health": {
        "description": (
            "Get shell-runner server health: total/denied/prompt rates, latency, "
            "catalog size, pending prompt count."
        ),
        "schema": {"type": "object", "properties": {}},
    },
    "shell_status": {
        "description": (
            "Poll the status of a background job started with shell_execute(run_in_background=true). "
            "Returns status (running|completed|failed|killed|timed_out), exit_code, "
            "stdout/stderr tails, and full log paths."
        ),
        "schema": {
            "type": "object",
            "required": ["job_id"],
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Job ID returned by shell_execute with run_in_background=true",
                },
            },
        },
    },
    "shell_kill": {
        "description": (
            "Send a signal to a running background job. "
            "Defaults to SIGTERM; use sig='SIGKILL' for forceful termination."
        ),
        "schema": {
            "type": "object",
            "required": ["job_id"],
            "properties": {
                "job_id": {"type": "string", "description": "Job ID to terminate"},
                "sig": {
                    "type": "string",
                    "default": "SIGTERM",
                    "description": "Signal name, e.g. SIGTERM or SIGKILL",
                },
            },
        },
    },
}
