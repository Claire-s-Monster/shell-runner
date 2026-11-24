# Approval wait in shell_execute + desktop approval notifier

Date: 2026-11-19
Base: development a3506b7. Rebase after PR #118 (#117 TKSENTINEL guard) merges.

## Problem

- Subagents that call `shell_execute` on a T3/T4 command get `decision=prompt_required` with `next_steps` telling them to poll `shell_wait`. Agents lacking `shell_wait` stop early.
- Evidence from an experiment on 2026-11-19 with `curl -sI https://example.com` (T3), the same brief in each arm:
  - focused-unit-resolver (`shell_execute` only) stopped after 1 call.
  - orchestrator (has `shell_wait`) polled twice, got the token and succeeded.
  - Re-running with a relayed `approve_token` also worked.
- Related, already done outside this repo: `~/.claude` commit f320b871 added `shell_wait` + `shell_status` to all 16 agents with `shell_execute`. This spec makes the server not depend on that.
- Second problem: the human approver does not notice prompts in time. The Streamlit UI (`ui/`, port 8511, refreshed by hand) sits on another desktop, and prompts expire after 300s.

## Constraints

- MCP transport cutoff of ~20s (#46), so `MAX_WAIT_S=15` for any single blocking call.
- No new Python dependencies: CI pins pixi 0.49.0 with lock v6, and dependency edits force a v7 re-solve that breaks CI.
- The #98 manifest-hash binding must not be weakened.

## Part B: opt-in bounded wait in shell_execute

### Request

- New optional field `approval_wait_s`.
- `None`/`0` means current behaviour; values are clamped to `MAX_WAIT_S`.

### Re-attach (always on)

- Before inserting a pending prompt, look for an open prompt to reuse. If found, reuse its id (`reattached=true`); otherwise insert as today.
- A candidate is re-attachable only if all of these hold:
  - same `agent_id`, `raw_cmd`, `cwd` and `detach`;
  - undecided, unconsumed and unexpired;
  - same `script_sha256`, compared NULL-safely with SQL `IS` (not `=`): `compute_pin` returns `None` for non-script commands, stored as NULL, so NULL must match NULL. The `script_pin.UNPINNABLE` sentinel value matches only itself;
  - same `normalized_template` and `command_tier`;
  - equal full `decision_path_json` and equal `matched_rule_category`. This covers the #98 manifest marker (`RUNNER_MANIFEST_NOT_APPROVED`) and the #107 non-durable loader marker without naming either.
- The caller (server.py) passes all of these values into `create_or_reattach_prompt`; persistence.py never imports server constants.
- The pin is computed before the transaction (`script_pin.compute_pin`); the comparison happens inside it.
- `idx_pending_agent_cmd` covers only `(agent_id, raw_cmd, cwd)`, so `detach` and the remaining fields are filtered in the query.
- Re-attach never extends `expires_at`.
- A prompt that has been decided (approved or denied) is never re-attached, even if it is unconsumed; re-calls after approval go through the classifier or `consume_approval_by_command`.
- Rationale: today every identical call inserts a new uuid row (`create_pending_prompt` in persistence.py), which would spam the queue and the notifier.

### Persistence

- New method `create_or_reattach_prompt(...) -> (row, reattached: bool)` in persistence.py. It runs the lookup and the insert in one `BEGIN IMMEDIATE` transaction on the write connection.
- `create_pending_prompt` stays unchanged. It remains the only path used by `_reprompt_script_changed` (server.py), which never re-attaches.

### Waiting

- The semaphore block now inside `_wait_for_prompt` (server.py ~2039; `_wait_semaphore`, `MAX_CONCURRENT_WAITS=8`, calling `_wait_for_prompt_decision(prompt_id, wait_s)`) is extracted into a small shared helper used by both `shell_wait` and `execute_route`. The agent-id check and the `ShellWaitResponse` shaping stay in `_wait_for_prompt`.
- When the wait semaphore is saturated (`_wait_semaphore.locked()`), the helper does a single check and returns `waiting` immediately, as `shell_wait` does.
- State mapping from the helper's row via `_prompt_wait_state`: `"error"` (consumed row) -> response state `"consumed"`; a `None` (swept) row -> `"expired"`; `"waiting"`, `"approved"` and `"denied"` pass through.

### Wait budget

- Module constant `APPROVAL_WAIT_DEADLINE_S = 18` in server.py: the ~20s MCP cutoff (#46) minus a 2s margin. The inline budget setting is not used.
- Effective wait = `max(0, min(approval_wait_s, MAX_WAIT_S, APPROVAL_WAIT_DEADLINE_S - elapsed_since_request_start))`.
- 0 means a single check, no sleeping.

### Outcomes when approval_wait_s is set

| state | response |
|---|---|
| approved | `decision=prompt_required`, `prompt.state="approved"`; `next_steps`: re-issue the identical call once to run it. No in-process execution after waiting (keeps wait + execution under the ~20s cutoff). See "Approved outcome" below. |
| denied | `decision="denied"`, `prompt.state="denied"`. See "Deny then re-call" below. |
| expired | `decision=prompt_required`, `prompt.state="expired"`; `next_steps`: stop. A prompt row already swept by cleanup also reports `expired`, as `_prompt_wait_state`/`_wait_for_prompt` do today. |
| consumed | `decision=prompt_required`, `prompt.state="consumed"`; `next_steps`: "this approval was already used by an earlier identical call; re-issue to request a new approval" |
| waiting | `decision=prompt_required`, `prompt.state="waiting"`, `waited_s`; `next_steps`: re-issue the identical call (same command, cwd, agent_id, `approval_wait_s`) to keep waiting; it re-attaches to prompt `<id>`; stop on denied/expired. Also returned immediately when the wait semaphore is saturated. |

- `decision` stays `prompt_required` for backward compatibility. The response's `prompt` block gains `state` and `reattached`.
- Audit: a re-attached call gets a `"reattached"` tag appended to `decision_path` in its `shell_calls` row (`record_call`, persistence.py). No schema change.

### Approved outcome

- The re-call runs because the classifier then allows the command; it gets the full inline budget.
- `approve_once`: the approval is consumed by the token-less `consume_approval_by_command` path (single use, TTL 3600s). A second re-call prompts again with a new prompt. `consumed` only arises when a concurrent identical call consumed the `approve_once` approval during this call's wait. A consumed prompt is never re-attachable, so a later re-call creates a new prompt.
- With `detach=true`, template/verb approvals do not apply. The downgrade happens in the approve handler in server.py: `guarded_note` is set for detach prompts at ~1753-1757, and `effective_decision` becomes `approve_once` at ~1829. `decide_prompt` only accepts `rule=None`. The detach test must go through the approve endpoint, not call `decide_prompt` directly. The detach re-call is therefore single-use.
- `approve_template`, `approve_template_global`, `approve_verb`: the re-call runs because the classifier now matches the newly created rule. Nothing is consumed and later identical calls keep running.

### Deny then re-call

- An agent following `next_steps` stops on `denied`.
- A later identical call finds no open prompt (the denied one is decided), so it creates a fresh prompt and popup. This is intended: the human can change their mind. There is no suppression window.

### Tests (B)

- Re-attach returns the same id and leaves expiry unchanged.
- Two concurrent identical calls create exactly 1 row.
- After expiry, a re-call creates a new prompt.
- A deny is seen as denied.
- A re-call after deny creates a fresh prompt (new id, new popup).
- `approve_once` end to end: wait -> approved -> re-call executes once -> a second re-call prompts again.
- Two concurrent waiters re-attached to one prompt, approved with `approve_once`: exactly one re-call executes; the other sees `state=consumed` (or a fresh prompt on a later re-call).
- Template approval: wait -> approved via `approve_template` -> re-call executes, and a further re-call executes again (nothing consumed).
- Consumed prompt reports `state="consumed"` with the re-issue `next_steps`; a swept row reports `expired`.
- The `approval_wait_s` clamp, and the deadline cap (`APPROVAL_WAIT_DEADLINE_S` minus elapsed, floored at 0, 0 = single check).
- Saturated wait semaphore returns `waiting` immediately.
- Callers that omit the field see no change except dedup.
- The reprompt path never re-attaches (it uses `create_pending_prompt`).
- Re-attach is refused on any mismatch: `script_sha256` (including `UNPINNABLE`), `normalized_template`, `command_tier`, `detach`, `matched_rule_category`, or `decision_path`.
- Two identical non-script calls re-attach (`script_sha256` NULL vs NULL).
- A differing `decision_path` (e.g. a #107 non-durable loader marker) does not re-attach.
- `detach=true`: wait -> approved via `approve_template` is downgraded to `approve_once`; the re-call executes once, then a further re-call prompts again.
- A re-attached call's `shell_calls` row carries the `reattached` tag in `decision_path`.
- State mapping: consumed row -> `consumed`, swept row -> `expired`.

## Part N: shell-runner-notify (desktop notifier)

### Process

- A separate process in `src/shell_runner/notifier.py`, run with `pixi run notify` or a systemd *user* unit (example unit file under `docs/` or `deploy/`).
- Stdlib only (`urllib`) plus the system `notify-send`.
- Every `notify-send` and `xdg-open` call uses an argv list (no shell).

### Polling

- Every 3s (configurable) it calls `POST http://127.0.0.1:4111/pending`.
- It skips prompts past `expires_at` (the endpoint does not filter expiry) and tracks seen ids in memory, since the endpoint has no "since" filter.

### Popups

- One popup per new prompt:
  - Title: "shell-runner: T<n> approval needed".
  - Body: `agent_id`, cwd shortened to `~`, the command cut to 120 chars, "expires in Xm Ys", and "decide on the Pending Prompts page".
  - A command cut to 120 chars gets "… (truncated)" appended.
  - Sanitising: control characters are stripped and markup (`<`, `>`, `&`) is escaped in `agent_id`, cwd and the command, since `notify-send` bodies may render markup.
  - `scope_warning` goes first if present.
  - T4 uses `urgency=critical`.
- Click-to-open: action "Open" runs `xdg-open` on a configurable URL (default `http://127.0.0.1:8511/Pending_Prompts`). Each popup's click wait (`notify-send --wait --action`) runs in its own thread, one thread per popup. If `--action` is unsupported, put the URL in the body.
- Reminder: one more popup ~60s before expiry if the prompt is still undecided (user requirement).
- Unreachable server: one popup, then back-off.
- Read-only: it never approves, and `/pending` returns no tokens.

### Decisions recorded

- The notifier polls rather than having the server spawn `notify-send`. This keeps desktop/DBUS side effects out of the sandboxed server.
- Popups inform only, with no approve buttons. The full context and scope warnings stay on the UI page (the `approve_template` normalization trap).

### Tests (N)

Inject the HTTP fetcher and the notify runner. Cover:

- A new prompt gives 1 popup.
- The same id is not repeated.
- Click-to-open: the action runs `xdg-open` with the configured URL; without `--action` support the URL is in the body.
- Reminder timing (~60s before expiry, once, only if still undecided).
- Expired prompts are skipped.
- Unreachable gives one popup and then backs off.
- Markup (`<b>`, `&`) and control-character inputs in `agent_id`, cwd and command are escaped/stripped.
- A command over 120 chars carries the "… (truncated)" marker; a shorter one does not.
- All `notify-send`/`xdg-open` invocations are argv lists.

### Security check (acceptance criterion)

- `POST /pending` is unauthenticated, so the server must bind only to 127.0.0.1.
- Static test: assert `--host 127.0.0.1` on the `ExecStart` line of `scripts/shell-runner.service` (line 21), on `DEFAULT_HOST` in `scripts/shell-runner-daemon.sh` (~line 25), and on the example service file if one exists under `scripts/`. No code reads `SHELL_RUNNER_HOST`, so the test does not check `Environment=SHELL_RUNNER_HOST` (line 10) and no `src/` default-host assertion is made.
- Ports found: `/mcp` is served by the same process on 4111 (`server.py` routes `/mcp`, ~2281), alongside the HTTP API. Port 4112 is used only by the throwaway `scripts/verify_mcp_mount.py` (127.0.0.1). `src/shell_runner/data/restart_registry.toml` has `[servers.shell-runner] port = 4111` and a smoke command with `--host 127.0.0.1`. If a separate 4112 `/mcp` listener is added to the unit, extend the test to it.
- Manual acceptance step: check the running server's listening sockets (`ss -ltnp`) show only 127.0.0.1:4111 (and :4112 if present). If not, file a separate issue.

## Acceptance criteria

Part B:

- [ ] `approval_wait_s` omitted or 0: behaviour unchanged except dedup.
- [ ] Wait is `max(0, min(approval_wait_s, MAX_WAIT_S, APPROVAL_WAIT_DEADLINE_S - elapsed))` with `APPROVAL_WAIT_DEADLINE_S = 18`; 0 means a single check.
- [ ] The semaphore block is a shared helper used by `shell_wait` and `execute_route`.
- [ ] Saturated wait semaphore returns `waiting` immediately.
- [ ] Identical open prompts re-attach (same id, `expires_at` unchanged, `reattached=true`); concurrent calls create 1 row; non-script calls re-attach (NULL `script_sha256` via `IS`).
- [ ] No re-attach on mismatch of agent, command, cwd, detach, `script_sha256` (incl. `UNPINNABLE`), template, tier, `matched_rule_category` or `decision_path`, nor on expired/decided/consumed prompts.
- [ ] Re-attached calls carry a `reattached` tag in the `shell_calls` `decision_path`.
- [ ] `detach=true` approvals are single-use (template/verb downgraded to `approve_once`).
- [ ] `_reprompt_script_changed` never re-attaches (`create_pending_prompt`).
- [ ] States approved, denied, expired, consumed and waiting return the documented response.
- [ ] `approve_once` re-call runs once, then prompts again; template/verb approvals keep running on later calls.
- [ ] Two concurrent waiters re-attached to one prompt, approved with `approve_once`: exactly one re-call executes; the other sees `state=consumed` (or a fresh prompt on a later re-call).
- [ ] Deny then identical re-call creates a fresh prompt.

Part N:

- [ ] One popup per new prompt, no repeats, expired prompts skipped.
- [ ] Click opens the UI URL (one thread per popup); URL in body when `--action` is unsupported.
- [ ] One reminder ~60s before expiry for still-undecided prompts.
- [ ] Unreachable server: one popup, then back-off.
- [ ] Popups use argv lists, strip control characters, escape markup, mark truncation and say "decide on the Pending Prompts page".
- [ ] Notifier is read-only and stdlib only.
- [ ] Static test: `--host 127.0.0.1` on the `ExecStart` line of `scripts/shell-runner.service`, `DEFAULT_HOST` in `scripts/shell-runner-daemon.sh`, and the example service file if present; port 4111 serves HTTP API and `/mcp`; 4112 checked per the Security check section.
- [ ] Manual: running server's listening sockets are loopback only.

## Out of scope

- Approve/deny buttons on popups.
- tmux and phone notifiers. Both could later reuse the same polling core.
- In-process execution after a wait.
- Notifier burst summary popup, startup summary popup and "reconnected" popup: add if needed.
- `shell_classify` not applying the cwd allowed-root check: it previewed `would_prompt` for a scratchpad cwd that `shell_execute` then denied. Track it as a separate issue.
