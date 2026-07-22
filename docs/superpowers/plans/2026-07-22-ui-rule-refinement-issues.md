# UI Rule-Refinement via GitHub Issues — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a human admin, from the Pending-Prompts page, (a) see *why* a command needs approval and which past approvals resemble it, and (b) trigger a read-only Claude analysis in the repo that proposes a classifier-rule enhancement, which the UI files as a GitHub issue for local review.

**Architecture:** Two independent capabilities on top of the existing Streamlit UI. **Layer 0** renders data the pending-prompt row *already* carries (`decision_path_json`, `matched_rule_category`) plus a "similar approvals" panel computed read-only from `template_approvals`/`shell_calls`. **Refinement** spawns `claude -p … --permission-mode plan` with `cwd=<repo>` (inherits `.mcp.json` + settings, cannot write), passes a *redacted* command + template + evidence, gets back a structured proposal, previews it to the human, and on explicit confirmation files a GitHub issue via the REST API (no `gh` CLI). Claude never mutates the repo, the DB, or the classifier — its only output is text; the issue write is performed by the UI as a human-confirmed action.

**Tech Stack:** Python 3.11+, Streamlit, `httpx` (already used), stdlib `subprocess`/`sqlite3`/`re`/`json`, pytest (+ `pytest-asyncio` auto, `respx`/`httpx.MockTransport` for HTTP stubs). No new runtime dependencies.

---

## Conventions for this repo (read before starting)

- **Git/shell are MCP-mediated.** This repo enforces primary-as-router: use the git MCP (`mcp__git__execute_tool`) for commits and the shell-runner MCP (or a subagent) for shell. The `git …` / `pytest …` commands below are written conventionally for readability — translate them to the MCP equivalents when executing.
- **Branch first.** `development` is the default branch; never commit feature work directly to it.
- **Commit trailer.** End commit messages with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **`raw_cmd` is unredacted and may contain secrets** (`Authorization` headers, tokens, `-u user:pass`). Anything that (1) is sent to the Claude subprocess or (2) is written into a GitHub issue MUST pass through `ui.refinement.redact()` first. This is the load-bearing safety property of the whole feature. Enforcement is **structural, not by convention**: `build_analysis_prompt()` and `build_issue_body()` call `redact()` on every command-bearing field themselves, so a forgotten upstream call cannot leak.

## Environment / configuration (new)

| Env var | Default | Purpose |
|---|---|---|
| `SHELL_RUNNER_CLAUDE_BIN` | `claude` | Path to the Claude CLI used for analysis. |
| `SHELL_RUNNER_ANALYSIS_TIMEOUT_S` | `180` | Hard timeout for the analysis subprocess. |
| `SHELL_RUNNER_GH_REPO` | `Claire-s-Monster/shell-runner` | `owner/repo` issues are filed against. |
| `SHELL_RUNNER_GH_TOKEN` | *(falls back to `GITHUB_TOKEN`)* | Token with `issues:write` on the repo. **Scrubbed from the analysis subprocess env** (Task 6). |

If `SHELL_RUNNER_GH_TOKEN`/`GITHUB_TOKEN` is unset, the "File issue" action is disabled with an explanatory message (analysis + preview still work).

## File structure

**Create:**
- `ui/refinement.py` — pure, Streamlit-free backend: `redact()`, `build_analysis_prompt()`, `run_claude_analysis()`, `validate_proposal()`, `build_issue_body()`, `find_open_issue()`, `file_github_issue()`. One responsibility: turn a pending prompt into a (validated) proposal and then into a GitHub issue.
- `tests/unit/test_ui_refinement.py` — unit tests for every pure function above (subprocess + HTTP stubbed).

**Modify:**
- `ui/data.py` — add read-only `get_similar_approvals(...)` (reimplements the server's verb-anchored Jaccard against the DB, keeping the UI's DB-only boundary) and `parse_decision_path(...)`.
- `tests/unit/test_ui_data.py` — add tests for the two new helpers.
- `ui/pages/2_Pending_Prompts.py` — Layer 0 rendering + the refinement button/preview/confirm-file flow.
- `README.md` — document the new page behaviour and env vars.

**Reference (do not modify):**
- `src/shell_runner/persistence.py:62-77` (pending_prompts schema), `:652-761` (`find_similar_approved_templates`, the algorithm to mirror), `:102-129` (`template_approvals`/`verb_approvals`).
- `src/shell_runner/catalog.py` (Rule dataclass + the four tier lists — the artifact Claude proposes a diff to).
- `src/shell_runner/classifier.py:96-118` (ClassificationResult / decision_path semantics).

---

## Phase 0 — Branch & scaffolding

### Task 0: Create the working branch and docs location

**Files:**
- Create: `docs/superpowers/plans/` (this document already lives here)

- [ ] **Step 1: Create branch**

Run (via git MCP): create + checkout `feat/ui-rule-refinement-issues` off `development`.
Expected: `git status` shows the new branch, clean tree.

- [ ] **Step 2: Confirm prerequisites (manual, one-time)**

Verify on the machine that will run the UI:
- `claude --version` succeeds (or set `SHELL_RUNNER_CLAUDE_BIN`).
- A token with `issues:write` is exported as `SHELL_RUNNER_GH_TOKEN` or `GITHUB_TOKEN`.
Record findings in the PR description. These are runtime prereqs, not code.

---

## Phase 1 — Layer 0: explain-why + similar approvals (no Claude)

### Task 1: `parse_decision_path` helper

**Files:**
- Modify: `ui/data.py`
- Test: `tests/unit/test_ui_data.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_ui_data.py
from ui.data import parse_decision_path

def test_parse_decision_path_valid_json_list():
    raw = '["normalized: curl <safe_url>", "segment \'curl\': T4 -> ALWAYS_APPROVE"]'
    assert parse_decision_path(raw) == [
        "normalized: curl <safe_url>",
        "segment 'curl': T4 -> ALWAYS_APPROVE",
    ]

def test_parse_decision_path_handles_none_and_garbage():
    assert parse_decision_path(None) == []
    assert parse_decision_path("") == []
    assert parse_decision_path("not json") == []
    assert parse_decision_path('{"not": "a list"}') == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_ui_data.py -k parse_decision_path -v`
Expected: FAIL — `ImportError: cannot import name 'parse_decision_path'`.

- [ ] **Step 3: Implement**

```python
# ui/data.py  (add near the other helpers)
import json

def parse_decision_path(decision_path_json: str | None) -> list[str]:
    """Decode the stored decision_path JSON list; return [] on any problem."""
    if not decision_path_json:
        return []
    try:
        value = json.loads(decision_path_json)
    except (ValueError, TypeError):
        return []
    if not isinstance(value, list):
        return []
    return [str(step) for step in value]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_ui_data.py -k parse_decision_path -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/data.py tests/unit/test_ui_data.py
git commit -m "feat(ui): add parse_decision_path helper"
```

### Task 2: `get_similar_approvals` (read-only Jaccard, mirrors server)

**Files:**
- Modify: `ui/data.py`
- Test: `tests/unit/test_ui_data.py`

**Algorithm (mirror of `persistence.find_similar_approved_templates`, `persistence.py:652-761`):** verb-anchored Jaccard over whitespace tokens; the first token (verb) must match exactly; filter `sim >= min_similarity`; sort by sim desc then `approved_at` desc; exclude exact-equal template; for duplicate templates keep the most permissive (lowest) tier, tie → prefer global. Enrich each with the most recent `raw_cmd` from `shell_calls` where `normalized_template = ?` and `decision = 'executed'`. **All returned `example_raw_cmd` values MUST be passed through `redact()` before display** (done at the render site, Task 8).

- [ ] **Step 1: Write the failing test** (seed an in-memory/temp sqlite with the two tables, assert ranking + verb filter + exact-exclude)

```python
# tests/unit/test_ui_data.py
import sqlite3
from pathlib import Path
from ui.data import get_similar_approvals

def _seed(tmp_path: Path) -> Path:
    db = tmp_path / "t.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE template_approvals (id INTEGER PRIMARY KEY, template TEXT,
            agent_id TEXT, approved_tier INTEGER, approved_at TEXT);
        CREATE TABLE shell_calls (id TEXT, ts TEXT, normalized_template TEXT,
            decision TEXT, raw_cmd TEXT);
        INSERT INTO template_approvals VALUES
            (1,'curl <safe_url> <arg>', NULL, 2, '2026-07-01T00:00:00'),
            (2,'curl <safe_url>',        NULL, 2, '2026-07-02T00:00:00'),
            (3,'wget <safe_url>',        NULL, 2, '2026-07-03T00:00:00');
        INSERT INTO shell_calls VALUES
            ('a','2026-07-02T00:00:00','curl <safe_url>','executed','curl https://x');
        """
    )
    conn.commit(); conn.close()
    return db

def test_get_similar_approvals_ranks_and_filters(tmp_path):
    db = _seed(tmp_path)
    out = get_similar_approvals("curl <safe_url> extra", agent_id="primary",
                                db_path=db, limit=3, min_similarity=0.3)
    templates = [row["template"] for row in out]
    assert "wget <safe_url>" not in templates          # verb filter excludes wget
    assert "curl <safe_url> extra" not in templates      # exact query excluded (not present anyway)
    assert templates and templates[0].startswith("curl") # curl candidates ranked
    top = out[0]
    assert set(top) >= {"template", "approved_tier", "example_raw_cmd", "similarity"}

def test_get_similar_approvals_missing_db_returns_empty(tmp_path):
    assert get_similar_approvals("curl <safe_url>", "primary",
                                 db_path=tmp_path / "nope.sqlite3") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_ui_data.py -k get_similar_approvals -v`
Expected: FAIL — import error.

- [ ] **Step 3: Implement**

```python
# ui/data.py
def _jaccard(a: set[str], b: set[str]) -> float:
    union = a | b
    return (len(a & b) / len(union)) if union else 0.0

def get_similar_approvals(
    template: str,
    agent_id: str,
    db_path: Path = DEFAULT_DB_PATH,
    *,
    limit: int = 3,
    min_similarity: float = 0.5,
) -> list[dict[str, Any]]:
    """Read-only mirror of Persistence.find_similar_approved_templates.

    Returns dicts: {template, approved_tier, example_raw_cmd, similarity}.
    example_raw_cmd is RAW (unredacted) — callers must redact before display.
    """
    if not db_path.exists():
        return []
    tokens = template.split()
    if not tokens:
        return []
    verb, qset = tokens[0], set(tokens)
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT template, approved_tier, approved_at,
                   CASE WHEN agent_id IS NULL THEN 0 ELSE 1 END AS scoped
            FROM template_approvals
            WHERE (agent_id IS NULL OR agent_id = ?) AND template != ?
            ORDER BY approved_at DESC
            """,
            (agent_id, template),
        ).fetchall()
        best: dict[str, tuple[int, str, int]] = {}
        for r in rows:
            t, tier, at, scoped = r["template"], r["approved_tier"], r["approved_at"], r["scoped"]
            if t not in best or tier < best[t][0]:
                best[t] = (tier, at, scoped)
        ranked = []
        for t, (tier, at, _) in best.items():
            ct = t.split()
            if not ct or ct[0] != verb:
                continue
            sim = _jaccard(qset, set(ct))
            if sim >= min_similarity:
                ranked.append((sim, at, t, tier))
        ranked.sort(key=lambda x: x[1], reverse=True)
        ranked.sort(key=lambda x: -x[0])
        out: list[dict[str, Any]] = []
        for sim, _at, t, tier in ranked[:limit]:
            call = conn.execute(
                "SELECT raw_cmd FROM shell_calls WHERE normalized_template = ? "
                "AND decision = 'executed' ORDER BY ts DESC LIMIT 1",
                (t,),
            ).fetchone()
            out.append({
                "template": t, "approved_tier": tier,
                "example_raw_cmd": call["raw_cmd"] if call else None,
                "similarity": round(sim, 3),
            })
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_ui_data.py -k get_similar_approvals -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/data.py tests/unit/test_ui_data.py
git commit -m "feat(ui): read-only get_similar_approvals mirroring server Jaccard"
```

---

## Phase 2 — Redaction (security-critical)

### Task 3: `redact()` — strip secrets from command text

**Files:**
- Create: `ui/refinement.py`
- Test: `tests/unit/test_ui_refinement.py`

**Ruleset (each is a TDD case):** replace matched secret material with the sentinel `‹REDACTED›`, preserving surrounding structure.
1. `Authorization: Bearer <tok>` and `Authorization: <tok>` (case-insensitive, in `-H`/`--header` values).
2. Header values whose name matches `authorization|cookie|x-api-key|api-key|token` → redact value.
3. `-u <user:pass>` / `--user <...>` → redact.
4. Body flags `-d|--data|--data-raw|--data-binary <payload>` → redact payload.
5. URL query params / `key=value` where key matches `(api[_-]?key|access[_-]?token|token|secret|password|sig|signature|key)` → redact value only.
6. Known token shapes anywhere: `sk-[A-Za-z0-9]{20,}`, `ghp_[A-Za-z0-9]{36,}`, `AKIA[0-9A-Z]{16}`, JWT `eyJ[\w-]+\.[\w-]+\.[\w-]+`.

**Design rule:** prefer over-redaction. A false positive costs readability; a false negative leaks a credential to GitHub.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_ui_refinement.py
import pytest
from ui.refinement import redact

R = "‹REDACTED›"

@pytest.mark.parametrize("raw,must_not_contain", [
    ('curl -H "Authorization: Bearer sk-abc1234567890TOKENVALUE" https://api.x', "sk-abc1234567890TOKENVALUE"),
    ('curl -u admin:hunter2 https://api.x', "hunter2"),
    ('curl --data "password=hunter2&x=1" https://api.x', "hunter2"),
    ('curl "https://api.x/v2?api_key=SECRETKEY123&page=2"', "SECRETKEY123"),
    ('gh auth login --with-token ghp_0123456789abcdef0123456789abcdef0123', "ghp_0123456789abcdef0123456789abcdef0123"),
    ('echo eyJhbGciOi.eyJzdWIiOiIxIn0.sig-part', "eyJhbGciOi.eyJzdWIiOiIxIn0.sig-part"),
])
def test_redact_removes_secret(raw, must_not_contain):
    out = redact(raw)
    assert must_not_contain not in out
    assert R in out

def test_redact_preserves_benign_command():
    raw = "curl -sSL https://example.com/v2/status?page=2"
    assert redact(raw) == raw  # nothing secret → unchanged

def test_redact_keeps_query_key_names_visible():
    out = redact("curl 'https://api.x?api_key=ABC123XYZ&page=2'")
    assert "api_key=" in out and "page=2" in out and "ABC123XYZ" not in out
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/unit/test_ui_refinement.py -k redact -v`
Expected: FAIL — module/function missing.

- [ ] **Step 3: Implement `redact()`** in `ui/refinement.py` using ordered `re.sub` passes for rules 1–6 above, sentinel `‹REDACTED›`. Keep each rule a separate named regex constant with a comment mapping to the ruleset number.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/test_ui_refinement.py -k redact -v`
Expected: PASS (all parametrizations).

- [ ] **Step 5: Commit**

```bash
git add ui/refinement.py tests/unit/test_ui_refinement.py
git commit -m "feat(ui): secret-redaction for analysis/issue payloads"
```

---

## Phase 3 — Claude analysis runner (read-only)

### Task 4: proposal schema + `validate_proposal()`

**Files:** Modify `ui/refinement.py`; Test `tests/unit/test_ui_refinement.py`.

**Proposal JSON contract (what Claude must emit as its final message):**
```json
{
  "missed_reason": "string, why the normalized template hit T3/T4 instead of an auto tier",
  "existing_lever": "approve_verb | approve_template_global | none",
  "proposed_rule": {
    "pattern": "regex string over the normalized template",
    "tier": "T1 | T2",
    "category": "kebab-case label",
    "match_target": "template | raw",
    "reason": "human explanation"
  },
  "catalog_section": "T0_DENY | T1_AUTO_LOG | T2_AUTO_CAPPED | T4_ALWAYS_APPROVE",
  "confidence": 0.0,
  "dedupe_key": "stable-slug-for-this-family",
  "issue_title": "short imperative title",
  "risk_notes": "why this could be wrong / what a reviewer must check"
}
```
`proposed_rule` may be `null` when `existing_lever != "none"`. `validate_proposal(obj)` raises `ValueError` on missing/mistyped keys or out-of-enum values; returns a normalized dict otherwise.

- [ ] **Step 1** Write failing tests: a valid full object passes; a valid `existing_lever` object with `proposed_rule: null` passes; missing `dedupe_key` raises; `tier: "T9"` raises; `confidence: "high"` raises.
- [ ] **Step 2** Run → fail.
- [ ] **Step 3** Implement `validate_proposal()` (explicit key/type/enum checks; no external deps).
- [ ] **Step 4** Run → pass.
- [ ] **Step 5** Commit: `feat(ui): validate Claude rule-proposal schema`.

### Task 5: `build_analysis_prompt()`

**Files:** Modify `ui/refinement.py`; Test `tests/unit/test_ui_refinement.py`.

Builds the `-p` prompt string from `{raw_cmd, normalized_template, command_tier, matched_rule_category, decision_path: list[str], similar: list[dict]}`. **This function calls `redact()` itself** on `raw_cmd` and on every `similar[i]["example_raw_cmd"]` before interpolating — redaction is enforced here structurally, so callers cannot forget it. The prompt instructs Claude to: read `src/shell_runner/catalog.py`, `classifier.py`, `normalizer.py`; explain why `normalized_template` did not match an auto rule; prefer an existing lever (`approve_verb`/`approve_template_global`) when one fits; otherwise propose exactly ONE catalog `Rule`; and **emit only the JSON object** (no prose) as the final message.

- [ ] **Step 1** Failing tests: (a) given a RAW command containing a secret (e.g. `curl -H "Authorization: Bearer sk-SECRET123..."`), the built prompt does NOT contain the secret and DOES contain `‹REDACTED›` — proves in-function redaction; (b) the prompt contains the normalized template, the exact schema key list, and the instruction "read src/shell_runner/catalog.py".
- [ ] **Step 2** Run → fail. **Step 3** Implement (call `redact()` internally). **Step 4** Run → pass.
- [ ] **Step 5** Commit: `feat(ui): analysis prompt builder with in-function redaction`.

### Task 6: `run_claude_analysis()` — subprocess, deny-by-default read-only, parsed

**Files:** Modify `ui/refinement.py`; Test `tests/unit/test_ui_refinement.py`.

**Why not `--permission-mode plan`:** plan mode does NOT neutralize allow rules — the repo's `.claude/settings.local.json` pre-approves `git_push`, `git_commit`, `mcp__claudecode-bash__execute_tool`, `Bash(git:*)`, `Bash(gh:*)`, and these still auto-execute under plan mode. Per Claude Code's documented precedence, **deny rules from any scope beat allow rules from any scope**, and `--permission-mode dontAsk` is the one headless mode with an explicit "auto-deny anything not allow-listed, never wait for input" guarantee. We therefore enforce read-only with (1) a deny wildcard, (2) `dontAsk`, and (3) not loading the repo's `.mcp.json` at all.

**Invocation (exact):**
```python
import os, json, subprocess

READONLY_SETTINGS = json.dumps({
    "permissions": {
        "allow": ["Read", "Grep", "Glob"],
        "deny": ["Bash", "Edit", "Write", "NotebookEdit", "mcp__*"],
        "defaultMode": "dontAsk",
    }
})

_SECRET_ENV_KEYS = ("SHELL_RUNNER_GH_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")

def _scrubbed_env() -> dict[str, str]:
    """Child env with issue-filing tokens removed so the analysis session can't read them."""
    env = dict(os.environ)
    for k in _SECRET_ENV_KEYS:
        env.pop(k, None)
    return env

cmd = [
    claude_bin, "-p", prompt,
    "--output-format", "json",
    "--permission-mode", "dontAsk",                                  # headless auto-deny, never blocks on a prompt
    "--allowedTools", "Read,Grep,Glob",
    "--disallowedTools", "Bash,Edit,Write,NotebookEdit,mcp__*",      # deny wildcard beats settings.local.json allow-list
    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',      # do NOT load repo .mcp.json (13 write-capable servers)
    "--settings", READONLY_SETTINGS,                                  # co-located deny rules for audit
]
proc = subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True,
                      timeout=timeout_s, check=False, env=_scrubbed_env())
```
CLAUDE.md/skills still load (we deliberately omit `--bare`), so the session keeps domain context while being unable to mutate anything.

**Parse:** `envelope = json.loads(proc.stdout)`; raise `AnalysisError` if `proc.returncode != 0` OR `envelope.get("is_error")` is truthy; else `text = envelope["result"]`, extract the JSON object from `text` (strip ```json fences / first `{`…last `}`), `obj = json.loads(...)`, `return validate_proposal(obj)`. Raise `AnalysisError` (new exception) with a short message on: non-zero exit, `is_error`, timeout (`subprocess.TimeoutExpired`), unparseable envelope, unparseable proposal, or validation failure.

- [ ] **Step 1: Write failing tests with a fake `claude` binary.** Write a tiny python script to `tmp_path` that prints a canned envelope `{"type":"result","result":"```json\\n{...valid proposal...}\\n```","is_error":false}` and pass its path as `claude_bin`. Tests:
  - parses fenced JSON from a valid envelope → returns validated proposal;
  - envelope with `"is_error": true` → raises `AnalysisError`;
  - fake binary exits 1 → raises `AnalysisError`;
  - **argv guard:** the constructed command list contains `--disallowedTools` with a value including `mcp__*` and includes `--strict-mcp-config` (refactor the argv construction into a testable helper `_build_argv(claude_bin, prompt)` so this can be asserted without spawning);
  - **env guard:** `_scrubbed_env()` contains none of `_SECRET_ENV_KEYS` even when they are set in `os.environ` (use `monkeypatch.setenv`).
- [ ] **Step 2** Run → fail. **Step 3** Implement `run_claude_analysis()`, `_build_argv()`, `_scrubbed_env()`, `AnalysisError`. **Step 4** Run → pass.
- [ ] **Step 5** Commit: `feat(ui): deny-by-default read-only claude analysis runner`.

---

## Phase 4 — GitHub issue filing (REST, deduped)

### Task 7: `build_issue_body()`, `find_open_issue()`, `file_github_issue()`

**Files:** Modify `ui/refinement.py`; Test `tests/unit/test_ui_refinement.py` (HTTP stubbed via `httpx.MockTransport`).

- `build_issue_body(proposal, redacted_cmd, normalized_template) -> (title, body, labels)`:
  - `title = proposal["issue_title"]`
  - `labels = ["classifier", "rule-enhancement", "ai-proposed", "needs-review"]`
  - body sections: **Redacted command**, **Normalized template**, **Why it missed**, **Proposed change** (either the `Rule(...)` snippet for `catalog_section`, or "Use existing lever: `<existing_lever>`"), **Confidence**, **Risk notes**, and a trailing hidden marker `<!-- dedupe:<dedupe_key> -->` for idempotency.
  - **Assert in tests** the body contains the dedupe marker and only the redacted command.
- `find_open_issue(dedupe_key, repo, token, *, transport=None) -> str | None`: GET `https://api.github.com/search/issues?q=repo:{repo}+is:issue+is:open+"dedupe:{dedupe_key}"`; return the first `html_url` or `None`.
- `file_github_issue(proposal, redacted_cmd, normalized_template, repo, token, *, transport=None) -> dict`: if `find_open_issue` hits, return `{"status": "duplicate", "url": ...}` without posting; else POST `https://api.github.com/repos/{repo}/issues` with `{title, body, labels}` and `Authorization: Bearer {token}`, return `{"status": "created", "url": resp["html_url"]}`. `transport` param enables `httpx.MockTransport` injection in tests.

- [ ] **Step 1** Write failing tests: (a) `build_issue_body` includes dedupe marker + labels; (b) `find_open_issue` returns url when search yields a hit, `None` when empty (MockTransport); (c) `file_github_issue` posts and returns created url (MockTransport asserts method/URL/auth header/body labels); (d) dedupe path returns `duplicate` and performs **no** POST.
- [ ] **Step 2** Run → fail. **Step 3** Implement. **Step 4** Run → pass.
- [ ] **Step 5** Commit: `feat(ui): github issue filing with dedupe`.

---

## Phase 5 — UI wiring (Pending Prompts page)

### Task 8: Layer 0 rendering

**Files:** Modify `ui/pages/2_Pending_Prompts.py`.

- [ ] **Step 1** Under each prompt's command block, add:
  - a caption line: `matched: <matched_rule_category or "no rule matched (T3 fallthrough)">`;
  - an expander **"Why this needs approval"** listing `parse_decision_path(p["decision_path_json"])` as bullet lines;
  - an expander **"Similar past approvals (N)"** calling `get_similar_approvals(p["normalized_template"], p["agent_id"])`; render each as `sim=<similarity> · T<tier> · <redact(example_raw_cmd)>` — **wrap `example_raw_cmd` in `redact()`**.
- [ ] **Step 2 (manual verify)** Launch the UI (`pixi run --environment <ui-env> streamlit run ui/app.py`, per README/prior session note), open Pending Prompts, confirm the new sections render and that a seeded secret-bearing example is redacted.
- [ ] **Step 3** Commit: `feat(ui): show classification reasoning + similar approvals`.

### Task 9: Refinement button → preview → confirm-file

**Files:** Modify `ui/pages/2_Pending_Prompts.py` (and import from `ui.refinement`).

Flow (per prompt, keyed by `pid`, state in `st.session_state`):
1. Button **"🔍 Propose rule enhancement"**. On click: build inputs — `redacted = redact(p["raw_cmd"])`, `decision_path = parse_decision_path(...)`, `similar = get_similar_approvals(...)` (redact example cmds) — then `prompt = build_analysis_prompt(...)`; run `run_claude_analysis(...)` inside `st.spinner("Analyzing in a read-only Claude session… (up to ~3 min)")`; store result/exception in `st.session_state[f"proposal_{pid}"]`.
2. When a proposal exists in state: render a **preview** (missed_reason, proposed rule / existing lever, confidence, risk_notes, the exact redacted command that will be posted). Show two buttons:
   - **"📋 File GitHub issue"** — enabled only if a token is configured; calls `file_github_issue(...)`, then `st.success(url)` (or `st.info("duplicate: url")`).
   - **"Discard"** — clears the state key.
3. On `AnalysisError`, show `st.error` with the short message; offer retry.

**Note:** `st.spinner` blocks the admin's session for the analysis duration; acceptable for a single-admin tool. A threaded/polling background runner is a documented future enhancement, not in scope.

- [ ] **Step 1** Implement the flow.
- [ ] **Step 2 (manual verify)** With a fake `claude` on PATH (from Task 6) or the real one, click through: analyze → preview shows redacted command → File issue (against a scratch repo or with a mocked token) → URL shown; re-file → duplicate path.
- [ ] **Step 3** Commit: `feat(ui): claude analysis + file-issue flow on pending prompts`.

---

## Phase 6 — Docs & security review

### Task 10: Documentation

**Files:** Modify `README.md`.

- [ ] **Step 1** Add a "Rule refinement" subsection to the UI docs: what the two expanders show, how the "Propose rule enhancement" flow works, the read-only guarantee, the redaction guarantee, and the four env vars. Note that proposals land as GitHub issues labelled `rule-enhancement` for local review/PR.
- [ ] **Step 2** Commit: `docs: document UI rule-refinement + issue filing`.

### Task 11: Full suite + security review

- [ ] **Step 1** Run the unit suite: `pytest tests/unit -v` → all pass. (Avoid `tests/live`, `tests/debug` — known to hang; already excluded from the default task.)
- [ ] **Step 2** Run the `/security-review` skill (or `focused-security-analyzer`) over `ui/refinement.py` and the two modified UI files. Focus: (a) can any code path send un-redacted `raw_cmd` to the subprocess or the issue body? (b) is `--permission-mode plan` sufficient to prevent the analysis session from executing shell/editing files, and is the `--disallowedTools` guard present? (c) token handling — never logged, never written to the issue. Address findings before merge.
- [ ] **Step 3** Open the PR against `development` with the prereq notes from Task 0.

---

## Out of scope / follow-ups (noted, not built here)

- **Expose `approve_verb` in the UI.** The server accepts it (`models.py` decision enum) but `2_Pending_Prompts.py` only surfaces `once/template/global/deny`. Adding a verb+cwd approval button is the cheapest real generalization win and pairs naturally with the "similar approvals" panel — recommend as the immediate next PR.
- **Pattern-based template promotion.** `template_approvals` is keyed on the exact normalized template; a regex/pattern promotion layer would let one approval cover a whole command family (the deepest fix to "looks like many I approved but didn't match"). Backend change; separate spec.
- **Async/threaded analysis runner** to avoid blocking the admin session during a multi-minute Claude call.
- **Auto-classify from proposals.** Deliberately excluded — proposals stay as issues for human review, preserving the trust boundary.
