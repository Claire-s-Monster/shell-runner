#!/usr/bin/env bash
#
# Daemon management for the shell-runner HTTP server.
#
# Usage:
#   ./shell-runner-daemon.sh start
#   ./shell-runner-daemon.sh stop
#   ./shell-runner-daemon.sh status
#   ./shell-runner-daemon.sh restart
#
# Environment Variables:
#   SHELL_RUNNER_HOST   Host to bind to (default: 127.0.0.1)
#   SHELL_RUNNER_PORT   Port to bind to (default: 4111)
#   SHELL_RUNNER_DB     SQLite database path
#                       (default: $HOME/.local/share/shell-runner/telemetry.sqlite3)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Defaults — override via environment
DEFAULT_HOST="127.0.0.1"
DEFAULT_PORT="4111"
DEFAULT_DB="${HOME}/.local/share/shell-runner/telemetry.sqlite3"

SHELL_RUNNER_HOST="${SHELL_RUNNER_HOST:-$DEFAULT_HOST}"
SHELL_RUNNER_PORT="${SHELL_RUNNER_PORT:-$DEFAULT_PORT}"
SHELL_RUNNER_DB="${SHELL_RUNNER_DB:-$DEFAULT_DB}"

# PID file location — prefer XDG_RUNTIME_DIR when available
if [ -n "${XDG_RUNTIME_DIR:-}" ]; then
    PID_FILE="${XDG_RUNTIME_DIR}/shell-runner.pid"
else
    PID_FILE="/tmp/shell-runner.pid"
fi

# Log directory alongside the DB
LOG_DIR="$(dirname "$SHELL_RUNNER_DB")"
LOG_FILE="${LOG_DIR}/server.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

is_running() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE")
        if ps -p "$pid" > /dev/null 2>&1; then
            return 0
        fi
    fi
    return 1
}

get_pid() {
    if [ -f "$PID_FILE" ]; then
        cat "$PID_FILE"
    else
        echo ""
    fi
}

start_server() {
    if is_running; then
        local pid
        pid=$(get_pid)
        log "Server already running with PID $pid"
        exit 1
    fi

    # Clean up stale PID file
    rm -f "$PID_FILE"

    # Ensure data and log directories exist
    mkdir -p "$LOG_DIR"

    log "Starting shell-runner HTTP server..."
    log "  Host: $SHELL_RUNNER_HOST"
    log "  Port: $SHELL_RUNNER_PORT"
    log "  DB:   $SHELL_RUNNER_DB"

    # Start uvicorn via pixi in the project directory
    cd "$PROJECT_DIR"
    export SHELL_RUNNER_DB SHELL_RUNNER_HOST SHELL_RUNNER_PORT
    export PYTHONPATH="${PROJECT_DIR}/src"
    nohup "${HOME}/.conda/envs/ClaudeCode/bin/pixi" run --environment ci uvicorn shell_runner.server:app \
        --host "$SHELL_RUNNER_HOST" \
        --port "$SHELL_RUNNER_PORT" \
        >> "$LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"

    # Wait briefly for startup
    sleep 2

    if is_running; then
        log "Server started successfully (PID: $pid)"
        log "Log file: $LOG_FILE"
        log ""
        log "Endpoints:"
        log "  POST http://$SHELL_RUNNER_HOST:$SHELL_RUNNER_PORT/execute"
        log "  POST http://$SHELL_RUNNER_HOST:$SHELL_RUNNER_PORT/classify"
        log "  POST http://$SHELL_RUNNER_HOST:$SHELL_RUNNER_PORT/approve_pending"
        log "  GET  http://$SHELL_RUNNER_HOST:$SHELL_RUNNER_PORT/health"
    else
        log "ERROR: Server failed to start. Check log file: $LOG_FILE"
        rm -f "$PID_FILE"
        exit 1
    fi
}

stop_server() {
    if ! is_running; then
        log "Server not running"
        rm -f "$PID_FILE"
        exit 0
    fi

    local pid
    pid=$(get_pid)
    log "Stopping server (PID: $pid)..."

    kill "$pid" 2>/dev/null || true

    # Wait up to 10 seconds for graceful shutdown
    local count=0
    while is_running && [ "$count" -lt 10 ]; do
        sleep 1
        count=$((count + 1))
    done

    if is_running; then
        log "Force killing..."
        kill -9 "$pid" 2>/dev/null || true
        sleep 1
    fi

    rm -f "$PID_FILE"
    log "Server stopped"
}

status_server() {
    if ! is_running; then
        log "Server not running"
        if [ -f "$PID_FILE" ]; then
            log "(Stale PID file found, cleaning up)"
            rm -f "$PID_FILE"
        fi
        exit 1
    fi

    local pid
    pid=$(get_pid)
    log "Server running (PID: $pid)"

    local health_url="http://$SHELL_RUNNER_HOST:$SHELL_RUNNER_PORT/health"
    if command -v curl > /dev/null 2>&1; then
        if curl -fsS "$health_url" > /dev/null 2>&1; then
            log "Health check: OK"
            # Capture response to variable first; python3 -m json.tool only
            # pretty-prints JSON — it does not execute the response as code.
            local health_json
            health_json="$(curl -fsS "$health_url" 2>/dev/null)" || true
            echo "$health_json" | python3 -m json.tool 2>/dev/null || true
        else
            log "Health check: FAILED (server may still be starting)"
        fi
    else
        log "Health check: curl not available"
    fi

    log ""
    log "Log file: $LOG_FILE"
    log "Recent log entries:"
    tail -5 "$LOG_FILE" 2>/dev/null || echo "  (no log entries)"
}

restart_server() {
    log "Restarting server..."
    stop_server || true
    sleep 1
    start_server
}

# Parse subcommand
ACTION="${1:-}"

case "$ACTION" in
    start)
        start_server
        ;;
    stop)
        stop_server
        ;;
    restart)
        restart_server
        ;;
    status)
        status_server
        ;;
    -h|--help|"")
        cat << 'EOF'
Usage: shell-runner-daemon.sh {start|stop|restart|status}

Commands:
  start     Start the HTTP server as a background daemon
  stop      Stop the running server
  restart   Restart the server
  status    Check server status and health endpoint

Environment Variables:
  SHELL_RUNNER_HOST   Bind address (default: 127.0.0.1)
  SHELL_RUNNER_PORT   Port (default: 4111)
  SHELL_RUNNER_DB     SQLite DB path
                      (default: $HOME/.local/share/shell-runner/telemetry.sqlite3)

Examples:
  ./shell-runner-daemon.sh start
  SHELL_RUNNER_PORT=5003 ./shell-runner-daemon.sh start
  ./shell-runner-daemon.sh status
  ./shell-runner-daemon.sh stop
EOF
        [ -z "$ACTION" ] && exit 1 || exit 0
        ;;
    *)
        echo "Unknown command: $ACTION"
        echo "Use --help for usage information"
        exit 1
        ;;
esac
