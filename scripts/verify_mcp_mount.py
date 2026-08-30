#!/usr/bin/env python3
"""Verify /health and /mcp endpoints respond on port 4112."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

# Resolve src/ path so uvicorn subprocess can import shell_runner
_src = str(Path(__file__).parent.parent / "src")
_env = {**os.environ, "SHELL_RUNNER_DB": "/tmp/sr-verify.sqlite3", "PYTHONPATH": _src}

# Start uvicorn on 4112 in the background
proc = subprocess.Popen(
    [
        sys.executable,
        "-m",
        "uvicorn",
        "shell_runner.server:app",
        "--host",
        "127.0.0.1",
        "--port",
        "4112",
        "--log-level",
        "warning",
    ],
    env=_env,
)

try:
    # Wait for startup
    for _ in range(20):
        time.sleep(0.3)
        try:
            r = httpx.get("http://127.0.0.1:4112/health", timeout=1.0)
            if r.status_code == 200:
                break
        except Exception:
            pass
    else:
        print("ERROR: server did not start in time", file=sys.stderr)
        sys.exit(1)

    # Verify /health
    r = httpx.get("http://127.0.0.1:4112/health", timeout=5.0)
    assert r.status_code == 200, f"/health returned {r.status_code}"
    body = r.json()
    assert body["status"] == "ok", f"/health body: {body}"
    print(f"[OK] /health => {json.dumps(body)}")

    # Verify /mcp — POST an MCP initialize request; FastMCP should respond (not 404)
    r2 = httpx.post(
        "http://127.0.0.1:4112/mcp",
        json={"jsonrpc": "2.0", "method": "initialize", "params": {}, "id": 1},
        headers={"Content-Type": "application/json"},
        timeout=5.0,
    )
    assert r2.status_code != 404, f"/mcp returned 404 — mount failed"
    print(f"[OK] /mcp => HTTP {r2.status_code} (not 404)")
    print(f"     body: {r2.text[:200]}")

finally:
    proc.terminate()
    proc.wait(timeout=5)
