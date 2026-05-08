#!/usr/bin/env bash
#
# cwd-roots.sh — Manage the shell-runner CWD allow-list without manual TOML editing.
#
# Usage:
#   cwd-roots.sh reload          # SIGHUP the daemon (re-read TOML)
#   cwd-roots.sh list            # print current TOML contents
#   cwd-roots.sh status          # show daemon pid + child uvicorn pid + TOML
#   cwd-roots.sh add PATH        # add PATH to TOML (idempotent), then reload
#   cwd-roots.sh remove PATH     # remove PATH from TOML, then reload
#   cwd-roots.sh -h | --help

set -euo pipefail

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

TOML_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/shell-runner"
TOML_FILE="${TOML_DIR}/cwd-roots.toml"
PID_FILE="${XDG_RUNTIME_DIR:-/tmp}/shell-runner.pid"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

usage() {
    cat <<'EOF'
Usage: cwd-roots.sh <subcommand> [PATH]

Subcommands:
  reload          Send SIGHUP to the daemon (re-read TOML without restart)
  list            Print current TOML contents
  status          Show daemon PID, uvicorn child PID, and TOML contents
  add PATH        Add PATH to the allow-list (idempotent), then reload
  remove PATH     Remove PATH from the allow-list, then reload
  -h, --help      Show this help message

Paths:
  TOML config : ${XDG_CONFIG_HOME:-$HOME/.config}/shell-runner/cwd-roots.toml
  PID file    : ${XDG_RUNTIME_DIR:-/tmp}/shell-runner.pid
EOF
}

# Ensure TOML directory and file exist (empty roots list).
ensure_toml() {
    mkdir -p "${TOML_DIR}"
    if [ ! -f "${TOML_FILE}" ]; then
        printf 'roots = []\n' > "${TOML_FILE}"
    fi
}

# Read parent PID from PID file; exit 2 if missing.
get_parent_pid() {
    if [ ! -f "${PID_FILE}" ]; then
        echo "ERROR: PID file not found: ${PID_FILE}" >&2
        echo "Is the shell-runner daemon running?" >&2
        exit 2
    fi
    cat "${PID_FILE}"
}

# Find uvicorn child PID of parent; fall back to parent if not found.
get_signal_target() {
    local parent_pid="$1"
    local child_pid
    child_pid="$(pgrep -P "${parent_pid}" -f uvicorn 2>/dev/null || true)"
    if [ -n "${child_pid}" ]; then
        echo "${child_pid}"
    else
        echo "${parent_pid}"
    fi
}

# Send SIGHUP to the appropriate PID.
do_reload() {
    local parent_pid
    parent_pid="$(get_parent_pid)"
    local target_pid
    target_pid="$(get_signal_target "${parent_pid}")"
    kill -HUP "${target_pid}"
    if [ "${target_pid}" = "${parent_pid}" ]; then
        echo "Sent SIGHUP to parent PID ${target_pid} (no uvicorn child found)"
    else
        echo "Sent SIGHUP to uvicorn child PID ${target_pid} (parent: ${parent_pid})"
    fi
}

# ---------------------------------------------------------------------------
# TOML round-trip via Python
# ---------------------------------------------------------------------------

toml_mutate() {
    local action="$1"
    local path="$2"
    python3 - "${action}" "${path}" "${TOML_FILE}" <<'PY'
import sys, pathlib

action, path, toml_path = sys.argv[1:4]
p = pathlib.Path(toml_path)

try:
    import tomllib
    data = tomllib.loads(p.read_text()) if p.exists() else {}
except ModuleNotFoundError:
    # Python < 3.11 fallback (tomli not required; simple parser for list-only TOML)
    data = {}
    if p.exists():
        import re
        text = p.read_text()
        matches = re.findall(r'"([^"]+)"', text)
        data["roots"] = matches

roots = list(dict.fromkeys(data.get("roots", [])))  # dedup, preserve order

if action == "add":
    if path not in roots:
        roots.append(path)
elif action == "remove":
    roots = [r for r in roots if r != path]

# Write back as canonical TOML
lines = ["roots = ["]
for r in roots:
    lines.append(f'  "{r}",')
lines.append("]")
p.write_text("\n".join(lines) + "\n")
PY
}

# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

cmd_reload() {
    do_reload
}

cmd_list() {
    ensure_toml
    cat "${TOML_FILE}"
}

cmd_status() {
    local parent_pid=""
    local child_pid=""

    if [ -f "${PID_FILE}" ]; then
        parent_pid="$(cat "${PID_FILE}")"
        child_pid="$(pgrep -P "${parent_pid}" -f uvicorn 2>/dev/null || true)"
    fi

    echo "=== Daemon PIDs ==="
    if [ -n "${parent_pid}" ]; then
        echo "  Parent PID : ${parent_pid}"
        if [ -n "${child_pid}" ]; then
            echo "  Uvicorn PID: ${child_pid}"
        else
            echo "  Uvicorn PID: (not found — parent will receive SIGHUP directly)"
        fi
    else
        echo "  PID file not found: ${PID_FILE}"
        echo "  (daemon may not be running)"
    fi

    echo ""
    echo "=== TOML Allow-List (${TOML_FILE}) ==="
    if [ -f "${TOML_FILE}" ]; then
        cat "${TOML_FILE}"
    else
        echo "  (file does not exist; run 'add' to create it)"
    fi
}

cmd_add() {
    local raw_path="${1:-}"
    if [ -z "${raw_path}" ]; then
        echo "ERROR: 'add' requires a PATH argument" >&2
        usage
        exit 2
    fi

    local resolved
    resolved="$(realpath "${raw_path}" 2>/dev/null || true)"
    if [ -z "${resolved}" ] || [ ! -d "${resolved}" ]; then
        echo "ERROR: directory does not exist or cannot be resolved: ${raw_path}" >&2
        exit 1
    fi

    ensure_toml
    toml_mutate "add" "${resolved}"
    echo "Added: ${resolved}"
    do_reload
}

cmd_remove() {
    local raw_path="${1:-}"
    if [ -z "${raw_path}" ]; then
        echo "ERROR: 'remove' requires a PATH argument" >&2
        usage
        exit 2
    fi

    local resolved
    resolved="$(realpath "${raw_path}" 2>/dev/null || echo "${raw_path}")"

    ensure_toml
    toml_mutate "remove" "${resolved}"
    echo "Removed (if present): ${resolved}"
    do_reload
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

ACTION="${1:-}"

case "${ACTION}" in
    reload)
        cmd_reload
        ;;
    list)
        cmd_list
        ;;
    status)
        cmd_status
        ;;
    add)
        cmd_add "${2:-}"
        ;;
    remove)
        cmd_remove "${2:-}"
        ;;
    -h|--help)
        usage
        exit 0
        ;;
    "")
        usage
        exit 2
        ;;
    *)
        echo "ERROR: unknown subcommand: ${ACTION}" >&2
        usage
        exit 2
        ;;
esac
