"""MCP wrapper for shell-runner HTTP service.

Exposes 3 meta-tools (discover_tools / get_tool_spec / execute_tool) per the
session-intelligence lean-MCP pattern.  Agents call
execute_tool("shell_execute", {...}) to run a command; the wrapper forwards the
request to the local HTTP service and returns the JSON response verbatim.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastmcp import FastMCP

DEFAULT_BASE_URL: str = os.environ.get("SHELL_RUNNER_URL", "http://127.0.0.1:4003")
DEFAULT_TIMEOUT_S: int = 60  # longer than HTTP timeout to allow server-side work

mcp = FastMCP("shell-runner")

# ---------------------------------------------------------------------------
# Tool registry — name → metadata + HTTP routing
# ---------------------------------------------------------------------------

TOOLS: dict[str, dict[str, Any]] = {
    "shell_execute": {
        "description": (
            "Execute a shell command with sandboxing. Returns decision "
            "(executed|denied|prompt_required), exit_code, stdout, stderr, telemetry_id. "
            "For T3/T4 commands, returns prompt_required with prompt_id; user must call "
            "shell_approve_pending then re-call shell_execute with approve_token."
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
                    "maximum": 600,
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
                },
            },
        },
        "method": "POST",
        "path": "/execute",
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
        "method": "POST",
        "path": "/classify",
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
        "method": "POST",
        "path": "/approve_pending",
    },
    "shell_health": {
        "description": (
            "Get shell-runner server health: total/denied/prompt rates, latency, "
            "catalog size, pending prompt count."
        ),
        "schema": {"type": "object", "properties": {}},
        "method": "GET",
        "path": "/health",
    },
}


def _client() -> httpx.Client:
    """Return a configured httpx client for the shell-runner HTTP service."""
    return httpx.Client(base_url=DEFAULT_BASE_URL, timeout=DEFAULT_TIMEOUT_S)


# ---------------------------------------------------------------------------
# Meta-tools
# ---------------------------------------------------------------------------


@mcp.tool()
def discover_tools(pattern: str = "") -> dict[str, Any]:
    """List shell-runner tools, optionally filtered by name substring.

    USE WHEN: finding which shell-runner operations are available.

    Args:
        pattern: Case-insensitive substring filter.  Leave empty to list all.

    Returns:
        available_tools: list of {name, description}
        total_tools: total count in registry
        filtered_count: count after filtering
    """
    matched = [
        {"name": name, "description": meta["description"]}
        for name, meta in TOOLS.items()
        if not pattern or pattern.lower() in name.lower()
    ]
    return {
        "available_tools": matched,
        "total_tools": len(TOOLS),
        "filtered_count": len(matched),
    }


@mcp.tool()
def get_tool_spec(tool_name: str) -> dict[str, Any]:
    """Return the JSON schema for a specific shell-runner tool.

    USE WHEN: need exact parameters before calling execute_tool.

    Args:
        tool_name: Exact name from discover_tools() output.

    Returns:
        name, description, schema — or error + available list if not found.
    """
    if tool_name not in TOOLS:
        return {"error": f"unknown tool: {tool_name}", "available": list(TOOLS.keys())}
    meta = TOOLS[tool_name]
    return {
        "name": tool_name,
        "description": meta["description"],
        "schema": meta["schema"],
    }


@mcp.tool()
def execute_tool(tool_name: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """Execute a shell-runner tool by name with the given parameters.

    USE WHEN: ready to run a shell command, classify, approve, or check health.

    Workflow:
        1. discover_tools(pattern)       — find the right tool
        2. get_tool_spec(tool_name)      — confirm parameter schema
        3. execute_tool(tool_name, {...}) — YOU ARE HERE

    Args:
        tool_name:  Exact name from discover_tools().
        parameters: Dict matching the tool schema.

    Returns:
        Tool-specific JSON response from the HTTP service, or an error dict.
    """
    if tool_name not in TOOLS:
        return {"error": f"unknown tool: {tool_name}", "available": list(TOOLS.keys())}
    meta = TOOLS[tool_name]
    client = _client()
    try:
        if meta["method"] == "GET":
            r = client.get(meta["path"])
        else:
            r = client.post(meta["path"], json=parameters)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as exc:
        return {
            "error": f"HTTP {exc.response.status_code}",
            "detail": exc.response.text,
        }
    except httpx.RequestError as exc:
        return {
            "error": "connection_failed",
            "detail": str(exc),
            "hint": (
                "Is the shell-runner service running? " "Try: systemctl --user status shell-runner"
            ),
        }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for the MCP server (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
