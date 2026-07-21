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
