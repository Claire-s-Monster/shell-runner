"""SQLite persistence layer for shell-runner telemetry and approval flow."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import uuid
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .seeds import SeedApproval

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path.home() / ".local/share/shell-runner/telemetry.sqlite3"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS shell_calls (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    raw_cmd TEXT NOT NULL,
    normalized_template TEXT NOT NULL,
    command_tier INTEGER NOT NULL,
    final_tier INTEGER NOT NULL,
    decision TEXT NOT NULL,
    matched_rule_pattern TEXT,
    matched_rule_category TEXT,
    exit_code INTEGER,
    output_bytes_stdout INTEGER,
    output_bytes_stderr INTEGER,
    duration_ms INTEGER,
    decision_path_json TEXT,
    normalizer_warnings_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_shell_calls_ts ON shell_calls(ts);
CREATE INDEX IF NOT EXISTS idx_shell_calls_agent ON shell_calls(agent_id);
CREATE INDEX IF NOT EXISTS idx_shell_calls_template ON shell_calls(normalized_template);

CREATE TABLE IF NOT EXISTS templates (
    template TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    observed_count INTEGER NOT NULL DEFAULT 1,
    observed_agents_json TEXT NOT NULL,
    current_tier INTEGER NOT NULL,
    denial_count INTEGER NOT NULL DEFAULT 0,
    promoted_to_tier INTEGER,
    promoted_at TEXT,
    promoted_by TEXT
);

CREATE TABLE IF NOT EXISTS pending_prompts (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    raw_cmd TEXT NOT NULL,
    normalized_template TEXT NOT NULL,
    command_tier INTEGER NOT NULL,
    matched_rule_category TEXT,
    decision_path_json TEXT,
    approve_token TEXT,
    approve_decision TEXT,
    approved_at TEXT,
    consumed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_pending_expires ON pending_prompts(expires_at);
CREATE INDEX IF NOT EXISTS idx_pending_token ON pending_prompts(approve_token);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    telemetry_id TEXT NOT NULL,
    raw_cmd TEXT NOT NULL,
    cwd TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    exit_code INTEGER,
    status TEXT NOT NULL,
    pid INTEGER,
    stdout_path TEXT,
    stderr_path TEXT,
    timeout_s INTEGER NOT NULL,
    FOREIGN KEY(telemetry_id) REFERENCES shell_calls(id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_started ON jobs(started_at);

CREATE TABLE IF NOT EXISTS template_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template TEXT NOT NULL,
    agent_id TEXT,
    approved_tier INTEGER NOT NULL,
    approved_at TEXT NOT NULL,
    approved_via_prompt_id TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_template_approvals_unique
    ON template_approvals(template, COALESCE(agent_id, ''));

CREATE INDEX IF NOT EXISTS idx_template_approvals_template ON template_approvals(template);

CREATE TABLE IF NOT EXISTS verb_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    verb TEXT NOT NULL,
    cwd_prefix TEXT NOT NULL,
    agent_id TEXT,
    approved_tier INTEGER NOT NULL,
    approved_at TEXT NOT NULL,
    approved_via_prompt_id TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_verb_approvals_unique
    ON verb_approvals(verb, cwd_prefix, COALESCE(agent_id, ''));

CREATE INDEX IF NOT EXISTS idx_verb_approvals_verb ON verb_approvals(verb);
"""

# approve_once durability TTL (issue #26 P2 Part B): an approve_once approval
# must survive the subagent that generated the pending prompt so a token-less
# re-submit matching (raw_cmd, cwd, agent_id) can still consume it. This is far
# longer than the default pending-prompt TTL (300s) used at prompt creation.
APPROVE_ONCE_DURABLE_TTL_S = 3600


def _now_utc() -> str:
    return datetime.now(UTC).isoformat()


class Persistence:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA_SQL)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
        finally:
            conn.close()

    # --- shell_calls ---

    def record_call(
        self,
        *,
        agent_id: str,
        cwd: str,
        raw_cmd: str,
        normalized_template: str,
        command_tier: int,
        final_tier: int,
        decision: str,
        matched_rule_pattern: str | None,
        matched_rule_category: str | None,
        exit_code: int | None,
        stdout_bytes: int,
        stderr_bytes: int,
        duration_ms: int,
        decision_path: list[str],
        normalizer_warnings: list[str],
        call_id: str | None = None,
    ) -> str:
        telemetry_id = call_id if call_id is not None else str(uuid.uuid4())
        ts = _now_utc()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO shell_calls (
                    id, ts, agent_id, cwd, raw_cmd, normalized_template,
                    command_tier, final_tier, decision,
                    matched_rule_pattern, matched_rule_category,
                    exit_code, output_bytes_stdout, output_bytes_stderr,
                    duration_ms, decision_path_json, normalizer_warnings_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    telemetry_id,
                    ts,
                    agent_id,
                    cwd,
                    raw_cmd,
                    normalized_template,
                    command_tier,
                    final_tier,
                    decision,
                    matched_rule_pattern,
                    matched_rule_category,
                    exit_code,
                    stdout_bytes,
                    stderr_bytes,
                    duration_ms,
                    json.dumps(decision_path),
                    json.dumps(normalizer_warnings),
                ),
            )
        return telemetry_id

    # --- templates ---

    def upsert_template(
        self,
        *,
        template: str,
        agent_id: str,
        current_tier: int,
        was_denied: bool,
    ) -> None:
        """Insert or update a template row, race-safe via BEGIN IMMEDIATE.

        Uses an explicit exclusive transaction so that concurrent callers
        serialise at the SQLite level: the second writer blocks until the
        first commits rather than racing on a stale read.
        """
        now = _now_utc()
        denial_inc = 1 if was_denied else 0
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT observed_agents_json FROM templates WHERE template = ?",
                (template,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO templates (
                        template, first_seen, last_seen, observed_count,
                        observed_agents_json, current_tier, denial_count
                    ) VALUES (?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        template,
                        now,
                        now,
                        json.dumps([agent_id]),
                        current_tier,
                        denial_inc,
                    ),
                )
            else:
                agents: list[str] = json.loads(existing["observed_agents_json"])
                if agent_id not in agents:
                    agents.append(agent_id)
                conn.execute(
                    """
                    UPDATE templates SET
                        last_seen = ?,
                        observed_count = observed_count + 1,
                        observed_agents_json = ?,
                        current_tier = ?,
                        denial_count = denial_count + ?
                    WHERE template = ?
                    """,
                    (
                        now,
                        json.dumps(agents),
                        current_tier,
                        denial_inc,
                        template,
                    ),
                )
            conn.execute("COMMIT")

    # --- pending prompts ---

    def create_pending_prompt(
        self,
        *,
        agent_id: str,
        cwd: str,
        raw_cmd: str,
        normalized_template: str,
        command_tier: int,
        matched_rule_category: str | None,
        decision_path: list[str],
        ttl_seconds: int = 300,
    ) -> str:
        prompt_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO pending_prompts (
                    id, created_at, expires_at, agent_id, cwd, raw_cmd,
                    normalized_template, command_tier, matched_rule_category,
                    decision_path_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prompt_id,
                    now.isoformat(),
                    expires_at,
                    agent_id,
                    cwd,
                    raw_cmd,
                    normalized_template,
                    command_tier,
                    matched_rule_category,
                    json.dumps(decision_path),
                ),
            )
        return prompt_id

    def approve_prompt(self, *, prompt_id: str, decision: str) -> str | None:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        is_approved = decision in (
            "approve_once",
            "approve_template",
            "approve_template_global",
            "approve_verb",
        )
        token = str(uuid.uuid4()) if is_approved else None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, expires_at FROM pending_prompts WHERE id = ?",
                (prompt_id,),
            ).fetchone()
            if row is None:
                return None
            if decision == "approve_once":
                # Durability (issue #26 P2 Part B): extend the TTL well past the
                # default pending-prompt window so a token-less re-submit
                # matching (raw_cmd, cwd, agent_id) can still consume this
                # approval via consume_approval_by_command, even after the
                # subagent that generated the prompt has exited.
                new_expires_at = (
                    now_dt + timedelta(seconds=APPROVE_ONCE_DURABLE_TTL_S)
                ).isoformat()
                conn.execute(
                    """
                    UPDATE pending_prompts SET
                        approve_token = ?,
                        approve_decision = ?,
                        approved_at = ?,
                        expires_at = ?
                    WHERE id = ?
                    """,
                    (token, decision, now, new_expires_at, prompt_id),
                )
            else:
                # Allow approving even expired prompts (expiry only gates token consumption)
                conn.execute(
                    """
                    UPDATE pending_prompts SET
                        approve_token = ?,
                        approve_decision = ?,
                        approved_at = ?
                    WHERE id = ?
                    """,
                    (token, decision, now, prompt_id),
                )
        return token

    def consume_approve_token(self, *, token: str) -> dict | None:
        """Atomically validate and consume an approve token.

        Uses BEGIN IMMEDIATE so that two concurrent callers serialise at the
        SQLite level.  The first caller that wins the lock sets consumed_at;
        the second sees consumed_at IS NOT NULL and returns None.
        """
        now = _now_utc()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT id, raw_cmd, agent_id, cwd, normalized_template,
                       command_tier, matched_rule_category, expires_at, consumed_at
                FROM pending_prompts
                WHERE approve_token = ?
                """,
                (token,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            # Already consumed
            if row["consumed_at"] is not None:
                conn.execute("ROLLBACK")
                return None
            # Expired
            if row["expires_at"] < now:
                conn.execute("ROLLBACK")
                return None
            conn.execute(
                "UPDATE pending_prompts SET consumed_at = ? WHERE approve_token = ?",
                (now, token),
            )
            conn.execute("COMMIT")
            return {
                "id": row["id"],
                "raw_cmd": row["raw_cmd"],
                "agent_id": row["agent_id"],
                "cwd": row["cwd"],
                "normalized_template": row["normalized_template"],
                "command_tier": row["command_tier"],
                "matched_rule_category": row["matched_rule_category"],
            }

    def consume_approval_by_command(
        self, *, raw_cmd: str, cwd: str, agent_id: str
    ) -> dict | None:
        """Atomically find and consume a durable approve_once approval by command.

        Mirrors consume_approve_token (same BEGIN IMMEDIATE serialisation and
        single-use guarantee) but matches on (raw_cmd, cwd, agent_id) instead
        of a token. Only rows that are approved (approved_at IS NOT NULL),
        unconsumed (consumed_at IS NULL), and unexpired are eligible. Since
        approve_once extends expires_at to APPROVE_ONCE_DURABLE_TTL_S, this
        lets a token-less re-submit consume the approval once even after the
        subagent that generated the pending prompt has exited.
        """
        now = _now_utc()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT id, raw_cmd, agent_id, cwd, normalized_template,
                       command_tier, matched_rule_category, expires_at, consumed_at
                FROM pending_prompts
                WHERE raw_cmd = ? AND cwd = ? AND agent_id = ?
                  AND approved_at IS NOT NULL AND consumed_at IS NULL
                ORDER BY approved_at DESC LIMIT 1
                """,
                (raw_cmd, cwd, agent_id),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            if row["expires_at"] < now:
                conn.execute("ROLLBACK")
                return None
            conn.execute(
                "UPDATE pending_prompts SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (now, row["id"]),
            )
            conn.execute("COMMIT")
            return {
                "id": row["id"],
                "raw_cmd": row["raw_cmd"],
                "agent_id": row["agent_id"],
                "cwd": row["cwd"],
                "normalized_template": row["normalized_template"],
                "command_tier": row["command_tier"],
                "matched_rule_category": row["matched_rule_category"],
            }

    def get_pending_prompt(self, prompt_id: str) -> dict | None:
        """Fetch a pending prompt by id (full row as dict)."""
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT id, raw_cmd, agent_id, cwd, normalized_template,
                       command_tier, matched_rule_category, expires_at,
                       approve_token, approve_decision, approved_at, consumed_at
                FROM pending_prompts WHERE id = ?
                """,
                (prompt_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def list_pending_prompts(
        self, *, include_resolved: bool = False, limit: int = 100
    ) -> list[dict]:
        """List pending prompts so the approver can inspect what they are
        deciding on (issue #37).

        By default only actionable prompts are returned (no decision made
        yet and not consumed). Expiry is intentionally not filtered in SQL:
        the approver benefits from seeing a just-expired prompt rather than
        it silently vanishing from the list.
        """
        query = """
            SELECT id, raw_cmd, agent_id, cwd, normalized_template,
                   command_tier, matched_rule_category, expires_at,
                   approve_token, approve_decision, approved_at, consumed_at,
                   created_at
            FROM pending_prompts
            {where}
            ORDER BY created_at DESC, id
            LIMIT ?
        """
        where = (
            "" if include_resolved else "WHERE approve_decision IS NULL AND consumed_at IS NULL"
        )
        with self._conn() as conn:
            rows = conn.execute(query.format(where=where), (limit,)).fetchall()
            return [dict(row) for row in rows]

    def peek_approve_token(self, *, token: str) -> dict | None:
        """Look up the prompt bound to `token` without consuming it.

        server.py:544 previously called consume_approve_token and only
        THEN validated that the token's bound command/cwd/agent match the
        request, so a caller who retries with a corrected cwd had already
        burned the approval and had to go through a whole fresh
        prompt/approve cycle. peek lets the server validate the binding
        first and consume only on a match (issue #36, secondary
        observation 1). This method never mutates a row: no consumed_at
        write, no decision write.
        """
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT id, raw_cmd, agent_id, cwd, normalized_template,
                       command_tier, matched_rule_category, expires_at, consumed_at
                FROM pending_prompts
                WHERE approve_token = ?
                """,
                (token,),
            ).fetchone()
            return dict(row) if row is not None else None

    def create_template_approval(
        self,
        *,
        template: str,
        agent_id: str | None,
        approved_tier: int,
        approved_via_prompt_id: str | None = None,
    ) -> int:
        """Persist a template approval. Replaces any prior approval for the
        same (template, agent_id) pair. agent_id=None means global approval.
        Returns the row id.
        """
        now = _now_utc()
        scope_key = agent_id if agent_id is not None else ""
        with self._conn() as conn:
            # Delete any existing approval for this scope (replace semantics)
            conn.execute(
                """
                DELETE FROM template_approvals
                WHERE template = ? AND COALESCE(agent_id, '') = ?
                """,
                (template, scope_key),
            )
            cur = conn.execute(
                """
                INSERT INTO template_approvals
                    (template, agent_id, approved_tier, approved_at, approved_via_prompt_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (template, agent_id, approved_tier, now, approved_via_prompt_id),
            )
            row_id = cur.lastrowid
            if row_id is None:  # pragma: no cover - a successful INSERT always sets lastrowid
                raise RuntimeError("INSERT did not produce a rowid")
            return row_id

    def seed_approvals(self, seeds: Iterable[SeedApproval]) -> int:
        """Idempotently insert seed approvals as global (agent_id IS NULL) rows.

        Skips templates already present in template_approvals (regardless of
        agent_id), so user-promoted approvals are never overwritten.

        Returns the count of newly-inserted rows.
        """
        inserted = 0
        with self._conn() as conn:
            for seed in seeds:
                existing = conn.execute(
                    "SELECT 1 FROM template_approvals WHERE template = ? LIMIT 1",
                    (seed.template,),
                ).fetchone()
                if existing is not None:
                    continue
                conn.execute(
                    """
                    INSERT INTO template_approvals
                        (template, agent_id, approved_tier, approved_at, approved_via_prompt_id)
                    VALUES (?, NULL, ?, CURRENT_TIMESTAMP, NULL)
                    """,
                    (seed.template, int(seed.tier)),
                )
                inserted += 1
        return inserted

    def get_template_approved_tier(self, template: str, agent_id: str) -> int | None:
        """Get the most permissive approved tier for a template.
        Global approval (agent_id IS NULL) takes precedence over agent-specific.
        Returns None if no approval exists.
        """
        with self._conn() as conn:
            # Global first
            row = conn.execute(
                """
                SELECT approved_tier FROM template_approvals
                WHERE template = ? AND agent_id IS NULL
                ORDER BY approved_at DESC LIMIT 1
                """,
                (template,),
            ).fetchone()
            if row is not None:
                return int(row[0])
            # Then agent-specific
            row = conn.execute(
                """
                SELECT approved_tier FROM template_approvals
                WHERE template = ? AND agent_id = ?
                ORDER BY approved_at DESC LIMIT 1
                """,
                (template, agent_id),
            ).fetchone()
            return int(row[0]) if row is not None else None

    def create_verb_approval(
        self,
        *,
        verb: str,
        cwd_prefix: str,
        agent_id: str | None,
        approved_tier: int,
        approved_via_prompt_id: str | None = None,
    ) -> int:
        """Persist a verb-level approval scoped to a cwd subtree.

        Replaces any prior approval for the same (verb, cwd_prefix, agent_id)
        triple (UPSERT semantics), race-safe via BEGIN IMMEDIATE — same pattern
        as create_template_approval. agent_id=None means the approval applies
        to any agent operating under cwd_prefix. Returns the row id.
        """
        now = _now_utc()
        scope_key = agent_id if agent_id is not None else ""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                DELETE FROM verb_approvals
                WHERE verb = ? AND cwd_prefix = ? AND COALESCE(agent_id, '') = ?
                """,
                (verb, cwd_prefix, scope_key),
            )
            cur = conn.execute(
                """
                INSERT INTO verb_approvals
                    (verb, cwd_prefix, agent_id, approved_tier, approved_at, approved_via_prompt_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (verb, cwd_prefix, agent_id, approved_tier, now, approved_via_prompt_id),
            )
            conn.execute("COMMIT")
            row_id = cur.lastrowid
            if row_id is None:  # pragma: no cover - a successful INSERT always sets lastrowid
                raise RuntimeError("INSERT did not produce a rowid")
            return row_id

    def get_verb_approved_tier(self, verb: str, cwd: str, agent_id: str) -> int | None:
        """Get the most restrictive approved tier among verb+cwd-prefix approvals
        whose cwd_prefix contains `cwd`.

        Candidate rows are fetched for `verb` where agent_id matches the caller
        or is NULL (applies to any agent under that cwd_prefix). Containment is
        checked in PYTHON using realpath + Path.is_relative_to — the same safe
        pattern executor.py uses for cwd-root validation — rather than a SQL
        string-prefix match, to avoid path-traversal / sibling-directory
        false positives (e.g. "/home/user2" is not contained by "/home/user").

        Returns the MAX approved_tier among matching rows (the most
        conservative one wins when multiple cwd_prefix/agent scopes overlap),
        or None if no row's cwd_prefix contains cwd.
        """
        resolved_cwd = Path(os.path.realpath(cwd))
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT cwd_prefix, approved_tier FROM verb_approvals
                WHERE verb = ? AND (agent_id = ? OR agent_id IS NULL)
                """,
                (verb, agent_id),
            ).fetchall()

        best: int | None = None
        for row in rows:
            prefix = Path(os.path.realpath(row["cwd_prefix"]))
            try:
                within = resolved_cwd == prefix or resolved_cwd.is_relative_to(prefix)
            except ValueError:
                within = False
            if not within:
                continue
            tier = int(row["approved_tier"])
            if best is None or tier > best:
                best = tier
        return best

    def find_similar_approved_templates(
        self,
        template: str,
        agent_id: str,
        *,
        limit: int = 3,
        min_similarity: float = 0.5,
    ) -> list[tuple[str, int, str | None, str | None]]:
        """Return up to `limit` approved templates similar to `template`.

        Similarity = verb-anchored Jaccard over whitespace-split tokens. The first
        token (verb) must match exactly; if verbs differ, the candidate is excluded
        regardless of overall overlap. Candidates are filtered by Jaccard >=
        min_similarity, then sorted by Jaccard descending, then by approved_at
        descending as a tiebreaker.

        Looks at both global (agent_id IS NULL) approvals and approvals scoped to
        the given `agent_id`. Excludes any approval whose template equals
        `template` exactly (caller already handled that via get_template_approved_tier).

        Returns: list of (template, approved_tier, category, example_raw_cmd) tuples.
        `category` is pulled from the `templates` table if present (None if no row).
        `example_raw_cmd` is the most recent raw_cmd from the `calls` table whose
        normalized_template equals the approved template AND whose decision is
        'executed' (None if no such call exists).
        """

        def _jaccard(a: set[str], b: set[str]) -> float:
            union = a | b
            if not union:
                return 0.0
            return len(a & b) / len(union)

        query_tokens = template.split()
        if not query_tokens:
            return []
        query_verb = query_tokens[0]
        query_set = set(query_tokens)

        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT template, approved_tier, approved_at,
                       CASE WHEN agent_id IS NULL THEN 0 ELSE 1 END AS is_agent_scoped
                FROM template_approvals
                WHERE (agent_id IS NULL OR agent_id = ?)
                  AND template != ?
                ORDER BY approved_at DESC
                """,
                (agent_id, template),
            ).fetchall()

            # Deduplicate: for same template keep most permissive tier; tie → prefer global
            best: dict[str, tuple[int, str, int]] = (
                {}
            )  # template → (tier, approved_at, is_agent_scoped)
            for row in rows:
                tmpl = row["template"]
                tier = row["approved_tier"]
                at = row["approved_at"]
                scoped = row["is_agent_scoped"]
                if tmpl not in best:
                    best[tmpl] = (tier, at, scoped)
                else:
                    prev_tier, prev_at, prev_scoped = best[tmpl]
                    if tier < prev_tier:  # lower tier = more permissive
                        best[tmpl] = (tier, at, scoped)
                    elif tier == prev_tier and scoped > prev_scoped:
                        # same tier, prefer global (scoped=0) over agent (scoped=1)
                        best[tmpl] = (prev_tier, prev_at, prev_scoped)

            # Rank by Jaccard with verb filter
            # Why: verb must match to avoid false positives across unrelated commands
            ranked: list[tuple[float, str, str, int]] = []  # (sim, approved_at, tmpl, tier)
            for tmpl, (tier, approved_at, _) in best.items():
                cand_tokens = tmpl.split()
                if not cand_tokens:
                    continue
                if cand_tokens[0] != query_verb:
                    continue
                sim = _jaccard(query_set, set(cand_tokens))
                if sim >= min_similarity:
                    ranked.append((sim, approved_at, tmpl, tier))

            # Two-pass stable sort: secondary key first, then primary (Python sort is stable)
            ranked.sort(key=lambda x: x[1], reverse=True)  # approved_at DESC
            ranked.sort(
                key=lambda x: -x[0]
            )  # sim DESC — stable, preserves approved_at order for ties
            top = ranked[:limit]

            results: list[tuple[str, int, str | None, str | None]] = []
            for _, _, tmpl, tier in top:
                # category: templates table has no category column → always None
                category: str | None = None

                # example_raw_cmd: most recent 'executed' call for this template
                call_row = conn.execute(
                    """
                    SELECT raw_cmd FROM shell_calls
                    WHERE normalized_template = ? AND decision = 'executed'
                    ORDER BY ts DESC LIMIT 1
                    """,
                    (tmpl,),
                ).fetchone()
                example_raw_cmd: str | None = call_row["raw_cmd"] if call_row is not None else None

                results.append((tmpl, tier, category, example_raw_cmd))

        return results

    def cleanup_expired_prompts(self) -> int:
        now = _now_utc()
        with self._conn() as conn:
            cursor = conn.execute("DELETE FROM pending_prompts WHERE expires_at < ?", (now,))
            return cursor.rowcount

    # --- jobs ---

    def create_job(
        self,
        *,
        job_id: str,
        telemetry_id: str,
        raw_cmd: str,
        cwd: str,
        agent_id: str,
        timeout_s: int,
        stdout_path: str,
        stderr_path: str,
        pid: int,
    ) -> None:
        started_at = _now_utc()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, telemetry_id, raw_cmd, cwd, agent_id,
                    started_at, status, pid, stdout_path, stderr_path, timeout_s
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)
                """,
                (
                    job_id,
                    telemetry_id,
                    raw_cmd,
                    cwd,
                    agent_id,
                    started_at,
                    pid,
                    stdout_path,
                    stderr_path,
                    timeout_s,
                ),
            )

    def update_job_status(
        self,
        job_id: str,
        *,
        status: str,
        exit_code: int | None = None,
        finished_at: str | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE jobs SET status = ?, exit_code = ?, finished_at = ?
                WHERE job_id = ?
                """,
                (status, exit_code, finished_at, job_id),
            )

    def get_job(self, job_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_running_jobs(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status = 'running' ORDER BY started_at",
            ).fetchall()
        return [dict(r) for r in rows]

    def cleanup_old_jobs(self, retention_seconds: int) -> list[dict]:
        """Delete jobs older than retention_seconds (by started_at).

        Returns rows that were removed so the caller can delete stdout/stderr files.

        Uses julianday() for timestamp arithmetic because strftime('%s', ...)
        returns NULL for ISO 8601 strings with timezone suffix (e.g. '+00:00'),
        which is the format produced by _now_iso().
        """
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE (julianday('now') - julianday(started_at)) * 86400.0 > ?
                """,
                (retention_seconds,),
            ).fetchall()
            removed = [dict(r) for r in rows]
            if removed:
                conn.execute(
                    """
                    DELETE FROM jobs
                    WHERE (julianday('now') - julianday(started_at)) * 86400.0 > ?
                    """,
                    (retention_seconds,),
                )
        return removed

    def cleanup_old_shell_calls(
        self, retention_seconds: float, batch_size: int = 5000, max_batches: int = 10
    ) -> int:
        """Delete stale passive /observe telemetry from shell_calls.

        Only rows with decision = 'observed_externally' are eligible for
        deletion. Audit rows (executed, denied, prompt_required, running)
        are NEVER deleted by this method, regardless of age — they are the
        record relied on for approval/denial history and may still be
        referenced by jobs.telemetry_id.

        The cutoff is computed as an ISO-8601 UTC timestamp string in Python
        and compared directly against the ts column (`ts < ?`) so the query
        can use idx_shell_calls_ts. Do NOT wrap ts in julianday() or any
        other function here — that would force a full scan of a 5M+ row
        table.

        Deletion is batched (at most batch_size rows per DELETE) and capped
        at max_batches so a single call does bounded work and cannot stall
        the periodic cleanup tick. Each DELETE auto-commits immediately
        (connections use isolation_level=None). Stops early once a batch
        deletes fewer than batch_size rows.

        Returns the total number of rows deleted. Note: shell_calls' table
        uses auto_vacuum=NONE, so deleting rows does not shrink the database
        file — run VACUUM separately (e.g. during a low-traffic window) to
        reclaim disk space.
        """
        cutoff = (datetime.now(UTC) - timedelta(seconds=retention_seconds)).isoformat()
        total_deleted = 0
        with self._conn() as conn:
            for _ in range(max_batches):
                cursor = conn.execute(
                    """
                    DELETE FROM shell_calls WHERE id IN (
                        SELECT id FROM shell_calls
                        WHERE decision = 'observed_externally' AND ts < ?
                        LIMIT ?
                    )
                    """,
                    (cutoff, batch_size),
                )
                deleted = cursor.rowcount
                total_deleted += deleted
                if deleted < batch_size:
                    break
        return total_deleted

    # --- queries ---

    def telemetry_query(
        self,
        *,
        agent_id: str | None = None,
        decision: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []
        if agent_id is not None:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if decision is not None:
            clauses.append("decision = ?")
            params.append(decision)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM shell_calls {where} ORDER BY ts DESC LIMIT ?",  # noqa: S608
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def health_stats(self) -> dict:
        since_24h = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        with self._conn() as conn:
            total_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM shell_calls WHERE ts >= ?",
                (since_24h,),
            ).fetchone()
            total = total_row["cnt"] if total_row else 0

            observed_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM shell_calls"
                " WHERE ts >= ? AND decision = 'observed_externally'",
                (since_24h,),
            ).fetchone()
            observed = observed_row["cnt"] if observed_row else 0

            denied_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM shell_calls WHERE ts >= ? AND decision = 'denied'",
                (since_24h,),
            ).fetchone()
            denied = denied_row["cnt"] if denied_row else 0

            prompt_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM shell_calls"
                " WHERE ts >= ? AND decision = 'prompt_required'",
                (since_24h,),
            ).fetchone()
            prompted = prompt_row["cnt"] if prompt_row else 0

            pending_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM pending_prompts"
                " WHERE consumed_at IS NULL AND expires_at >= ?",
                (_now_utc(),),
            ).fetchone()
            pending = pending_row["cnt"] if pending_row else 0

            # Latency percentiles from 24h window
            latency_rows = conn.execute(
                "SELECT duration_ms FROM shell_calls"
                " WHERE ts >= ? AND duration_ms IS NOT NULL ORDER BY duration_ms",
                (since_24h,),
            ).fetchall()

        durations = [r["duration_ms"] for r in latency_rows]
        p50 = _percentile(durations, 50)
        p99 = _percentile(durations, 99)

        # denied_rate_24h / prompt_rate_24h are gating rates: they exclude
        # passive 'observed_externally' /observe telemetry from the
        # denominator so the rate reflects actual gating decisions rather
        # than being diluted ~200x by passive observation volume.
        gated_total = total - observed
        denied_rate = denied / gated_total if gated_total > 0 else 0.0
        prompt_rate = prompted / gated_total if gated_total > 0 else 0.0

        return {
            "total_calls_24h": total,
            "observed_24h": observed,
            "denied_rate_24h": denied_rate,
            "prompt_rate_24h": prompt_rate,
            "p50_latency_ms": p50,
            "p99_latency_ms": p99,
            "pending_prompts_count": pending,
            "last_error": None,
        }


def _percentile(values: list[int], pct: int) -> int:
    if not values:
        return 0
    idx = max(0, int(len(values) * pct / 100) - 1)
    return values[idx]


class TelemetryWriter:
    """Non-blocking async wrapper around :meth:`Persistence.record_call`.

    Telemetry writes are fire-and-forget: the request path enqueues a record
    via :meth:`submit` (non-blocking) and returns immediately.  A background
    drain task processes entries via :func:`asyncio.to_thread` so SQLite I/O
    never stalls the event loop.

    The queue is bounded (default 10 000 entries).  If the drain falls behind
    and the queue fills, :meth:`submit` drops the incoming record and increments
    :attr:`dropped_count` rather than blocking the caller.
    """

    def __init__(self, persistence: Persistence, queue_maxsize: int = 10_000) -> None:
        self._persistence = persistence
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_maxsize)
        self._drain_task: asyncio.Task[None] | None = None
        self._stopping = False
        self.dropped_count: int = 0

    async def start(self) -> None:
        """Spawn the drain task.  Call once from the FastAPI lifespan."""
        self._stopping = False
        self._drain_task = asyncio.create_task(self._drain_loop(), name="telemetry-drain")

    async def stop(self) -> None:
        """Drain remaining queued records then cancel the task."""
        self._stopping = True
        # Only join() if the drain task is still alive; if it died early, join() deadlocks.
        if self._drain_task is not None and not self._drain_task.done():
            try:
                await asyncio.wait_for(self._queue.join(), timeout=30.0)
            except TimeoutError:
                logger.warning("TelemetryWriter.stop(): timed out waiting for queue drain")
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None
        # Discard any items the drain task never processed (drain died or timed out).
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break

    async def submit(self, *, wait: bool = False, **kwargs: Any) -> None:
        """Enqueue a record for async writing.  Never blocks the caller by default.

        If the queue is full the record is silently dropped and
        :attr:`dropped_count` is incremented (telemetry is best-effort).

        If ``wait`` is True, blocks until this specific record has been
        processed by the single drain task — write ordering/serialization is
        still preserved (all records go through the same queue), only this
        caller waits. Required when the caller's next statement depends on
        the row already being visible, e.g. an FK-dependent insert.
        """
        if self._stopping:
            self.dropped_count += 1
            logger.warning(
                "TelemetryWriter stopped — dropped telemetry record (total dropped: %d)",
                self.dropped_count,
            )
            return
        done: asyncio.Future[None] | None = None
        if wait:
            done = asyncio.get_running_loop().create_future()
            kwargs["_done_future"] = done
        try:
            self._queue.put_nowait(kwargs)
        except asyncio.QueueFull:
            self.dropped_count += 1
            logger.warning(
                "TelemetryWriter queue full — dropped telemetry record (total dropped: %d)",
                self.dropped_count,
            )
            return
        if done is not None:
            await done

    async def _drain_loop(self) -> None:
        """Continuously drain the queue, writing each record to SQLite off-thread."""
        while True:
            try:
                record = await self._queue.get()
            except asyncio.CancelledError:
                break
            done_future = record.pop("_done_future", None)
            cancelled = False
            write_exc: BaseException | None = None
            try:
                await asyncio.to_thread(self._persistence.record_call, **record)
            except asyncio.CancelledError:
                cancelled = True
                write_exc = asyncio.CancelledError()
                logger.warning("TelemetryWriter: drain interrupted by cancellation")
            except Exception as exc:
                write_exc = exc
                logger.exception("TelemetryWriter: error writing telemetry record; dropping")
            finally:
                self._queue.task_done()
                if done_future is not None and not done_future.done():
                    if write_exc is not None:
                        done_future.set_exception(write_exc)
                    else:
                        done_future.set_result(None)
            if cancelled:
                break


class CatalogWriter:
    """Non-blocking async wrapper around :meth:`Persistence.upsert_template`.

    Catalog writes are fire-and-forget: the request path enqueues a record
    via :meth:`submit` (non-blocking) and returns immediately.  A background
    drain task processes entries via :func:`asyncio.to_thread` so SQLite I/O
    never stalls the event loop.

    The queue is bounded (default 10 000 entries).  If the drain falls behind
    and the queue fills, :meth:`submit` drops the incoming record and increments
    :attr:`dropped_count` rather than blocking the caller.
    """

    def __init__(self, persistence: Persistence, queue_maxsize: int = 10_000) -> None:
        self._persistence = persistence
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_maxsize)
        self._drain_task: asyncio.Task[None] | None = None
        self._stopping = False
        self.dropped_count: int = 0

    async def start(self) -> None:
        """Spawn the drain task.  Call once from the FastAPI lifespan."""
        self._stopping = False
        self._drain_task = asyncio.create_task(self._drain_loop(), name="catalog-drain")

    async def stop(self) -> None:
        """Drain remaining queued records then cancel the task."""
        self._stopping = True
        # Only join() if the drain task is still alive; if it died early, join() deadlocks.
        if self._drain_task is not None and not self._drain_task.done():
            try:
                await asyncio.wait_for(self._queue.join(), timeout=30.0)
            except TimeoutError:
                logger.warning("CatalogWriter.stop(): timed out waiting for queue drain")
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None
        # Discard any items the drain task never processed (drain died or timed out).
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break

    async def submit(self, **kwargs: Any) -> None:
        """Enqueue a record for async writing.  Never blocks the caller.

        If the queue is full the record is silently dropped and
        :attr:`dropped_count` is incremented (catalog updates are best-effort).
        """
        if self._stopping:
            self.dropped_count += 1
            logger.warning(
                "CatalogWriter stopped — dropped catalog record (total dropped: %d)",
                self.dropped_count,
            )
            return
        try:
            self._queue.put_nowait(kwargs)
        except asyncio.QueueFull:
            self.dropped_count += 1
            logger.warning(
                "CatalogWriter queue full — dropped catalog record (total dropped: %d)",
                self.dropped_count,
            )

    async def _drain_loop(self) -> None:
        """Continuously drain the queue, writing each record to SQLite off-thread."""
        while True:
            try:
                record = await self._queue.get()
            except asyncio.CancelledError:
                break
            cancelled = False
            try:
                await asyncio.to_thread(self._persistence.upsert_template, **record)
            except asyncio.CancelledError:
                cancelled = True
                logger.warning("CatalogWriter: drain interrupted by cancellation")
            except Exception:
                logger.exception("CatalogWriter: error writing catalog record; dropping")
            finally:
                self._queue.task_done()
            if cancelled:
                break
