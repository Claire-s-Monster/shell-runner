"""Integration tests for shell-runner MCP wrapper (discover/get_spec/execute_tool).

The HTTP backend is the real FastAPI app wired in-process via httpx.ASGITransport,
so the full classifier + persistence stack runs — no mocking of business logic.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import shell_runner.mcp_wrapper as mcp_mod
from shell_runner.mcp_wrapper import TOOLS, discover_tools, execute_tool, get_tool_spec
from shell_runner.persistence import Persistence
from shell_runner.server import app


@pytest.fixture(autouse=True)
def _fresh_db_per_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the module-level db singleton for a fresh per-test instance."""
    fresh = Persistence(db_path=tmp_path / "test.sqlite3")
    monkeypatch.setattr("shell_runner.server.db", fresh)


@pytest.fixture
def asgi_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Replace the wrapper's _client() with a TestClient backed by the in-process ASGI app.

    TestClient (starlette/fastapi) handles the async/sync bridge via anyio, so it
    works correctly with synchronous httpx usage (no ASGITransport needed).
    """
    client = TestClient(app)
    monkeypatch.setattr(mcp_mod, "_client", lambda: client)
    yield client


# ---------------------------------------------------------------------------
# discover_tools — no HTTP needed
# ---------------------------------------------------------------------------


def test_discover_tools_returns_all() -> None:
    r = discover_tools()
    assert r["total_tools"] == 4
    names = {t["name"] for t in r["available_tools"]}
    assert names == {"shell_execute", "shell_classify", "shell_approve_pending", "shell_health"}


def test_discover_tools_with_pattern() -> None:
    r = discover_tools("execute")
    assert r["filtered_count"] == 1
    assert r["available_tools"][0]["name"] == "shell_execute"


def test_discover_tools_empty_pattern_returns_all() -> None:
    r = discover_tools("")
    assert r["filtered_count"] == r["total_tools"]


# ---------------------------------------------------------------------------
# get_tool_spec — no HTTP needed
# ---------------------------------------------------------------------------


def test_get_tool_spec_known() -> None:
    r = get_tool_spec("shell_execute")
    assert r["name"] == "shell_execute"
    assert "command" in r["schema"]["properties"]
    assert "cwd" in r["schema"]["required"]
    assert "agent_id" in r["schema"]["required"]


def test_get_tool_spec_unknown() -> None:
    r = get_tool_spec("nonexistent")
    assert "error" in r
    assert "available" in r
    assert "nonexistent" in r["error"]


# ---------------------------------------------------------------------------
# execute_tool — unknown tool (no HTTP needed)
# ---------------------------------------------------------------------------


def test_execute_tool_unknown() -> None:
    r = execute_tool("nonexistent", {})
    assert "error" in r


# ---------------------------------------------------------------------------
# execute_tool — live HTTP via ASGI transport
# ---------------------------------------------------------------------------


def test_execute_tool_health(asgi_client: TestClient) -> None:
    r = execute_tool("shell_health", {})
    assert r["status"] == "ok"
    assert isinstance(r["catalog_size"], int)
    assert r["catalog_size"] == 100


def test_execute_tool_classify_safe_cmd(asgi_client: TestClient) -> None:
    r = execute_tool(
        "shell_classify",
        {"command": "ls /tmp", "cwd": "/tmp", "agent_id": "focused-ghc-ci-analyzer"},
    )
    assert r["decision_preview"] == "would_execute"


def test_execute_tool_execute_safe_cmd(asgi_client: TestClient, tmp_path: Path) -> None:
    r = execute_tool(
        "shell_execute",
        {
            "command": "echo hello",
            "cwd": str(tmp_path),
            "agent_id": "focused-ghc-ci-analyzer",
        },
    )
    assert r["decision"] == "executed"
    assert r["exit_code"] == 0
    assert "hello" in r["stdout"]


def test_execute_tool_execute_denied(asgi_client: TestClient) -> None:
    r = execute_tool(
        "shell_execute",
        {"command": "rm -rf /", "cwd": "/tmp", "agent_id": "focused-ghc-ci-analyzer"},
    )
    assert r["decision"] == "denied"


def test_execute_tool_execute_prompt_required(asgi_client: TestClient, tmp_path: Path) -> None:
    r = execute_tool(
        "shell_execute",
        {
            "command": "pip install requests",
            "cwd": str(tmp_path),
            "agent_id": "focused-ghc-ci-analyzer",
        },
    )
    assert r["decision"] == "prompt_required"
    assert r["prompt"] is not None
    assert "id" in r["prompt"]
