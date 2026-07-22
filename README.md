# shell-runner

Sandboxed shell execution MCP server with telemetry and adaptive allowlist learning.

## Overview

`shell-runner` is an HTTP MCP server that provides sandboxed shell execution with:
- Command normalization for consistent telemetry
- Adaptive allowlist learning from execution patterns
- Command classification against known-safe patterns

## Development

This project uses [pixi](https://pixi.sh) for environment management.

```bash
# Install environment
pixi install

# Run tests
pixi run test

# Run all quality checks
pixi run check-all
```

## Available Tasks

| Task | Description |
|------|-------------|
| `test` | Run tests (excludes live and debug) |
| `test-unit` | Run unit tests only |
| `test-cov` | Run tests with coverage report |
| `lint` | Run ruff linter with auto-fix |
| `format` | Run black formatter |
| `type-check` | Run mypy type checker |
| `quality` | Run lint + type-check |
| `check-all` | Run test + quality |
| `build` | Build distribution package |
| `http-server` | Start the HTTP MCP server |
| `ui` | Launch the Streamlit approval/admin UI (pixi env `ui`) |

## Admin / Approval UI

`shell-runner` ships a Streamlit-based admin UI (`ui/app.py`) for reviewing
telemetry and acting on pending T3/T4 approval prompts. It lives in the `ui`
pixi environment.

```bash
# Launch the UI (binds 127.0.0.1:8511)
pixi run -e ui ui
```

Pages:
- **Dashboard** — recent shell calls read directly from the telemetry DB.
- **Pending Prompts** — non-expired, unapproved prompts; approve
  (`approve_once` / `approve_template` / `approve_template_global`) or deny.

### Rule refinement (Pending Prompts page)

Each pending prompt shows **why** it needs approval and lets you propose a
classifier-rule improvement as a GitHub issue — nothing is applied live.

- **Why this needs approval** — the classifier's decision path and matched rule
  category (from stored telemetry; no external calls).
- **Similar past approvals** — previously-approved templates that resemble this
  command (verb-anchored similarity). Example commands are shown redacted.
- **🔍 Propose rule enhancement** — runs a strictly **read-only** Claude session
  in the repo (Read/Grep/Glob only; it cannot edit files, run shell, or call any
  MCP write tool, and the repo's `.mcp.json` is not loaded). It explains why the
  command missed the auto-execute rules and returns a proposed catalog rule or an
  existing-lever recommendation. You review the **redacted** command and proposal,
  then **File GitHub issue** files it under the `rule-enhancement` label for local
  review and a follow-up PR to `src/shell_runner/catalog.py`.

Secrets in the command are redacted before anything is sent to Claude or written
into an issue.

**Configuration (environment variables):**

| Var | Default | Purpose |
| --- | --- | --- |
| `SHELL_RUNNER_CLAUDE_BIN` | `claude` | Claude CLI used for analysis |
| `SHELL_RUNNER_ANALYSIS_TIMEOUT_S` | `180` | Analysis subprocess timeout (seconds) |
| `SHELL_RUNNER_GH_REPO` | `Claire-s-Monster/shell-runner` | Repo issues are filed against |
| `SHELL_RUNNER_GH_TOKEN` | falls back to `GITHUB_TOKEN` | Token with `issues:write`; if unset, filing is disabled |
| `SHELL_RUNNER_REPO_DIR` | UI repo root | Directory the read-only Claude session runs in |

Approvals from this UI POST to the server's `/approve_pending` endpoint with
`approver_agent_id="primary"`, which engages the Layer-1 approver-identity guard
(see [#29](https://github.com/Claire-s-Monster/shell-runner/issues/29)):
`"primary"` resolves to DENY capability and self-approval is rejected. The UI
reads the telemetry DB read-only and talks to the server at
`$SHELL_RUNNER_API_URL` (default `http://127.0.0.1:4111`).

## Approval precedence & agent_cap

When a command is classified, `_apply_template_approval` (server.py) may
override the catalog-derived tier using persisted approvals, in this order:

1. **DENY (T0) always wins.** A hard-denied command can never be promoted by
   any approval, exact-template or verb-level. This is a security invariant.
2. **Exact-template promotion** (`approve_template` / `approve_template_global`)
   — keyed on the full normalized command template. Checked first.
3. **Verb + cwd-prefix promotion** (`approve_verb`) — keyed on just the
   command's verb and a cwd subtree, checked only when the exact-template
   lookup misses. This collapses command-variant re-escalation: once a verb
   (e.g. `git`, `pytest`) is approved for a directory subtree, later
   invocations with different arguments no longer re-prompt just because
   their exact normalized template differs from the one originally approved.
4. **Catalog rule** — the base tier from `catalog.py` applies when neither
   promotion hits.

Whichever tier results (promoted or catalog), the agent's `agent_cap` is
re-applied via the PERMISSIVENESS cap-min formula (`_cap_permissiveness`).
This means an `AUTO_CAPPED`-capped agent can still only receive
`APPROVE_ONCE` for a command whose promoted tier (e.g. `AUTO_LOG`) exceeds
its cap — promotion raises the *command's* tier, but the agent's own trust
ceiling is never bypassed.

`/approve_pending` (`shell_approve_pending`) accepts an optional
`approver_agent_id`. When supplied, the server checks it as defense-in-depth:
self-approval (approver == the prompt's executing agent) and non-primary
(non-`DENY`-cap) approvers are rejected with 403. This is a mitigation only —
`approver_agent_id` is self-asserted, not cryptographically authenticated.
Full authenticated-identity enforcement of "primary session only" is a known
limitation tracked in [#29](https://github.com/Claire-s-Monster/shell-runner/issues/29).

## License

MIT License - see [LICENSE](LICENSE) for details.
