# shell_monitor

`shell_monitor` watches a systemd **user** unit and/or follows a log file in the
background, so an agent can wait for "the build finished" or "the log says
`Overall: PASS`" without polling shell commands. It is read-only: it runs no
command, so it does not go through the classifier or the approval tiers. It
auto-executes and never prompts; `agent_id` is required but only logged.

It returns a `job_id` immediately. The monitor is an ordinary background job:
poll it with `shell_wait` (use `result_contains` to pull matching event lines)
or `shell_status`; stop it with `shell_kill`.

## Parameters

| Name | Default | Meaning |
|------|---------|---------|
| `agent_id` | required | Caller identity (logged only, never gated) |
| `unit` | none | systemd user unit. `.service` is appended unless the name ends in `.service/.scope/.timer/.socket/.target/.path`. Charset `[A-Za-z0-9@._:-]`, max 256 chars, no leading `-` |
| `log_path` | none | Absolute path of a log to follow. It may not exist yet |
| `patterns` | `[]` | Regexes (max 20, each <= 500 chars). A log line matching any becomes a `line` event. Empty = no line events (the log still feeds `log_idle`) |
| `preset` | none | Named pattern set, added to `patterns` (see Presets) |
| `until` | `unit_exit` if `unit` is set | `unit_exit`, `pattern:<regex>` or `log_idle:<seconds>`. Required when there is no unit |
| `from_start` | `false` | Read a log that already exists from its beginning instead of its end. A file that appears later is always read from the beginning |
| `timeout_s` | 3600 | 1..21600. On expiry the job ends `timed_out` |
| `max_events` | 200 | 1..1000 line events. After that one `note` says events were truncated; the monitor keeps watching for its terminal condition |
| `unit_grace_s` | 15 | 0..300. How long a not-yet-existing unit may be absent before `unit_missing` |

At least one of `unit` / `log_path` is required. `unit_exit` needs a unit;
`pattern:` and `log_idle:` need a `log_path`. Invalid input returns
`status: "error"` with an `error` message and creates no job.

Response: `job_id`, `status` (`running`), `unit_resolved`, `log_path_resolved`,
`until`, `events_path`.

## Events and statuses

Events are JSON lines in `events_path` (`stdout_path` of the job):

- `{"kind": "line", "source": "log", "text": ..., "pattern": ...}` (text cut to 2000 chars)
- `{"kind": "unit", "load_state", "active_state", "sub_state"}` on each state change
- `{"kind": "note", "text": ...}` (log rotated, systemctl hiccup, truncation)
- `{"kind": "terminal", "state", "reason", ...}` written **last, always**; for a
  unit it also carries `result`, `exec_main_status`, `exec_main_code`

The job status equals the terminal `state`:

| State | When |
|-------|------|
| `completed` | unit exited with `Result=success` and status 0; the `pattern:` matched; the log went idle; a transient unit was collected after being seen running (`unit_collected_after_exit`) |
| `failed` | unit failed (`unit_failed`, with its result fields); unit exited before the awaited pattern (`unit_exited_before_pattern`, even if it succeeded: silence is never success); `systemctl_error` (3 consecutive failures); `log_path_not_allowed`; `monitor_error: <Type>` (traceback in the job's stderr log) |
| `timed_out` | `timeout_s` elapsed |
| `killed` | `shell_kill` |
| `unit_missing` | the unit never appeared within `unit_grace_s` |

With `until=log_idle:N` a unit exiting first ends the monitor per the unit's
result (it is not treated as "exited before pattern").

Note: systemd keeps *failed* transient units unless started with `--collect`; a
unit that was seen running and then vanished is therefore reported as success.

## log_path allowlist

The path is resolved with `realpath` and must lie under one of:

- the shell-runner cwd roots (see `docs/cwd-roots.md`)
- `/tmp`
- a directory in `SHELL_RUNNER_MONITOR_LOG_DIRS` (`os.pathsep`-separated)
- a directory in `log_dirs` of `monitor.toml`

The deny-list wins even inside an allowed root: any path component `.ssh`,
`.gnupg`, `.aws`, `.kube`, `.docker`, `.password-store`, the sequence
`.config/gh`, and anything under `/etc /proc /sys /dev /root /boot`. Credential
files are refused by name wherever they sit: `.credentials.json`, `.netrc`,
`.pgpass`, `.git-credentials`, `.npmrc`, `.pypirc`, `credentials.json`,
`id_rsa`, `id_ed25519`, `id_ecdsa`, `id_dsa`, any `.env*`, `*.pem` and `*.key`
(checked on the symlink-resolved path). The check
is repeated whenever the file is (re)opened, so a symlink created later cannot
escape. Rejections return `log_path not allowed` plus the allowed roots.

## Presets and monitor.toml

`${XDG_CONFIG_HOME:-~/.config}/shell-runner/monitor.toml` is re-read on every call
(a parse error is logged and the file ignored).

Built-in presets: `errors` (`(?i)\b(error|fatal|traceback|exception|failed)\b`) and
`pytest` (the `=== N passed ... ===` summary line). A `[presets.<name>]` entry in
the TOML defines a new preset or overrides a built-in one.

```toml
log_dirs = ["~/.local/state/my-app"]

# Example: a rebuild script's log (drop in as-is, adjust to taste)
[presets.hb-rebuild]
patterns = [
  "gated green",
  "Gating",
  "FAILED",
  "fatal",
  "CONFLICT",
  "index\\.lock",
  "Overall:",
]
```

Usage: `shell_monitor(agent_id=..., unit="hb-rebuild", log_path="/tmp/hb-rebuild.log",
preset="hb-rebuild", until="pattern:Overall:")`.

Patterns are Python regexes evaluated on the server's event loop against lines
truncated to 2000 chars; avoid nested quantifiers.
