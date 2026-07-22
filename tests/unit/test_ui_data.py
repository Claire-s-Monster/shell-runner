"""Unit tests for ui/data.py helper functions.

Uses a tmp SQLite DB with the real schema from persistence.py.
"""

import sqlite3
from pathlib import Path

import pytest

from ui.data import get_health, get_pending_prompts, get_recent_calls, parse_decision_path


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "telemetry.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE shell_calls (
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
        INSERT INTO shell_calls
            (id, ts, agent_id, cwd, raw_cmd, normalized_template,
             command_tier, final_tier, decision, exit_code, duration_ms)
        VALUES
            ('id-1','2026-01-01T00:00:01','agent-a','/tmp','ls','ls',1,1,'executed',0,5),
            ('id-2','2026-01-01T00:00:02','agent-b','/tmp','rm -f x','rm -f <arg>',3,3,'prompt_required',NULL,NULL);

        CREATE TABLE pending_prompts (
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
        INSERT INTO pending_prompts
            (id, created_at, expires_at, agent_id, cwd, raw_cmd, normalized_template, command_tier)
        VALUES
            ('pid-1','2026-01-01T00:00:00',
             strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '+1 hour')),
             'agent','/tmp','rm -rf /','rm -rf <arg>',4),
            ('pid-2','2026-01-01T00:00:00',
             strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-1 hour')),
             'agent','/tmp','old cmd','old <arg>',3),
            ('pid-3','2026-01-01T00:00:00',
             strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '+1 hour')),
             'agent','/tmp','approved cmd','approved <arg>',3);
        -- pid-3: already approved (approve_decision set)
        UPDATE pending_prompts SET approve_decision='approve_once' WHERE id='pid-3';
    """)
    conn.commit()
    conn.close()
    return db


def test_recent_calls_orders_desc(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    rows = get_recent_calls(db_path=db, limit=10)
    assert len(rows) == 2
    assert rows[0]["ts"] > rows[1]["ts"]


def test_recent_calls_limit(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    assert len(get_recent_calls(db_path=db, limit=1)) == 1


def test_recent_calls_missing_db_returns_empty(tmp_path: Path) -> None:
    assert get_recent_calls(db_path=tmp_path / "nope.db") == []


def test_recent_calls_returns_dicts(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    rows = get_recent_calls(db_path=db, limit=1)
    assert isinstance(rows[0], dict)
    assert "decision" in rows[0]


def test_pending_prompts_filters_expired_and_already_approved(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    rows = get_pending_prompts(db_path=db)
    ids = [r["id"] for r in rows]
    assert "pid-1" in ids
    assert "pid-2" not in ids  # expired
    assert "pid-3" not in ids  # approve_decision already set


def test_pending_prompts_missing_db_returns_empty(tmp_path: Path) -> None:
    assert get_pending_prompts(db_path=tmp_path / "nope.db") == []


def test_health_unreachable_returns_error_dict() -> None:
    h = get_health(api_url="http://127.0.0.1:1", timeout=0.5)
    assert h["status"] == "unreachable"
    assert "error" in h


# ---------------------------------------------------------------------------
# approve_prompt — promote_to_tier forwarding
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock, patch  # noqa: E402


def test_approve_prompt_includes_promote_to_tier_when_provided() -> None:
    from ui.data import approve_prompt

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"applied": True}
    mock_resp.raise_for_status = MagicMock()
    with patch("ui.data.httpx.post", return_value=mock_resp) as mock_post:
        approve_prompt("pid-x", "approve_template", promote_to_tier=2)
        assert mock_post.call_args.kwargs["json"]["promote_to_tier"] == 2


def test_approve_prompt_omits_promote_to_tier_when_none() -> None:
    from ui.data import approve_prompt

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"applied": True}
    mock_resp.raise_for_status = MagicMock()
    with patch("ui.data.httpx.post", return_value=mock_resp) as mock_post:
        approve_prompt("pid-x", "approve_once")
        assert "promote_to_tier" not in mock_post.call_args.kwargs["json"]


def test_approve_prompt_sends_global_decision() -> None:
    from ui.data import approve_prompt

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"applied": True}
    mock_resp.raise_for_status = MagicMock()
    with patch("ui.data.httpx.post", return_value=mock_resp) as mock_post:
        approve_prompt("pid-x", "approve_template_global", promote_to_tier=1)
        body = mock_post.call_args.kwargs["json"]
        assert body["decision"] == "approve_template_global"
        assert body["promote_to_tier"] == 1


def test_approve_prompt_sends_approver_agent_id_primary_by_default() -> None:
    from ui.data import approve_prompt

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"applied": True}
    mock_resp.raise_for_status = MagicMock()
    with patch("ui.data.httpx.post", return_value=mock_resp) as mock_post:
        approve_prompt("pid-x", "approve_once")
        # UI is the human/primary approval surface: it must send
        # approver_agent_id="primary" so the server's Layer-1 guard engages (issue #29).
        assert mock_post.call_args.kwargs["json"]["approver_agent_id"] == "primary"


def test_approve_prompt_forwards_custom_approver_agent_id() -> None:
    from ui.data import approve_prompt

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"applied": True}
    mock_resp.raise_for_status = MagicMock()
    with patch("ui.data.httpx.post", return_value=mock_resp) as mock_post:
        approve_prompt("pid-x", "deny", approver_agent_id="reviewer-1")
        assert mock_post.call_args.kwargs["json"]["approver_agent_id"] == "reviewer-1"


# ---------------------------------------------------------------------------
# parse_decision_path
# ---------------------------------------------------------------------------


def test_parse_decision_path_valid_json_list():
    raw = '["normalized: curl <safe_url>", "segment \'curl\': T4 -> ALWAYS_APPROVE"]'
    assert parse_decision_path(raw) == [
        "normalized: curl <safe_url>",
        "segment 'curl': T4 -> ALWAYS_APPROVE",
    ]


def test_parse_decision_path_handles_none_and_garbage():
    assert parse_decision_path(None) == []
    assert parse_decision_path("") == []
    assert parse_decision_path("not json") == []
    assert parse_decision_path('{"not": "a list"}') == []
