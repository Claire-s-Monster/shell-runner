# CWD Allow-List — Multi-Root Configuration

Shell-runner restricts command execution to directories that live under one or
more *allowed roots*. This document explains how to configure those roots and
reload them without restarting the server.

## Why Multi-Root?

A single `SHELL_RUNNER_CWD_ROOT` forces you to restart the server whenever you
start working in a new project or git worktree. The multi-root allow-list lets
you add new paths at runtime: edit a config file, send `SIGHUP`, done.

## Configuration Sources

All three sources are **unioned and deduplicated** at startup and on every
SIGHUP reload. No source overrides another; they are merged.

### 1. TOML config file (recommended)

`${XDG_CONFIG_HOME:-$HOME/.config}/shell-runner/cwd-roots.toml`

```toml
roots = [
  "/home/you/projects/my-project",
  "/home/you/worktrees",
]
```

Copy `config/cwd-roots.example.toml` as a starting point.

### 2. `SHELL_RUNNER_CWD_ROOTS` env var

Colon-separated list, like the `PATH` variable:

```bash
export SHELL_RUNNER_CWD_ROOTS="/home/you/projects:/home/you/worktrees"
```

### 3. `SHELL_RUNNER_CWD_ROOT` env var (legacy)

Single path — kept for backwards compatibility with existing deployments:

```bash
export SHELL_RUNNER_CWD_ROOT="/home/you/projects"
```

### Fallback

If none of the above are set the server falls back to the process working
directory at startup time.

## SIGHUP Reload

Send `SIGHUP` to the running daemon to reload the allow-list without
restarting:

```bash
kill -HUP "$(cat "${XDG_RUNTIME_DIR:-/tmp}/shell-runner.pid")"
```

The server logs the new root list at `INFO` level:

```
reloaded cwd-roots: ['/home/you/projects/my-project', '/home/you/worktrees']
```

## SessionStart Hook — Auto-Register Projects (Claude Code)

Add the following snippet to your Claude Code `SessionStart` hook so that the
current project directory is registered automatically each time you open a new
session:

```bash
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/shell-runner"
config="${XDG_CONFIG_HOME:-$HOME/.config}/shell-runner/cwd-roots.toml"
if ! grep -qF "\"$CLAUDE_PROJECT_DIR\"" "$config" 2>/dev/null; then
    [ -f "$config" ] || echo "roots = []" > "$config"
    # naive append; replace with toml-aware tool if you want
    sed -i "s|^roots = \[|roots = [\n  \"$CLAUDE_PROJECT_DIR\",|" "$config"
    pid="${XDG_RUNTIME_DIR:-/tmp}/shell-runner.pid"
    [ -f "$pid" ] && kill -HUP "$(cat "$pid")"
fi
```

This appends the project path once (idempotent check via `grep -qF`) and sends
a SIGHUP so the change takes effect immediately.

---

## `cwd-roots.sh` Helper Script

`development/scripts/cwd-roots.sh` provides TOML-aware subcommands so you
never need to hand-edit the config file or hunt for the uvicorn child PID.

### Manual usage

```bash
# Add the current project to the allow-list and reload the daemon
./cwd-roots.sh add /home/you/projects/my-project

# Remove a path
./cwd-roots.sh remove /home/you/projects/old-project

# Inspect the current allow-list
./cwd-roots.sh list

# Show daemon PID, uvicorn child PID, and TOML contents
./cwd-roots.sh status

# Reload without modifying the TOML (e.g. after editing it manually)
./cwd-roots.sh reload
```

Exit codes: `0` success, `1` invalid path / missing directory,
`2` missing PID file or bad arguments.

### SessionStart hook (recommended)

Replace the manual `sed` snippet above with the helper for a cleaner,
TOML-safe registration:

```json
"hooks": {
  "SessionStart": [
    {
      "type": "command",
      "command": "\"${HOME}/ClaudeCode/Servers/shell-runner/development/scripts/cwd-roots.sh\" add \"$CLAUDE_PROJECT_DIR\""
    }
  ]
}
```

Or as a one-liner in `.claude/settings.json`:

```bash
"${HOME}/ClaudeCode/Servers/shell-runner/development/scripts/cwd-roots.sh" \
  add "$CLAUDE_PROJECT_DIR"
```

The script uses `realpath` to resolve symlinks, deduplicates entries, and
signals the uvicorn child directly (or falls back to the parent process).
If the daemon is not running the `add` still writes the TOML so it takes
effect on next startup; only the `reload` step is skipped (exit 2, which
you can suppress with `|| true` in the hook).
