#!/usr/bin/env bash
# Restart shell-runner.service from outside its own process, verify health, and
# record the outcome for the next session. Launched by the mcp_restart tool via
# `systemd-run --user --on-active=2` (issue #56). Invoke with `bash` (no exec bit needed).
#
# The state file is written atomically on every exit path (trap EXIT).
set -uo pipefail

UNIT="shell-runner.service"
PORT="${SHELL_RUNNER_PORT:-4111}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/shell-runner"
STATE_FILE="$STATE_DIR/last-restart.json"
MAX_ATTEMPTS="${SHELL_RUNNER_RESTART_MAX_ATTEMPTS:-60}"

restart_rc=-1
ok=false
attempts=0

write_state() {
    local tail ts tmp
    mkdir -p "$STATE_DIR" 2>/dev/null || return 0
    tmp="$STATE_FILE.tmp.$$"
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)
    tail=$(journalctl --user -u "$UNIT" -n 40 --no-pager 2>&1 || true)
    if ! python3 - "$tmp" "$ok" "$attempts" "$restart_rc" "$ts" "$tail" 2>/dev/null <<'PY'
import json
import sys

path, ok, attempts, rc, ts, tail = sys.argv[1:7]
record = {
    "timestamp": ts,
    "ok": ok == "true",
    "attempts": int(attempts),
    "restart_rc": int(rc),
    "journal_tail": tail,
}
with open(path, "w") as fh:
    json.dump(record, fh, indent=2)
PY
    then
        printf '{"timestamp":"%s","ok":%s,"attempts":%s,"restart_rc":%s}\n' \
            "$ts" "$ok" "$attempts" "$restart_rc" >"$tmp" 2>/dev/null || return 0
    fi
    mv -f "$tmp" "$STATE_FILE" 2>/dev/null || true
}
trap write_state EXIT

systemctl --user restart "$UNIT"
restart_rc=$?

while [ "$attempts" -lt "$MAX_ATTEMPTS" ]; do
    attempts=$((attempts + 1))
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 -X POST \
        -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"self-restart","version":"1"}}}' \
        "http://127.0.0.1:${PORT}/mcp" || true)
    if [ "$code" = "200" ]; then
        if [ "$restart_rc" -eq 0 ]; then
            ok=true
        fi
        break
    fi
    sleep 1
done

[ "$ok" = "true" ]
