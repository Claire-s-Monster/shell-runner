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
            "approve_token to pass back to shell_execute, plus an echo of the "
            "command that was actually approved (raw_cmd, normalized_template, "
            "cwd, agent_id) so a wrong approval is detectable. "
            "PRIMARY SESSION ONLY — agents cannot call this themselves. "
            "approver_agent_id is REQUIRED: the server rejects self-approval and "
            "non-primary approvers. Call shell_get_pending first to see what a "
            "prompt_id actually refers to before approving it."
        ),
        "schema": {
            "type": "object",
            "required": ["prompt_id", "decision", "approver_agent_id"],
            "properties": {
                "prompt_id": {"type": "string"},
                "decision": {
                    "type": "string",
                    "enum": [
                        "approve_once",
                        "approve_template",
                        "approve_template_global",
                        "approve_verb",
                        "deny",
                    ],
                    "description": (
                        "approve_verb promotes the command's VERB for the current cwd "
                        "subtree + agent to the given tier (poll-free auto-exec for later "
                        "variants of that verb in that directory)."
                    ),
                },
                "promote_to_tier": {"type": ["integer", "null"]},
                "reason": {"type": ["string", "null"]},
                "approver_agent_id": {
                    "type": "string",
                    "description": (
                        "REQUIRED. Identity of the approver — the primary/DENY-capability "
                        "session, and necessarily distinct from the command's executing "
                        "agent. Accepts 'primary' or a suffixed form such as "
                        "'primary-session-<id>'. Omitting it is no longer permitted: the "
                        "omitted path used to skip the self-approval and capability checks "
                        "entirely, making it strictly more permissive than supplying it "
                        "(issue #36). NOTE: agent_id is self-asserted — this is a "
                        "mitigation, not an authenticated boundary; see issue #29."
                    ),
                },
            },
        },
    },
    "shell_get_pending": {
        "description": (
            "Read-only inspection of pending T3/T4 approval prompts. Returns "
            "raw_cmd, normalized_template, cwd, agent_id, command_tier and a "
            "scope_warning for each prompt. Omit prompt_id to list all outstanding "
            "prompts. Exists because the approver is otherwise deciding blind "
            "(issue #37), and because normalization can silently widen what "
            "approve_template promotes — e.g. 'rm -f /repo/.git/index.lock' "
            "becomes 'rm -f <cwd_path>', granting auto-'rm -f' on ANY absolute "
            "path. Mutates nothing; safe to call before every approval."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "prompt_id": {
                    "type": ["string", "null"],
                    "description": ("Specific prompt to inspect; omit to list all outstanding."),
                },
                "include_resolved": {
                    "type": "boolean",
                    "description": (
                        "Also include prompts already approved/denied/consumed. Default false."
                    ),
                },
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
