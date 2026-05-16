"""Data-fetch helpers for the shell-runner admin UI.

Pure functions with no Streamlit imports so they can be unit-tested without
spinning up Streamlit.

Schema notes (from src/shell_runner/persistence.py):
  shell_calls:      id, ts, agent_id, cwd, raw_cmd, normalized_template,
                    command_tier, final_tier, decision, matched_rule_pattern,
                    matched_rule_category, exit_code, output_bytes_stdout,
                    output_bytes_stderr, duration_ms, decision_path_json,
                    normalizer_warnings_json
  pending_prompts:  id, created_at, expires_at, agent_id, cwd, raw_cmd,
                    normalized_template, command_tier, matched_rule_category,
                    decision_path_json, approve_token, approve_decision,
                    approved_at, consumed_at
  (No 'status' column — pending = approve_decision IS NULL AND consumed_at IS NULL
   AND expires_at > now)

Approve endpoint: POST /approve_pending
  Body: {"prompt_id": str, "decision": str, "reason": str | None}
Health endpoint:  GET /health
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx

DEFAULT_DB_PATH = Path(
    os.environ.get(
        "SHELL_RUNNER_DB",
        str(Path.home() / ".local" / "share" / "shell-runner" / "telemetry.sqlite3"),
    )
)
DEFAULT_API_URL = os.environ.get("SHELL_RUNNER_API_URL", "http://127.0.0.1:4111")


def get_health(api_url: str = DEFAULT_API_URL, timeout: float = 2.0) -> dict[str, Any]:
    """Fetch /health. Returns the JSON body, or {'status': 'unreachable', 'error': ...} on failure."""
    try:
        r = httpx.get(f"{api_url}/health", timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreachable", "error": str(exc)}


def get_recent_calls(
    db_path: Path = DEFAULT_DB_PATH, limit: int = 50
) -> list[dict[str, Any]]:
    """Return the most recent N calls from shell_calls, ordered DESC by ts."""
    if not db_path.exists():
        return []
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM shell_calls ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_pending_prompts(db_path: Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    """Return non-expired, unapproved, unconsumed pending prompts.

    'Pending' means: approve_decision IS NULL AND consumed_at IS NULL
    AND expires_at > current UTC ISO timestamp.
    """
    if not db_path.exists():
        return []
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM pending_prompts
            WHERE approve_decision IS NULL
              AND consumed_at IS NULL
              AND expires_at > strftime('%Y-%m-%dT%H:%M:%S', 'now')
            ORDER BY created_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def approve_prompt(
    prompt_id: str,
    decision: str,
    reason: str = "",
    promote_to_tier: int | None = None,
    api_url: str = DEFAULT_API_URL,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """POST to /approve_pending.

    decision is one of: approve_once, approve_template, approve_template_global, deny.
    promote_to_tier is forwarded to the server when provided (T1 or T2); applies only
    to template decisions. Server defaults to T2 when omitted.
    Returns the JSON response. Raises on HTTP error.
    """
    body: dict[str, Any] = {
        "prompt_id": prompt_id,
        "decision": decision,
        "reason": reason if reason else None,
    }
    if promote_to_tier is not None:
        body["promote_to_tier"] = promote_to_tier
    r = httpx.post(f"{api_url}/approve_pending", json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()
