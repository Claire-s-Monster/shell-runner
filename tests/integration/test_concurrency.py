"""Concurrency safety tests for shell-runner under parallel sessions."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import shell_runner.server as srv
from shell_runner.persistence import Persistence


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.sqlite3"


@pytest.fixture
def client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient backed by a fresh per-test DB."""
    fresh = Persistence(db_path=db_path)
    monkeypatch.setattr(srv, "db", fresh)
    monkeypatch.setenv("SHELL_RUNNER_DB", str(db_path))
    return TestClient(srv.app)


def test_concurrent_executes(client: TestClient, tmp_path: Path) -> None:
    """20 concurrent /execute calls must not corrupt the DB."""
    results: list[dict] = []
    lock = threading.Lock()

    def call() -> None:
        r = client.post(
            "/execute",
            json={
                "command": "echo hello",
                "cwd": str(tmp_path),
                "agent_id": "focused-ghc-ci-analyzer",
            },
        )
        with lock:
            results.append(r.json())

    threads = [threading.Thread(target=call) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 20
    assert all(r["decision"] == "executed" for r in results), results
    assert all(r["exit_code"] == 0 for r in results), results
    # All telemetry_ids must be unique
    ids = [r["telemetry_id"] for r in results]
    assert len(set(ids)) == 20, f"duplicate telemetry IDs: {ids}"


def test_concurrent_approve_token_consume(
    client: TestClient, tmp_path: Path, db_path: Path
) -> None:
    """Same approve_token consumed twice concurrently — exactly one succeeds."""
    # Create a pending prompt via T4 command
    r = client.post(
        "/execute",
        json={
            "command": "pip install requests",
            "cwd": str(tmp_path),
            "agent_id": "focused-ghc-ci-analyzer",
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["decision"] == "prompt_required", data
    prompt_id = data["prompt"]["id"]

    # Approve it to get a token
    r2 = client.post(
        "/approve_pending",
        json={"prompt_id": prompt_id, "decision": "approve_once"},
    )
    assert r2.status_code == 200
    token = r2.json()["approve_token"]
    assert token is not None

    # Two threads try to consume the same token simultaneously
    results: list[tuple[int, dict | None]] = []
    lock = threading.Lock()

    def consume() -> None:
        r = client.post(
            "/execute",
            json={
                "command": "pip install requests",
                "cwd": str(tmp_path),
                "agent_id": "focused-ghc-ci-analyzer",
                "approve_token": token,
            },
        )
        body = r.json() if r.status_code < 500 else None
        with lock:
            results.append((r.status_code, body))

    threads = [threading.Thread(target=consume) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    success_count = sum(1 for sc, _ in results if sc == 200)
    failure_count = sum(1 for sc, _ in results if sc == 403)
    assert success_count == 1, f"expected exactly one success, got {success_count}: {results}"
    assert failure_count == 1, f"expected exactly one 403, got {failure_count}: {results}"


def test_concurrent_template_upserts(
    client: TestClient, tmp_path: Path, db_path: Path
) -> None:
    """15 concurrent agents hitting the same command keep a consistent template count."""

    def call(agent_id: str) -> None:
        client.post(
            "/execute",
            json={
                "command": "ls /tmp",
                "cwd": str(tmp_path),
                "agent_id": agent_id,
            },
        )

    threads = [threading.Thread(target=call, args=(f"agent-{i}",)) for i in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Query via db_path directly (bypasses env var resolution)
    p = Persistence(db_path)
    with p._conn() as c:
        # The normalizer may replace /tmp with a placeholder; query for any template
        # that contains "ls" to find the row regardless of exact normalization.
        row = c.execute(
            "SELECT template, observed_count, observed_agents_json"
            " FROM templates WHERE template LIKE 'ls%'",
        ).fetchone()

    assert row is not None, "template row missing — no 'ls' template found in DB"
    agents = json.loads(row["observed_agents_json"])
    assert row["observed_count"] >= 15, (
        f"observed_count={row['observed_count']} < 15 (template={row['template']!r})"
    )
    assert len(set(agents)) == 15, (
        f"expected 15 distinct agents, got {len(set(agents))}: {agents}"
    )
