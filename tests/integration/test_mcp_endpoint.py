"""Integration tests for shell-runner /mcp JSON-RPC 2.0 endpoint.

All tests use the FastAPI TestClient (in-process ASGI) so the full
classifier + persistence stack runs — no mocking of business logic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shell_runner.persistence import Persistence
from shell_runner.server import app


@pytest.fixture(autouse=True)
def _fresh_db_per_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the module-level db singleton for a fresh per-test instance."""
    fresh = Persistence(db_path=tmp_path / "test.sqlite3")
    monkeypatch.setattr("shell_runner.server.db", fresh)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _post(client: TestClient, method: str, params: dict | None = None, req_id: int = 1) -> dict:
    body: dict = {"jsonrpc": "2.0", "method": method, "id": req_id}
    if params is not None:
        body["params"] = params
    r = client.post("/mcp", json=body)
    assert r.status_code == 200
    return r.json()


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------


def test_initialize_returns_server_info(client: TestClient) -> None:
    resp = _post(client, "initialize", params={}, req_id=1)
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    result = resp["result"]
    assert result["serverInfo"]["name"] == "shell-runner"
    assert result["serverInfo"]["version"] == "0.1.0"
    assert "protocolVersion" in result


def test_initialize_sets_protocol_version_header(client: TestClient) -> None:
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize", "id": 1})
    assert "MCP-Protocol-Version" in r.headers


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------


def test_tools_list_returns_seven_tools(client: TestClient) -> None:
    resp = _post(client, "tools/list", req_id=2)
    tools = resp["result"]["tools"]
    assert len(tools) == 7
    names = {t["name"] for t in tools}
    assert names == {
        "shell_execute",
        "shell_classify",
        "shell_approve_pending",
        "shell_get_pending",
        "shell_health",
        "shell_status",
        "shell_kill",
    }


def test_tools_list_includes_input_schemas(client: TestClient) -> None:
    resp = _post(client, "tools/list", req_id=2)
    execute_tool = next(t for t in resp["result"]["tools"] if t["name"] == "shell_execute")
    assert "inputSchema" in execute_tool
    assert "command" in execute_tool["inputSchema"]["properties"]


# ---------------------------------------------------------------------------
# tools/call — shell_health
# ---------------------------------------------------------------------------


def test_tools_call_shell_health(client: TestClient) -> None:
    resp = _post(
        client,
        "tools/call",
        params={"name": "shell_health", "arguments": {}},
        req_id=3,
    )
    content = resp["result"]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"
    payload = json.loads(content[0]["text"])
    assert payload["status"] == "ok"
    assert isinstance(payload["catalog_size"], int)


# ---------------------------------------------------------------------------
# tools/call — shell_execute
# ---------------------------------------------------------------------------


def test_tools_call_shell_execute_safe_cmd(client: TestClient, tmp_path: Path) -> None:
    resp = _post(
        client,
        "tools/call",
        params={
            "name": "shell_execute",
            "arguments": {
                "command": "echo hello",
                "cwd": str(tmp_path),
                "agent_id": "focused-ghc-ci-analyzer",
            },
        },
        req_id=4,
    )
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["decision"] == "executed"
    assert payload["exit_code"] == 0
    assert "hello" in payload["stdout"]


def test_tools_call_shell_execute_returns_structured_error_on_exception(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unhandled exception in execute_route must not surface as a bare 500.

    _dispatch_tool_call should catch it and return a structured error dict
    (issue #26 fix).
    """

    def _boom(request, exec_req):  # noqa: ANN001, ARG001
        raise RuntimeError("boom")

    monkeypatch.setattr("shell_runner.server.execute_route", _boom)

    resp = _post(
        client,
        "tools/call",
        params={
            "name": "shell_execute",
            "arguments": {
                "command": "echo hello",
                "cwd": str(tmp_path),
                "agent_id": "focused-ghc-ci-analyzer",
            },
        },
        req_id=4,
    )
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert "error" in payload
    assert "boom" in payload["error"]


def test_tools_call_shell_execute_denied(client: TestClient) -> None:
    resp = _post(
        client,
        "tools/call",
        params={
            "name": "shell_execute",
            "arguments": {
                "command": "rm -rf /",
                "cwd": "/tmp",
                "agent_id": "focused-ghc-ci-analyzer",
            },
        },
        req_id=4,
    )
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["decision"] == "denied"


# ---------------------------------------------------------------------------
# unknown method → -32601
# ---------------------------------------------------------------------------


def test_unknown_method_returns_error(client: TestClient) -> None:
    resp = _post(client, "unknown_method", req_id=5)
    assert "error" in resp
    assert resp["error"]["code"] == -32601
    assert resp["id"] == 5


# ---------------------------------------------------------------------------
# ping / notifications
# ---------------------------------------------------------------------------


def test_ping_returns_empty_result(client: TestClient) -> None:
    resp = _post(client, "ping", req_id=6)
    assert resp["result"] == {}


def test_notifications_initialized_returns_empty_result(client: TestClient) -> None:
    resp = _post(client, "notifications/initialized", req_id=7)
    assert resp["result"] == {}


# ---------------------------------------------------------------------------
# malformed JSON
# ---------------------------------------------------------------------------


def test_malformed_json_returns_parse_error(client: TestClient) -> None:
    r = client.post("/mcp", content=b"not-json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    data = r.json()
    assert data["error"]["code"] == -32700


# ---------------------------------------------------------------------------
# GET /mcp SSE endpoint
# ---------------------------------------------------------------------------


def test_get_mcp_route_exists_not_405(client: TestClient) -> None:
    """GET /mcp must be a registered route (not return 405 Method Not Allowed).

    Starlette's TestClient buffers the full response body before returning, so
    we cannot use client.stream() against an infinite SSE generator in-process.
    Instead, we verify the route is registered by asking Starlette's router
    directly, confirming Claude Code's client won't receive a 405.
    """
    from starlette.routing import Match

    scope = {"type": "http", "method": "GET", "path": "/mcp"}
    matched = any(
        route.matches(scope)[0] == Match.FULL
        for route in client.app.routes  # type: ignore[attr-defined]
    )
    assert matched, "GET /mcp route is not registered — Claude Code client will receive 405"


# ---------------------------------------------------------------------------
# resources/* handlers
# ---------------------------------------------------------------------------


def test_resources_list_returns_empty(client: TestClient) -> None:
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "resources/list", "id": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == {"resources": []}


def test_resources_templates_list_returns_empty(client: TestClient) -> None:
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "resources/templates/list", "id": 1})
    assert r.status_code == 200
    assert r.json()["result"] == {"resourceTemplates": []}


def test_resources_read_returns_error(client: TestClient) -> None:
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "resources/read", "id": 1})
    assert r.status_code == 200
    body = r.json()
    assert "error" in body
    assert body["error"]["code"] == -32602


# ---------------------------------------------------------------------------
# prompts/* handlers
# ---------------------------------------------------------------------------


def test_prompts_list_returns_empty(client: TestClient) -> None:
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "prompts/list", "id": 1})
    assert r.status_code == 200
    assert r.json()["result"] == {"prompts": []}


def test_prompts_get_returns_error(client: TestClient) -> None:
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "prompts/get", "id": 1})
    assert r.status_code == 200
    body = r.json()
    assert "error" in body
    assert body["error"]["code"] == -32602


# ---------------------------------------------------------------------------
# initialize — extended capabilities
# ---------------------------------------------------------------------------


def test_initialize_advertises_resources_and_prompts_capabilities(client: TestClient) -> None:
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize", "id": 1})
    assert r.status_code == 200
    caps = r.json()["result"]["capabilities"]
    assert "tools" in caps
    assert "resources" in caps
    assert "prompts" in caps
