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
  Body: {"prompt_id": str, "decision": str, "reason": str | None,
         "approver_agent_id": str}
Health endpoint:  GET /health
"""

from __future__ import annotations

import json
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
    approver_agent_id: str = "primary",
    api_url: str = DEFAULT_API_URL,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """POST to /approve_pending.

    decision is one of: approve_once, approve_template, approve_template_global, deny.
    promote_to_tier is forwarded to the server when provided (T1 or T2); applies only
    to template decisions. Server defaults to T2 when omitted.

    approver_agent_id identifies who is approving. It defaults to "primary" because
    this admin UI is the human/primary approval surface — sending it engages the
    server's Layer-1 approver-identity guard (issue #29), which resolves "primary"
    to DENY capability and rejects self-approval. Omitting it makes the server skip
    the guard entirely, so the UI always sends it.
    Returns the JSON response. Raises on HTTP error.
    """
    body: dict[str, Any] = {
        "prompt_id": prompt_id,
        "decision": decision,
        "reason": reason if reason else None,
        "approver_agent_id": approver_agent_id,
    }
    if promote_to_tier is not None:
        body["promote_to_tier"] = promote_to_tier
    r = httpx.post(f"{api_url}/approve_pending", json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()


def parse_decision_path(decision_path_json: str | None) -> list[str]:
    """Decode the stored decision_path JSON list; return [] on any problem."""
    if not decision_path_json:
        return []
    try:
        value = json.loads(decision_path_json)
    except (ValueError, TypeError):
        return []
    if not isinstance(value, list):
        return []
    return [str(step) for step in value]


def _jaccard(a: set[str], b: set[str]) -> float:
    union = a | b
    return (len(a & b) / len(union)) if union else 0.0


def get_similar_approvals(
    template: str,
    agent_id: str,
    db_path: Path = DEFAULT_DB_PATH,
    *,
    limit: int = 3,
    min_similarity: float = 0.5,
) -> list[dict[str, Any]]:
    """Read-only mirror of Persistence.find_similar_approved_templates.

    Returns dicts: {template, approved_tier, example_raw_cmd, similarity}.
    example_raw_cmd is RAW (unredacted) — callers must redact before display.
    """
    if not db_path.exists():
        return []
    tokens = template.split()
    if not tokens:
        return []
    verb, qset = tokens[0], set(tokens)
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT template, approved_tier, approved_at,
                   CASE WHEN agent_id IS NULL THEN 0 ELSE 1 END AS scoped
            FROM template_approvals
            WHERE (agent_id IS NULL OR agent_id = ?) AND template != ?
            ORDER BY approved_at DESC
            """,
            (agent_id, template),
        ).fetchall()
        best: dict[str, tuple[int, str, int]] = {}
        for r in rows:
            t, tier, at, scoped = r["template"], r["approved_tier"], r["approved_at"], r["scoped"]
            if t not in best or tier < best[t][0]:
                best[t] = (tier, at, scoped)
        ranked = []
        for t, (tier, at, _) in best.items():
            ct = t.split()
            if not ct or ct[0] != verb:
                continue
            sim = _jaccard(qset, set(ct))
            if sim >= min_similarity:
                ranked.append((sim, at, t, tier))
        ranked.sort(key=lambda x: x[1], reverse=True)
        ranked.sort(key=lambda x: -x[0])
        out: list[dict[str, Any]] = []
        for sim, _at, t, tier in ranked[:limit]:
            call = conn.execute(
                "SELECT raw_cmd FROM shell_calls WHERE normalized_template = ? "
                "AND decision = 'executed' ORDER BY ts DESC LIMIT 1",
                (t,),
            ).fetchone()
            out.append({
                "template": t, "approved_tier": tier,
                "example_raw_cmd": call["raw_cmd"] if call else None,
                "similarity": round(sim, 3),
            })
    return out
