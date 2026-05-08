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
