"""SQLite persistence layer for shell-runner telemetry and approval flow."""

from __future__ import annotations

import asyncio
import json
import logging
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
"""


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
    ) -> str:
        telemetry_id = str(uuid.uuid4())
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
        now = _now_utc()
        is_approved = decision in ("approve_once", "approve_template", "approve_template_global")
        token = str(uuid.uuid4()) if is_approved else None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, expires_at FROM pending_prompts WHERE id = ?",
                (prompt_id,),
            ).fetchone()
            if row is None:
                return None
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
            return int(cur.lastrowid)

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

        denied_rate = denied / total if total > 0 else 0.0
        prompt_rate = prompted / total if total > 0 else 0.0

        return {
            "total_calls_24h": total,
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
        # Wait for every queued item to be processed before cancelling.
        await self._queue.join()
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None

    async def submit(self, **kwargs: Any) -> None:
        """Enqueue a record for async writing.  Never blocks the caller.

        If the queue is full the record is silently dropped and
        :attr:`dropped_count` is incremented (telemetry is best-effort).
        """
        try:
            self._queue.put_nowait(kwargs)
        except asyncio.QueueFull:
            self.dropped_count += 1
            logger.warning(
                "TelemetryWriter queue full — dropped telemetry record (total dropped: %d)",
                self.dropped_count,
            )

    async def _drain_loop(self) -> None:
        """Continuously drain the queue, writing each record to SQLite off-thread."""
        while True:
            try:
                record = await self._queue.get()
            except asyncio.CancelledError:
                break
            try:
                await asyncio.to_thread(self._persistence.record_call, **record)
            except Exception:
                logger.exception("TelemetryWriter: error writing telemetry record; dropping")
            finally:
                self._queue.task_done()
