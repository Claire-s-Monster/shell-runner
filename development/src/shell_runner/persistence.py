"""SQLite persistence layer for shell-runner telemetry and approval flow."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_running_jobs(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status = 'running' ORDER BY started_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def cleanup_old_jobs(self, retention_seconds: int) -> list[dict]:
        """Delete jobs older than retention_seconds (by started_at).

        Returns rows that were removed so the caller can delete stdout/stderr files.
        """
        cutoff = (
            datetime.now(UTC) - timedelta(seconds=retention_seconds)
        ).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE started_at < ?", (cutoff,)
            ).fetchall()
            removed = [dict(r) for r in rows]
            if removed:
                conn.execute("DELETE FROM jobs WHERE started_at < ?", (cutoff,))
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
                "SELECT COUNT(*) as cnt FROM shell_calls WHERE ts >= ?", (since_24h,)
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
