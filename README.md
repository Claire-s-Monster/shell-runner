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

## What auto-executes (no prompt)

Beyond the per-tier seed rules in `catalog.py`, three categories run without a
prompt. In every case a credential path (the `monitor.is_credential_path`
list: `~/.ssh`, `~/.aws`, `~/.gnupg`, `.netrc`, `~/.claude/.credentials.json`,
`.env*`, `*.pem`, `*.key`, ...) is classified `<cred_path>` and never matches
an auto-execute rule, so it keeps prompting for every verb, metadata ones
included.

1. **Read-only inspection** (AUTO_LOG): `ls`, `stat`, `du`, `df`, `file`, `which`/`type`,
   `ps`, `pgrep`, `wc`, `head`, `tail`, `grep`, `diff`/`cmp` (not `-r`), `rg`
   (cwd/tmp only), on any non-credential path, with `2>&1` and the usual safe
   pipe filters. `/etc`, `/proc`, `/sys`, `/dev`, and paths built from `$VAR`
   still prompt.
2. **Leading `cd`** (the tier of the rest): `cd DIR && REST` and
   `cd DIR || exit [n]; REST` (or `|| return`) classify REST as if the cwd were DIR, when DIR exists
   and resolves inside the configured cwd roots (and is not a credential store).
   Plain `cd DIR; REST` is never folded (a failed cd would run REST in the original
   cwd). Chains (`cd a && cd b && X`) fold each hop up to the first non-folding one. The template, and so approvals,
   are REST's; `decision_path` records `cd folded: <dir>`. Execution is unchanged.
3. **Scratch-directory file management** (AUTO_CAPPED): `mkdir`, `touch`, `rm`
   (incl. `-rf`), `cp`, `mv`, `ln -s` and `tar` create/extract when every write
   target really resolves (realpath, so `/tmp/../x` and symlinks do not count)
   to a strict descendant of `/tmp`. Not scratch: `/tmp` itself, hidden or
   service entries (`/tmp/.X11-unix`, `systemd-private-*`, `tmux-*`, `ssh-*`,
   `shell-runner*`), and the bare `/tmp/claude-<uid>` session root (its children
   are). `$TMPDIR` is not a scratch root: it is not forwarded to the executed
   command, where it would expand to an empty string. `cp`/`tar`/`ln` sources may
   be anywhere except credential paths; `mv` sources stay cwd/tmp (it deletes them).

## Audit trail: approver columns, retention, `shell_audit`

**Approver columns.** Every approve *and* deny decision stores the supplied
`approver_agent_id` on the `pending_prompts` row (`approver_agent_id`), and on
the `template_approvals` / `verb_approvals` row it creates or replaces
(`approved_by`). Older databases gain these nullable columns automatically at
startup. Because `approver_agent_id` is self-asserted
([#29](https://github.com/Claire-s-Monster/shell-runner/issues/29)), these
columns record the claim, not a verified identity.

**Retention.** `SHELL_RUNNER_RETENTION_DAYS` (integer, default `14`) sets how
long passive `observed_externally` rows in `shell_calls` are kept (these are
the only rows it prunes, and by far the bulk of the table); `0` or a
negative value keeps everything forever. The legacy `SHELL_CALLS_RETENTION_S`
(seconds) is still honoured when the new variable is unset. The effective value
is logged at startup. It must be at least `SHELL_RUNNER_JOB_RETENTION_S`
(default 7 days). Audit rows (`executed`, `denied`, `prompt_required`,
`running`) are never pruned, regardless of this setting, and neither are
`observed_externally` rows with `flagged = 1` (protected-path writes, which are
trial evidence). Decided prompts
(those carrying an approver) are also kept forever, independent of this
window, because the audit rows they explain are never pruned; undecided
prompts are still removed on expiry.

**`shell_audit`** (MCP tool; HTTP `POST /audit`) is a read-only query over
`shell_calls`. Claude Code caches `tools/list`, so start a new session to see
the tool after the server is updated.

| Parameter | Meaning |
|-----------|---------|
| `agent_id` / `agent_id_prefix` | exact match / case-sensitive prefix |
| `since` / `until` | ISO-8601 or relative (`30m`, `24h`, `7d`, `2w`) |
| `decision` | exact match, e.g. `executed`, `denied`, `prompt_required` |
| `flagged_only` | only flagged calls; needs a `flagged` column, otherwise ignored with a note in `notes` |
| `mode` | `rows` (default) or `summary` |
| `limit` | default 100, capped at 1000 (rows, or agents in summary mode) |

`rows` returns `ts, agent_id, cwd, raw_cmd, command_tier, final_tier, decision,
matched_rule_category, matched_rule_pattern, exit_code` plus `approver_agent_id`
and `approve_decision` from the decided prompt with the same
agent_id + raw_cmd + cwd whose lifetime covers the call (null if none).
`summary` returns per `agent_id`: `total_calls`, `by_decision`, `denied_count`,
`prompt_count`, `first_ts`, `last_ts`.

## Rules files

User-configurable deny rules and protected-path rules, loaded from TOML files.
Intended for agent trials (for example "agents must not edit the acceptance
specs"): an **accident guardrail** and a **measurement tool**, not enforcement.

Files live under the config dir (`$SHELL_RUNNER_CONFIG_DIR`, else
`${XDG_CONFIG_HOME:-~/.config}/shell-runner`):

- `rules.toml` — global rules, applied to every command.
- `projects/*.toml` — project rules; each declares `roots = ["/abs/path", ...]`
  and applies when `realpath(cwd)` is equal to or under one of the roots.
  All matching project files are merged with the global file.

Schema (both levels):

```toml
[[deny]]            # Python regex, searched against the raw command
pattern = 'pixi\s+run\s+acceptance-record'
reason  = "acceptance records are frozen"

[[protected]]
glob   = "tests/acceptance/**"   # ** spans directories; * and ? do not cross /
mode   = "deny"                  # "deny" (default) | "flag"
reason = "acceptance specs are frozen"
```

In a project file, relative globs resolve against the matching root. In the
global file, globs must be absolute or start with `~` (a relative glob there is
a config error). The global file may also set `default_agent_cap = "<TIER>"`
(`DENY`, `AUTO_LOG`, `AUTO_CAPPED`, `APPROVE_ONCE`, `ALWAYS_APPROVE`), which
replaces the built-in `AUTO_CAPPED` cap for agent ids not in the known-agent
registry; when absent, behaviour is unchanged.

Example, `projects/myrepo.toml`:

```toml
roots = ["/home/me/src/myrepo"]

[[protected]]
glob   = "tests/acceptance/**"
reason = "acceptance specs are frozen during the trial"

[[protected]]
glob   = "tests/acceptance.sha256"
reason = "acceptance hash is frozen during the trial"

[[deny]]
pattern = 'pixi\s+run\s+acceptance-record'
reason  = "re-recording acceptance output is not allowed"
```

Behaviour:

- A `[[deny]]` match behaves like a T0 deny (`matched_rule_category =
  "config_deny"`, the reason is returned in stderr). Built-in T0 rules win first.
- Best-effort **write targets** are extracted per command and resolved against
  the cwd (`~` expanded, `..` normalised, the path need not exist): output
  redirects, `tee`, `sed -i`, `cp`/`mv`/`install`/`ln` destinations (and `-t`),
  `rm`/`rmdir`/`truncate`/`touch`/`chmod`/`chown`/`shred`/`unlink`, `dd of=`,
  `git apply`/`checkout`/`restore`/`rm`/`mv`, and protected-path string literals
  inside `python`/`perl`/`ruby`/`node`/`sh -c|-e` one-liners. `mv` sources count
  as writes.
- A write match with `mode = "deny"` denies the command
  (`matched_rule_category = "protected_path"`). With `mode = "flag"` the command
  is classified normally and only flagged.
- Any other argument matching a protected glob is a **touch** (read or write
  unknown). Touches are recorded but never deny.
- Audit: `shell_calls.protected_path_match` holds a JSON list of
  `{path, glob, mode, kind: "write"|"touch", source_file}` and
  `shell_calls.flagged` is `1` when the command wrote to a protected path
  (flag-mode write, or deny-mode write that was denied). Both are populated for
  every recorded call, including denied ones. Older databases are migrated on
  startup.
- Parsed rules are cached; they are reloaded whenever a file's mtime or the set
  of files changes (no restart or SIGHUP needed).

**Fail closed.** If any rules file fails to parse or validate (bad TOML, bad
regex, unknown mode or tier name, unknown key, relative glob in the global file,
missing or relative `roots`), every command is denied with
`matched_rule_category = "rules_config_error"` and a stderr reason naming the
file and the error. A missing file or directory simply means no rules.

**Limitations.** Shell parsing is best-effort: `eval`, variables, scripts,
interpreters and here-docs can evade it (for example `python script.py` where
the script edits a protected file). Directory changes are tracked conservatively:
relative paths are checked against the cwd and every directory introduced by an
earlier `cd`/`pushd` in the same command, and against `git -C DIR`, so this may
over-match; `tar -C DIR` counts as a write target. `cd -`, bare `cd`, and
variable or subshell-computed directories are not tracked. The rules cover only commands that go
through shell-runner, so plain Bash or the Edit/Write tools bypass them. The
rules files live under the same OS user as the agents, so an agent that can
write them can change them: this is an accident guardrail and a measurement aid,
not a security boundary.

## License

MIT License - see [LICENSE](LICENSE) for details.
