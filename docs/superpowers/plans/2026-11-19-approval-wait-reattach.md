# Approval wait + prompt re-attach Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `shell_execute` wait, if the caller opts in, for a T3/T4 approval (`approval_wait_s`), and make identical calls re-attach to one open pending prompt instead of adding a new row each time (issue #119, spec Part B only).

**Architecture:** A new `Persistence.create_or_reattach_prompt` runs the lookup and the insert in one `BEGIN IMMEDIATE` transaction and returns `(row, reattached)`. `execute_route` uses it in place of `create_pending_prompt`, tags re-attached audit rows, and, when `approval_wait_s` is set, waits for the prompt decision through the same semaphore-bounded helper that `shell_wait` uses. The wait is capped by `APPROVAL_WAIT_DEADLINE_S = 18`. The command is never executed in-process after a wait: `next_steps` tells the agent to re-issue the identical call.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, SQLite (WAL, shared write connection), pytest + pytest-asyncio (`asyncio_mode = "auto"`), FastAPI `TestClient`, pixi environments `dev` and `quality-ci`.

---

## Constraints (read before starting)

- **No dependency or `[project]` edits in `pyproject.toml`.** CI pins pixi 0.49.0 with lock v6, and any dependency or `[project]` change forces a v7 re-solve that breaks CI. No task in this plan edits `pyproject.toml` or `pixi.lock`.
- **Always `pixi run --frozen`.** Without `--frozen`, pixi may re-solve and rewrite `pixi.lock`. Every quality gate ends with `git diff --exit-code pixi.lock pyproject.toml`; if that fails, stop and `git checkout -- pixi.lock pyproject.toml`.
- **Don't weaken the #98 binding.** `decide_prompt` binds reusable rules to the prompt row's `script_paths` (persistence.py ~945-951). A re-attached row must therefore carry the same pin as the new call. Task 1 compares `script_sha256` **and** `script_paths` NULL-safely (see Spec deviation 1).
- **Rebase after #118 merges.** This branch is based on development `a3506b7`. Once PR #118 (#117 TKSENTINEL guard) merges, run `git fetch origin && git rebase origin/development` and re-run the full suite (Task 12).
- **Test reports must state the commit SHA and the tree path.** Every report of a test run (task hand-off, PR comment) says `git rev-parse HEAD` and the absolute tree path (`/home/memento/ClaudeCode/Servers/shell-runner/worktrees/features`).
- Test command: `pixi run --frozen -e dev pytest tests/<path>::<name> -v` (`dev` = feature `quality`, which provides pytest; see `pyproject.toml` `[tool.pixi.environments]`).
- **Quality gate (QG)**, run in every task before its commit, with `<task files>` replaced by the task's `git add` list:

  ```bash
  pixi run --frozen -e quality-ci ruff check --fix src/ tests/
  pixi run --frozen -e quality-ci ruff format src/ tests/
  git add <task files>            # re-stage whatever the fixers changed
  pixi run --frozen -e quality-ci ruff check src/ tests/
  pixi run --frozen -e quality-ci ruff format --check src/ tests/
  pixi run --frozen -e quality-ci mypy src/
  git diff --exit-code pixi.lock pyproject.toml
  ```

  If the fixers touch a file outside the task, `git checkout -- <that file>` and mention it in the hand-off.
- **Import placement** (ruff isort, `I`): within the stdlib block, plain `import x` lines come first (alphabetical), then `from x import y` lines (alphabetical). Each "add import" step says which existing line the new one goes after.
- `tests/unit/test_no_sync_db_writes_in_async.py` fails the build if an `async def` in server.py calls a Persistence write method directly. Every new write goes through `asyncio.to_thread` (Task 3 adds the new method to that guard).
- Commit with explicit paths (`git add <paths>`), never `git add -A`.

## Spec deviations

1. **`script_paths` is matched too.** The spec's match list has `script_sha256` but not `script_paths`. `decide_prompt` passes `row["script_paths"]` to `_apply_rule` as the #98 manifest binding (persistence.py ~948), so re-attaching to a row whose `script_paths` differs would bind a rule to manifest refs the current call did not compute. Both columns are compared with `IS`.
2. **`waited_s` is in the `prompt` block.** The spec lists `waited_s` in the `waiting` row of the outcome table but doesn't say where it goes. It goes next to `state` and `reattached` in `ExecutePromptInfo`, and is set for every state after a wait.
3. **"Swept row -> expired, as `_wait_for_prompt` does today" is only half true.** `shell_wait` reports a prompt that is already missing *when the call starts* as `state="error"` (`test_swept_prompt_reports_error_not_found`). Only a row swept *during* the wait becomes `expired` (server.py ~2060). In `execute_route` the row always exists when the wait starts (it was just created or re-attached), so `None` -> `expired` is correct there. `shell_wait` is left unchanged.
4. **`next_steps` for `denied` is not in the spec.** The spec only says "an agent following `next_steps` stops on `denied`". This plan uses "Prompt <id> was denied. Stop: do not re-issue this command."
5. **The audit row records the call, not the wait outcome.** The `shell_calls` row is written before the wait, with `decision="prompt_required"` (and the `reattached` tag). It is not updated when the wait ends in `denied`. The spec allows no schema change, so none is made.
6. **`create_pending_prompt` is reused, not copied.** The spec says it "stays unchanged", and it does: byte-for-byte. `create_or_reattach_prompt` calls it inside its own `BEGIN IMMEDIATE` transaction. This works because `_write_conn` is reentrant on the same connection and inner levels never commit or roll back (persistence.py ~518-546).

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `src/shell_runner/persistence.py` | Modify | `_REATTACH_SQL` constant; `Persistence.create_or_reattach_prompt` (lookup + insert in one transaction). `create_pending_prompt` unchanged. |
| `src/shell_runner/models.py` | Modify | `ExecutePromptState` alias; `ExecuteRequest.approval_wait_s`; `ExecutePromptInfo.reattached/state/waited_s`. |
| `src/shell_runner/mcp_wrapper.py` | Modify | `shell_execute` schema property `approval_wait_s` plus one description sentence. |
| `src/shell_runner/server.py` | Modify | `execute_route` uses re-attach, tags `decision_path`, and waits. New `_bounded_prompt_wait` (shared with `shell_wait`), `APPROVAL_WAIT_DEADLINE_S`, `_approval_wait_budget`, `_execute_prompt_state`, `_WAIT_NEXT_STEPS`, `_wait_on_prompt`. `_reprompt_script_changed` unchanged. |
| `README.md` | Modify | New section on `approval_wait_s`, re-attach and the response states. |
| `tests/unit/test_prompt_reattach.py` | Create | Persistence re-attach: match criteria, NULL-safety, expiry, decided/consumed, in-process and cross-process concurrency. |
| `tests/unit/test_no_sync_db_writes_in_async.py` | Modify | Add `create_or_reattach_prompt` to `WRITE_METHODS`. |
| `tests/integration/test_concurrency.py` | Modify | HTTP-level: concurrent identical calls share one row. |
| `tests/integration/test_wait_for_approval.py` | Modify | HTTP: dedup, tag, `reattached`, wait budget, states, `next_steps`, template approval, approve_once, deny, two waiters. |
| `tests/unit/test_shell_wait.py` | Modify | Shared helper, budget function, state mapping. |
| `tests/unit/test_models.py` | Modify | New model fields. |
| `tests/integration/test_mcp_endpoint.py` | Modify | Schema exposes `approval_wait_s`. |
| `tests/unit/test_detach_approval.py` | Modify | Detach: `approve_template` during a wait is downgraded and single-use. |
| `tests/unit/test_script_content_binding.py` | Modify | Guard: the script-changed reprompt never calls `create_or_reattach_prompt`. |

---

### Task 1: `Persistence.create_or_reattach_prompt`

**Files:**
- Modify: `src/shell_runner/persistence.py`: constant after `APPROVE_ONCE_DURABLE_TTL_S` (~line 327); method right after `create_pending_prompt` (~line 792)
- Test: `tests/unit/test_prompt_reattach.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_prompt_reattach.py`:

```python
"""create_or_reattach_prompt: identical open prompts are reused, not duplicated (issue #119)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from shell_runner.persistence import Persistence
from shell_runner.script_pin import UNPINNABLE

BASE: dict[str, Any] = {
    "agent_id": "agent-a",
    "cwd": "/tmp",
    "raw_cmd": "mytool --flag",
    "normalized_template": "mytool --flag",
    "command_tier": 3,
    "matched_rule_category": None,
    "decision_path": ["fallthrough"],
}
SHA_A = "a" * 64
SHA_B = "b" * 64


@pytest.fixture
def db(tmp_path: Path) -> Persistence:
    return Persistence(db_path=tmp_path / "test.sqlite3")


def _open(db: Persistence, **overrides: Any) -> tuple[dict, bool]:
    return db.create_or_reattach_prompt(**{**BASE, **overrides})


def _count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM pending_prompts").fetchone()[0]


def test_first_call_inserts_a_prompt(db: Persistence) -> None:
    row, reattached = _open(db)
    assert reattached is False
    assert row["raw_cmd"] == BASE["raw_cmd"]
    assert datetime.fromisoformat(row["expires_at"]) > datetime.now(UTC)
    assert _count(db.db_path) == 1


def test_identical_call_reattaches_and_keeps_expiry(db: Persistence) -> None:
    first, _ = _open(db)
    second, reattached = _open(db)
    assert reattached is True
    assert second["id"] == first["id"]
    assert second["expires_at"] == first["expires_at"]  # re-attach never extends expiry
    assert _count(db.db_path) == 1


def test_non_script_calls_match_null_sha(db: Persistence) -> None:
    first, _ = _open(db)
    assert first["script_sha256"] is None
    second, reattached = _open(db)
    assert (second["id"], reattached) == (first["id"], True)


def test_unpinnable_matches_only_itself(db: Persistence) -> None:
    first, _ = _open(db, script_sha256=UNPINNABLE)
    second, reattached = _open(db, script_sha256=UNPINNABLE)
    assert (second["id"], reattached) == (first["id"], True)


@pytest.mark.parametrize(
    ("field", "first", "second"),
    [
        ("agent_id", "agent-a", "agent-b"),
        ("raw_cmd", "mytool --flag", "mytool --other"),
        ("cwd", "/tmp", "/var/tmp"),
        ("detach", False, True),
        ("script_sha256", None, SHA_A),
        ("script_sha256", SHA_A, SHA_B),
        ("script_sha256", UNPINNABLE, SHA_A),
        ("script_sha256", UNPINNABLE, None),
        ("script_paths", None, '{"/tmp/x.sh": "' + SHA_A + '"}'),
        ("normalized_template", "mytool --flag", "mytool <arg>"),
        ("command_tier", 3, 4),
        ("matched_rule_category", None, "package"),
        ("matched_rule_category", "package", "network"),
        # the #98 manifest marker (server.RUNNER_MANIFEST_NOT_APPROVED) ...
        ("decision_path", ["fallthrough"], ["fallthrough", "runner_manifest_not_approved"]),
        # ... and the #107 non-durable loader marker (classifier.py ~508)
        (
            "decision_path",
            ["fallthrough"],
            ["fallthrough", "non_durable: LD_PRELOAD; approve_once only"],
        ),
    ],
)
def test_any_mismatch_creates_a_new_prompt(
    db: Persistence, field: str, first: Any, second: Any
) -> None:
    a, _ = _open(db, **{field: first})
    b, reattached = _open(db, **{field: second})
    assert reattached is False
    assert b["id"] != a["id"]
    assert _count(db.db_path) == 2


def test_expired_prompt_is_not_reattached(db: Persistence) -> None:
    old = db.create_pending_prompt(**BASE, ttl_seconds=-1)
    row, reattached = _open(db)
    assert reattached is False
    assert row["id"] != old


@pytest.mark.parametrize("decision", ["approve_once", "approve_template", "deny"])
def test_decided_prompt_is_never_reattached(db: Persistence, decision: str) -> None:
    first, _ = _open(db)
    db.approve_prompt(prompt_id=first["id"], decision=decision)
    row, reattached = _open(db)
    assert reattached is False
    assert row["id"] != first["id"]


def test_consumed_prompt_is_never_reattached(db: Persistence) -> None:
    first, _ = _open(db)
    db.approve_prompt(prompt_id=first["id"], decision="approve_once")
    consumed = db.consume_approval_by_command(
        raw_cmd=BASE["raw_cmd"], cwd=BASE["cwd"], agent_id=BASE["agent_id"]
    )
    assert consumed is not None
    row, reattached = _open(db)
    assert reattached is False
    assert row["id"] != first["id"]


def test_create_pending_prompt_still_always_inserts(db: Persistence) -> None:
    a = db.create_pending_prompt(**BASE)
    b = db.create_pending_prompt(**BASE)
    assert a != b
    assert _count(db.db_path) == 2
```

- [ ] **Step 2: Run the tests and see them fail**

Run: `pixi run --frozen -e dev pytest tests/unit/test_prompt_reattach.py -v`
Expected: every test except `test_create_pending_prompt_still_always_inserts` fails with `AttributeError: 'Persistence' object has no attribute 'create_or_reattach_prompt'`. That one passes because it is a guard on unchanged behaviour.

- [ ] **Step 3: Minimal implementation**

In `src/shell_runner/persistence.py`, add this module constant directly after `APPROVE_ONCE_DURABLE_TTL_S = 3600` (~line 327):

```python
# issue #119: an open prompt is re-attachable only if everything the approver decides on
# matches. idx_pending_agent_cmd covers (agent_id, raw_cmd, cwd); the rest is filtered
# here. Nullable columns use IS so NULL matches NULL (non-script commands store no pin)
# and the UNPINNABLE sentinel matches only itself. script_paths is compared too: it is
# the #98 manifest binding decide_prompt attaches to reusable rules.
_REATTACH_SQL = """
    SELECT id FROM pending_prompts
    WHERE agent_id = ? AND raw_cmd = ? AND cwd = ? AND detach = ?
      AND approve_decision IS NULL AND consumed_at IS NULL AND expires_at >= ?
      AND script_sha256 IS ? AND script_paths IS ?
      AND normalized_template = ? AND command_tier = ?
      AND decision_path_json IS ? AND matched_rule_category IS ?
    ORDER BY created_at DESC, rowid DESC
    LIMIT 1
"""
```

Add this method right after `create_pending_prompt` (which ends `return prompt_id`, ~line 791). Leave `create_pending_prompt` itself as it is.

```python
    def create_or_reattach_prompt(
        self,
        *,
        agent_id: str,
        cwd: str,
        raw_cmd: str,
        normalized_template: str,
        command_tier: int,
        matched_rule_category: str | None,
        decision_path: list[str],
        detach: bool = False,
        script_sha256: str | None = None,
        script_paths: str | None = None,
    ) -> tuple[dict, bool]:
        """Reuse an identical open prompt, else insert one (issue #119).

        Lookup and insert share one BEGIN IMMEDIATE transaction, so concurrent identical
        calls (threads or processes) create a single row. Only undecided, unconsumed,
        unexpired prompts that match on every field in _REATTACH_SQL qualify; re-attach
        never touches expires_at. The insert reuses create_pending_prompt (the shared
        write connection is reentrant, so it joins this transaction).
        Returns (full row, reattached).
        """
        with self._write_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = _now_utc()  # sampled after the write lock, as in decide_prompt
            found = conn.execute(
                _REATTACH_SQL,
                (
                    agent_id,
                    raw_cmd,
                    cwd,
                    int(detach),
                    now,
                    script_sha256,
                    script_paths,
                    normalized_template,
                    command_tier,
                    json.dumps(decision_path),
                    matched_rule_category,
                ),
            ).fetchone()
            reattached = found is not None
            prompt_id = (
                found["id"]
                if found is not None
                else self.create_pending_prompt(
                    agent_id=agent_id,
                    cwd=cwd,
                    raw_cmd=raw_cmd,
                    normalized_template=normalized_template,
                    command_tier=command_tier,
                    matched_rule_category=matched_rule_category,
                    decision_path=decision_path,
                    detach=detach,
                    script_sha256=script_sha256,
                    script_paths=script_paths,
                )
            )
            row = conn.execute(
                "SELECT * FROM pending_prompts WHERE id = ?", (prompt_id,)
            ).fetchone()
            conn.execute("COMMIT")
        return dict(row), reattached
```

`json` and `_now_utc` already exist in persistence.py, so no import is needed.

- [ ] **Step 4: Run the tests and see them pass**

Run: `pixi run --frozen -e dev pytest tests/unit/test_prompt_reattach.py tests/unit/test_persistence.py -v`
Expected: all PASS.
QG with `<task files>` = `src/shell_runner/persistence.py tests/unit/test_prompt_reattach.py`.

- [ ] **Step 5: Commit**

```bash
git add src/shell_runner/persistence.py tests/unit/test_prompt_reattach.py
git commit -m "feat(persistence): create_or_reattach_prompt reuses identical open prompts (#119)"
```

---

### Task 2: Concurrency: in-process and cross-process

**Files:**
- Test: `tests/unit/test_prompt_reattach.py` (imports + append)

Two tests, because they cover different things. Persistence instances on the same path share one `_write_state` and its RLock (persistence.py ~370, ~476), so **the thread test covers only the in-process lock** and would pass even without `BEGIN IMMEDIATE`. Only the cross-process test exercises `BEGIN IMMEDIATE`, because separate processes share no Python lock. It is a deterministic lock-ordering test, not a race:

- The parent holds the SQLite write lock through its own raw connection (`BEGIN IMMEDIATE`).
- A spawned child calls `create_or_reattach_prompt` and must block.
- While holding the lock, the parent inserts a matching open prompt and commits. The child must then re-attach to that row.

Why this is deterministic: the database runs in WAL mode (`PRAGMA journal_mode=WAL`, persistence.py:383 for the shared write connection and :509 for read connections), so readers are never blocked by a writer. If the lookup ran outside `BEGIN IMMEDIATE`, the child's `SELECT` would run straight through during the parent's lock and find nothing. Its `INSERT` would then wait, run after the parent commits, and leave 2 rows with `reattached=False`. With `BEGIN IMMEDIATE`, the child waits *before* the lookup, and the wait is bounded: the write connection uses `timeout=BUSY_TIMEOUT_MS / 1000` and `PRAGMA busy_timeout=5000` (`BUSY_TIMEOUT_MS = 5000`, persistence.py:330, :379, :385). The parent holds the lock for about 1s, well inside 5s, so the child waits instead of failing with "database is locked". The test does not need to set a timeout of its own on the child; the parent's raw connection uses `timeout=5` for symmetry.

- [ ] **Step 1: Write the tests**

In `tests/unit/test_prompt_reattach.py`, replace the stdlib import lines

```python
import sqlite3
from datetime import UTC, datetime
```

with

```python
import json
import multiprocessing
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
```

(`from pathlib import Path` and `from typing import Any` stay right after `from datetime ...`.) Then append:

```python
def test_concurrent_threads_create_one_row(tmp_path: Path) -> None:
    """In-process only: same-path instances share one RLock, so this guards the lock,
    not BEGIN IMMEDIATE (see the cross-process test below)."""
    path = tmp_path / "test.sqlite3"
    Persistence(db_path=path)  # schema first, so the threads only race on the prompt
    barrier = threading.Barrier(8)
    results: list[tuple[str, bool]] = []
    lock = threading.Lock()

    def call() -> None:
        own = Persistence(db_path=path)
        barrier.wait()
        row, reattached = own.create_or_reattach_prompt(**BASE)
        with lock:
            results.append((row["id"], reattached))

    threads = [threading.Thread(target=call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 8
    assert len({pid for pid, _ in results}) == 1
    assert sorted(r for _, r in results) == [False] + [True] * 7
    assert _count(path) == 1


def _reattach_in_child(db_path: str, ready: Any, go: Any, entered: Any, results: Any) -> None:
    """Spawned child (top-level so spawn can pickle it). Its Persistence is built before
    the parent takes the write lock, so schema init never contends with it."""
    db = Persistence(db_path=db_path)
    ready.set()
    go.wait(timeout=10)
    entered.set()  # about to call create_or_reattach_prompt
    row, reattached = db.create_or_reattach_prompt(**BASE)
    results.put((row["id"], reattached))


def test_lookup_waits_for_a_concurrent_writer_across_processes(tmp_path: Path) -> None:
    """Deterministic: the child must block on BEGIN IMMEDIATE while the parent holds the
    write lock, then re-attach to the row the parent committed. A lookup outside the
    transaction would not block (WAL readers never wait), miss the row and insert a second."""
    path = tmp_path / "test.sqlite3"
    Persistence(db_path=path)  # schema
    ctx = multiprocessing.get_context("spawn")
    ready, go, entered = ctx.Event(), ctx.Event(), ctx.Event()
    results = ctx.Queue()
    child = ctx.Process(target=_reattach_in_child, args=(str(path), ready, go, entered, results))
    child.start()
    try:
        assert ready.wait(timeout=30), "child never initialised"
        writer = sqlite3.connect(path, isolation_level=None, timeout=5)
        try:
            writer.execute("BEGIN IMMEDIATE")  # the parent now holds the write lock
            go.set()
            assert entered.wait(timeout=10), "child never reached create_or_reattach_prompt"
            time.sleep(1.0)
            assert child.is_alive()
            assert results.empty(), "child finished while the parent held the write lock"
            parent_id = str(uuid.uuid4())
            now = datetime.now(UTC)
            # same column set as create_pending_prompt's INSERT
            writer.execute(
                """
                INSERT INTO pending_prompts (
                    id, created_at, expires_at, agent_id, cwd, raw_cmd,
                    normalized_template, command_tier, matched_rule_category,
                    decision_path_json, detach, script_sha256, script_paths
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    parent_id,
                    now.isoformat(),
                    (now + timedelta(seconds=300)).isoformat(),
                    BASE["agent_id"],
                    BASE["cwd"],
                    BASE["raw_cmd"],
                    BASE["normalized_template"],
                    BASE["command_tier"],
                    BASE["matched_rule_category"],
                    json.dumps(BASE["decision_path"]),
                    0,
                    None,
                    None,
                ),
            )
            writer.execute("COMMIT")
        finally:
            writer.close()
        child_id, reattached = results.get(timeout=10)
    finally:
        child.join(timeout=10)
        if child.is_alive():
            child.terminate()

    assert (child_id, reattached) == (parent_id, True)
    assert _count(path) == 1
```

- [ ] **Step 2: Run them**

Run: `pixi run --frozen -e dev pytest tests/unit/test_prompt_reattach.py -k "concurrent_threads or across_processes" -v`
Expected: both PASS (Task 1 already holds the transaction).

- [ ] **Step 3: Temporarily remove `BEGIN IMMEDIATE`, see it fail, restore**

Temporarily delete the line `conn.execute("BEGIN IMMEDIATE")` from `create_or_reattach_prompt`, and the matching `conn.execute("COMMIT")` (without the BEGIN there is no transaction to commit). Re-run the command from Step 2.
Expected: `test_lookup_waits_for_a_concurrent_writer_across_processes` FAILS at `assert (child_id, reattached) == (parent_id, True)`. The child's `SELECT` ran during the parent's lock, found nothing, and its `INSERT` went in after the commit, so `reattached` is `False` with a new id and there are 2 rows. `test_concurrent_threads_create_one_row` still PASSES, which is why it only counts as an in-process guard.
If the cross-process test passes even with the lines removed, the result is spurious: the child took longer than the parent's 1s hold to reach its `SELECT`, so the lookup ran after the commit. Re-run it on a less loaded machine. With `BEGIN IMMEDIATE` in place the test is deterministic either way.
Restore both lines exactly, then run `git diff src/shell_runner/persistence.py`; it must show no change from the Task 1 commit.

- [ ] **Step 4: Quality gate**

QG with `<task files>` = `tests/unit/test_prompt_reattach.py`.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_prompt_reattach.py
git commit -m "test(persistence): in-process and cross-process prompt re-attach concurrency (#119)"
```

---

### Task 3: `execute_route` switches to re-attach (reprompt path unchanged)

**Files:**
- Modify: `src/shell_runner/server.py`: `execute_route` "T3/T4 — create pending prompt" block (~lines 1423-1437)
- Modify: `tests/unit/test_no_sync_db_writes_in_async.py`: `WRITE_METHODS` (~line 23)
- Test: `tests/integration/test_wait_for_approval.py`, `tests/integration/test_concurrency.py`, `tests/unit/test_script_content_binding.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_wait_for_approval.py` (no new imports):

```python
# --- issue #119: re-attach ---------------------------------------------------


def test_identical_calls_share_one_open_prompt(client: TestClient, tmp_path: Path) -> None:
    first = _request_prompt(client, tmp_path)
    second = _request_prompt(client, tmp_path)
    assert second == first
    assert [r["id"] for r in srv.db.list_pending_prompts()] == [first]
```

Append to `tests/integration/test_concurrency.py` (`threading` and `srv` are already imported):

```python
def test_concurrent_identical_prompts_share_one_row(client: TestClient, tmp_path: Path) -> None:
    """issue #119: identical concurrent T3/T4 calls re-attach to one pending prompt."""
    ids: list[str] = []
    lock = threading.Lock()

    def call() -> None:
        r = client.post(
            "/execute",
            json={
                "command": "pip install requests",
                "cwd": str(tmp_path),
                "agent_id": "focused-ghc-ci-analyzer",
            },
        )
        with lock:
            ids.append(r.json()["prompt"]["id"])

    threads = [threading.Thread(target=call) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(ids) == 5
    assert len(set(ids)) == 1, ids
    assert len(srv.db.list_pending_prompts()) == 1
```

Append to `tests/unit/test_script_content_binding.py` (no new imports). It is a guard: `_reprompt_script_changed` must keep using `create_pending_prompt`.

```python
def test_script_changed_reprompt_never_reattaches(
    client: TestClient,
    calls: list[Any],
    script: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = _execute(client, SCRIPT_CMD, tmp_path)["prompt"]
    _approve(client, prompt["id"])
    script.write_text("echo rewritten\n")
    reattach_calls: list[dict] = []
    original = srv.db.create_or_reattach_prompt

    def _spy(**kwargs: Any) -> tuple[dict, bool]:
        reattach_calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(srv.db, "create_or_reattach_prompt", _spy)
    reprompted = _execute(client, SCRIPT_CMD, tmp_path)  # consumes -> _reprompt_script_changed
    assert reprompted["decision"] == "prompt_required"
    assert "changed" in reprompted["prompt"]["why"]
    assert reattach_calls == []
    assert calls == []
```

In `tests/unit/test_no_sync_db_writes_in_async.py`, add `"create_or_reattach_prompt",` to `WRITE_METHODS` directly after the line `"create_pending_prompt",`.

- [ ] **Step 2: Run them and see them fail**

Run: `pixi run --frozen -e dev pytest tests/integration/test_wait_for_approval.py::test_identical_calls_share_one_open_prompt tests/integration/test_concurrency.py::test_concurrent_identical_prompts_share_one_row tests/unit/test_script_content_binding.py::test_script_changed_reprompt_never_reattaches -v`
Expected:
- `test_identical_calls_share_one_open_prompt` FAILS with `AssertionError` (`second == first` is false: two uuids).
- `test_concurrent_identical_prompts_share_one_row` FAILS on `len(set(ids)) == 1`.
- `test_script_changed_reprompt_never_reattaches` PASSES (guard).

- [ ] **Step 3: Minimal implementation**

In `src/shell_runner/server.py` `execute_route`, replace this block:

```python
    # T3/T4 — create pending prompt, return prompt_required
    pin = await asyncio.to_thread(script_pin.compute_pin, req.command, req.cwd)
    prompt_id = await asyncio.to_thread(
        db.create_pending_prompt,
        agent_id=req.agent_id,
```

through its closing `)` (the call ends with `script_paths=pin.paths_json if pin else None,` and `)`) with:

```python
    # T3/T4 — re-attach to an identical open prompt or create one (issue #119); the pin
    # is computed before the transaction, compared inside it.
    pin = await asyncio.to_thread(script_pin.compute_pin, req.command, req.cwd)
    prompt_row, reattached = await asyncio.to_thread(
        db.create_or_reattach_prompt,
        agent_id=req.agent_id,
        cwd=req.cwd,
        raw_cmd=req.command,
        normalized_template=cls.template,
        command_tier=int(cls.command_tier),
        matched_rule_category=cls.matched_rule.category if cls.matched_rule else None,
        decision_path=list(cls.decision_path),
        detach=req.detach,
        script_sha256=pin.storage_sha if pin else None,
        script_paths=pin.paths_json if pin else None,
    )
    prompt_id = prompt_row["id"]
```

`reattached` is used in Task 4. Do not touch `_reprompt_script_changed` (~line 658): it keeps `db.create_pending_prompt`.

- [ ] **Step 4: Run them, then the FULL unit suite**

Run: `pixi run --frozen -e dev pytest tests/integration/test_wait_for_approval.py tests/integration/test_concurrency.py -v`, then the full unit suite: `pixi run --frozen -e dev pytest tests/unit -v`.
Expected: all PASS.

Existing tests that create two identical open prompts: **none found.** I checked every `== "prompt_required"` assertion in `tests/` (71 sites across 18 files: test_detach_approval, test_rule_match_telemetry, test_deny_revokes_rules, test_runner_manifest_binding, test_env_loader_approvals, test_wait_for_approval, test_template_approval_flow, test_server, test_scoped_approvals, test_script_content_binding, test_path_guard_approvals, test_inline_code_not_promotable, test_git_dir_write_guard, test_heredoc_tail, test_background_execution, test_verb_approval_flow, test_detach_cap, test_concurrency). Each second identical call comes after an approve, deny or consume of the first prompt, or uses a different command, agent or detach value. The script-changed reprompt rows carry `script_changed_since_approval` in their `decision_path`, so they never match. If the full run still shows a test that relied on two rows for identical open calls, update it to the dedup expectation and name it in the commit body.

QG with `<task files>` = `src/shell_runner/server.py tests/integration/test_wait_for_approval.py tests/integration/test_concurrency.py tests/unit/test_script_content_binding.py tests/unit/test_no_sync_db_writes_in_async.py`.

- [ ] **Step 5: Commit**

```bash
git add src/shell_runner/server.py tests/integration/test_wait_for_approval.py tests/integration/test_concurrency.py tests/unit/test_script_content_binding.py tests/unit/test_no_sync_db_writes_in_async.py
git commit -m "feat(execute): re-attach identical calls to one open prompt (#119)"
```

---

### Task 4: `"reattached"` tag in the audit `decision_path`

**Files:**
- Modify: `src/shell_runner/server.py`: `execute_route`, the telemetry block right after the Task 3 call (~lines 1438-1484)
- Test: `tests/integration/test_wait_for_approval.py`

- [ ] **Step 1: Write the failing test**

In `tests/integration/test_wait_for_approval.py`, add `import json` on its own line directly before `from pathlib import Path` (it is the first stdlib import; plain imports precede `from` imports). Then append:

```python
def test_reattached_call_is_tagged_in_shell_calls(client: TestClient, tmp_path: Path) -> None:
    pid = _request_prompt(client, tmp_path)
    _request_prompt(client, tmp_path)
    rows = srv.db.telemetry_query(agent_id=AGENT, decision="prompt_required")
    paths = [json.loads(r["decision_path_json"]) for r in rows]
    tagged = [p for p in paths if "reattached" in p]
    assert len(paths) == 2
    assert len(tagged) == 1
    assert tagged[0][-1] == "reattached"
    # the prompt row keeps the untagged path, so it stays the match key
    with srv.db._conn() as conn:
        stored = conn.execute(
            "SELECT decision_path_json FROM pending_prompts WHERE id = ?", (pid,)
        ).fetchone()[0]
    assert "reattached" not in json.loads(stored)
    assert _request_prompt(client, tmp_path) == pid
```

- [ ] **Step 2: Run it and see it fail**

Run: `pixi run --frozen -e dev pytest tests/integration/test_wait_for_approval.py::test_reattached_call_is_tagged_in_shell_calls -v`
Expected: FAIL at `assert len(tagged) == 1` (`0 == 1`).

- [ ] **Step 3: Minimal implementation**

In `execute_route`, directly after the line `telemetry_id = str(uuid.uuid4())` that follows `prompt_id = prompt_row["id"]` (and before `if telemetry_writer is not None:`), add:

```python
    # issue #119: tag the audit row of a re-attached call. The prompt row keeps the
    # untagged path, so later identical calls still match it.
    call_path = [*cls.decision_path, "reattached"] if reattached else list(cls.decision_path)
```

Then, **only in the two telemetry calls of this block** (the `telemetry_writer.submit(...)` and the `db.record_call` under `else:`, both with `decision="prompt_required"`, between `call_path = ...` and `why = (`), replace `decision_path=list(cls.decision_path),` with `decision_path=call_path,`. Leave every other `decision_path=list(cls.decision_path)` in the file alone.

- [ ] **Step 4: Run it and see it pass**

Run: `pixi run --frozen -e dev pytest tests/integration/test_wait_for_approval.py -v`
Expected: all PASS.
QG with `<task files>` = `src/shell_runner/server.py tests/integration/test_wait_for_approval.py`.

- [ ] **Step 5: Commit**

```bash
git add src/shell_runner/server.py tests/integration/test_wait_for_approval.py
git commit -m "feat(audit): tag re-attached shell_execute calls in decision_path (#119)"
```

---

### Task 5: `approval_wait_s` model field, prompt fields, MCP schema

**Files:**
- Modify: `src/shell_runner/models.py`: alias after `MAX_TIMEOUT_S` (~line 15); `ExecuteRequest` after `detach` (~line 51); `ExecutePromptInfo` (~lines 54-62)
- Modify: `src/shell_runner/mcp_wrapper.py`: `shell_execute` description (~lines 19-38) and `properties` after `detach` (~line 95)
- Modify: `src/shell_runner/server.py`: `ExecutePromptInfo(...)` at the end of `execute_route` (~line 1510)
- Test: `tests/unit/test_models.py`, `tests/integration/test_mcp_endpoint.py`, `tests/integration/test_wait_for_approval.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_models.py` (no new module imports). The tests import inside the function because other tests in this file delete `shell_runner.models` from `sys.modules`.

```python
def test_execute_request_approval_wait_s_is_optional() -> None:
    from shell_runner.models import ExecuteRequest

    assert ExecuteRequest(command="ls", cwd="/tmp", agent_id="a").approval_wait_s is None
    req = ExecuteRequest(command="ls", cwd="/tmp", agent_id="a", approval_wait_s=5)
    assert req.approval_wait_s == 5


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_execute_request_rejects_non_finite_approval_wait_s(bad: float) -> None:
    from shell_runner.models import ExecuteRequest

    with pytest.raises(ValidationError):
        ExecuteRequest(command="ls", cwd="/tmp", agent_id="a", approval_wait_s=bad)


def test_prompt_info_wait_fields_default_to_unset() -> None:
    from shell_runner.models import ExecutePromptInfo

    info = ExecutePromptInfo(id="p", raw_cmd="x", template="x", category=None, tier=3, why="w")
    assert (info.reattached, info.state, info.waited_s) == (False, None, None)
```

Append to `tests/integration/test_mcp_endpoint.py` (no new imports):

```python
def test_shell_execute_schema_exposes_approval_wait_s(client: TestClient) -> None:
    resp = _post(client, "tools/list", req_id=2)
    execute_tool = next(t for t in resp["result"]["tools"] if t["name"] == "shell_execute")
    prop = execute_tool["inputSchema"]["properties"]["approval_wait_s"]
    assert prop["type"] == ["number", "null"]
    assert "approval_wait_s" in execute_tool["description"]
```

Append to `tests/integration/test_wait_for_approval.py` (no new imports):

```python
def test_response_reports_reattached(client: TestClient, tmp_path: Path) -> None:
    body = {"command": COMMAND, "cwd": str(tmp_path), "agent_id": AGENT}
    first = client.post("/execute", json=body).json()["prompt"]
    second = client.post("/execute", json=body).json()["prompt"]
    assert (first["reattached"], second["reattached"]) == (False, True)
```

- [ ] **Step 2: Run them and see them fail**

Run: `pixi run --frozen -e dev pytest tests/unit/test_models.py tests/integration/test_mcp_endpoint.py::test_shell_execute_schema_exposes_approval_wait_s tests/integration/test_wait_for_approval.py::test_response_reports_reattached -v`
Expected:
- `test_execute_request_approval_wait_s_is_optional` FAILS with `AttributeError: 'ExecuteRequest' object has no attribute 'approval_wait_s'`. Pydantic ignores unknown fields by default, so the constructor does not raise.
- The non-finite test FAILS with `DID NOT RAISE`.
- The prompt-info test FAILS with `AttributeError` (`reattached`).
- The MCP test FAILS with `KeyError: 'approval_wait_s'`.
- `test_response_reports_reattached` FAILS with `KeyError: 'reattached'`.

- [ ] **Step 3: Minimal implementation**

`src/shell_runner/models.py`: directly after the line `MAX_TIMEOUT_S = int(os.environ.get(...))`, add:

```python
# issue #119: where a prompt stands after shell_execute(approval_wait_s) waited on it.
ExecutePromptState = Literal["approved", "denied", "expired", "consumed", "waiting"]
```

(`Literal` is already imported from `typing`.) In `ExecuteRequest`, after the `detach` field, add:

```python
    approval_wait_s: float | None = Field(
        default=None,
        allow_inf_nan=False,
        description=(
            "Opt-in (issue #119): on prompt_required, wait up to this many seconds for the "
            "approver's decision. Clamped to [0, 15] and to an 18s per-call deadline. "
            "None or 0 returns at once."
        ),
    )
```

In `ExecutePromptInfo`, after `detach: bool = False`, add:

```python
    # issue #119: reattached is True when this call reused an identical open prompt;
    # state and waited_s are set only when approval_wait_s made the call wait.
    reattached: bool = False
    state: ExecutePromptState | None = None
    waited_s: float | None = None
```

`src/shell_runner/mcp_wrapper.py`, `shell_execute` description: after the string line ending `"Stop if state is 'denied' or 'expired'. "`, insert:

```python
            "Alternatively pass approval_wait_s (max 15) to wait inside shell_execute: the "
            "response's prompt.state is approved|denied|expired|consumed|waiting and "
            "prompt.next_steps says what to do; identical calls re-attach to the same open "
            "prompt. "
```

In `properties`, after the `"detach": {...},` entry, add:

```python
                "approval_wait_s": {
                    "type": ["number", "null"],
                    "description": (
                        "Opt-in: if the command needs approval, wait up to this many seconds "
                        "for the decision (clamped to [0, 15] and to an 18s per-call "
                        "deadline). Omit or 0 to return at once. On state 'approved' "
                        "re-issue the identical call once to run it; on 'waiting' re-issue "
                        "it to keep waiting (it re-attaches to the same prompt); stop on "
                        "'denied' or 'expired'."
                    ),
                },
```

`src/shell_runner/server.py`: in the `ExecutePromptInfo(...)` at the end of `execute_route`, after `detach=req.detach,` add `reattached=reattached,`.

- [ ] **Step 4: Run them and see them pass**

Run: `pixi run --frozen -e dev pytest tests/unit/test_models.py tests/integration/test_mcp_endpoint.py tests/integration/test_wait_for_approval.py -v`
Expected: all PASS.
QG with `<task files>` = `src/shell_runner/models.py src/shell_runner/mcp_wrapper.py src/shell_runner/server.py tests/unit/test_models.py tests/integration/test_mcp_endpoint.py tests/integration/test_wait_for_approval.py`.

- [ ] **Step 5: Commit**

```bash
git add src/shell_runner/models.py src/shell_runner/mcp_wrapper.py src/shell_runner/server.py tests/unit/test_models.py tests/integration/test_mcp_endpoint.py tests/integration/test_wait_for_approval.py
git commit -m "feat(models): approval_wait_s request field and prompt reattached/state (#119)"
```

---

### Task 6: Extract the shared semaphore helper `_bounded_prompt_wait`

**Files:**
- Modify: `src/shell_runner/server.py`: new helper after `_wait_for_prompt_decision` (~line 2037); `_wait_for_prompt` semaphore block (~lines 2054-2059)
- Test: `tests/unit/test_shell_wait.py`

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_shell_wait.py`, add `import time` directly after the line `import asyncio` (before `import uuid`). Then append:

```python
def _make_prompt(db: Persistence, tmp_path: Path) -> str:
    return db.create_pending_prompt(
        agent_id="test-agent",
        cwd=str(tmp_path),
        raw_cmd="mytool --flag",
        normalized_template="mytool --flag",
        command_tier=3,
        matched_rule_category=None,
        decision_path=[],
    )


async def test_bounded_prompt_wait_saturated_does_single_check(
    db: Persistence, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_module, "_wait_semaphore", asyncio.Semaphore(0))
    pid = _make_prompt(db, tmp_path)
    started = time.monotonic()
    row = await server_module._bounded_prompt_wait(pid, 5)
    assert row is not None
    assert row["id"] == pid
    assert time.monotonic() - started < 1


async def test_prompt_mode_shell_wait_uses_shared_helper(
    db: Persistence, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _make_prompt(db, tmp_path)
    seen: list[tuple[str, float]] = []

    async def _spy(prompt_id: str, wait_s: float) -> dict | None:
        seen.append((prompt_id, wait_s))
        return db.get_pending_prompt(prompt_id)

    monkeypatch.setattr(server_module, "_bounded_prompt_wait", _spy)
    resp = await shell_wait_route(ShellWaitRequest(prompt_id=pid, agent_id="test-agent", wait_s=3))
    assert resp.state == "waiting"
    assert seen == [(pid, 3.0)]
```

- [ ] **Step 2: Run them and see them fail**

Run: `pixi run --frozen -e dev pytest tests/unit/test_shell_wait.py -k "bounded_prompt_wait or shared_helper" -v`
Expected: both FAIL with `AttributeError: module 'shell_runner.server' has no attribute '_bounded_prompt_wait'` (from the call, and from `monkeypatch.setattr`'s default `raising=True`).

- [ ] **Step 3: Minimal implementation**

In `src/shell_runner/server.py`, after `_wait_for_prompt_decision`, add:

```python
async def _bounded_prompt_wait(prompt_id: str, wait_s: float) -> dict | None:
    """Wait for a prompt decision under the shared wait semaphore (shell_wait and
    shell_execute). Saturated: a single check rather than queueing behind other waiters."""
    if _wait_semaphore.locked():
        return await _wait_for_prompt_decision(prompt_id, 0.0)
    async with _wait_semaphore:
        return await _wait_for_prompt_decision(prompt_id, wait_s)
```

In `_wait_for_prompt`, replace:

```python
    # Saturated: do a single check rather than queueing behind other waiters.
    if _wait_semaphore.locked():
        row = await _wait_for_prompt_decision(prompt_id, 0.0)
    else:
        async with _wait_semaphore:
            row = await _wait_for_prompt_decision(prompt_id, wait_s)
```

with:

```python
    row = await _bounded_prompt_wait(prompt_id, wait_s)
```

The agent-id check and the `ShellWaitResponse` shaping stay in `_wait_for_prompt`. The job-mode semaphore block in `shell_wait_route` is out of scope and stays as it is.

- [ ] **Step 4: Run them and see them pass**

Run: `pixi run --frozen -e dev pytest tests/unit/test_shell_wait.py tests/integration/test_wait_for_approval.py -v`
Expected: all PASS.
QG with `<task files>` = `src/shell_runner/server.py tests/unit/test_shell_wait.py`.

- [ ] **Step 5: Commit**

```bash
git add src/shell_runner/server.py tests/unit/test_shell_wait.py
git commit -m "refactor(wait): extract shared _bounded_prompt_wait helper (#119)"
```

---

### Task 7: The wait in `execute_route`: deadline budget and state mapping

**Files:**
- Modify: `src/shell_runner/server.py`: import `ExecutePromptState` (models import block ~line 59); new constant and helpers after `_bounded_prompt_wait`; the final `return ExecuteResponse(...)` of `execute_route` (~lines 1499-1526)
- Test: `tests/unit/test_shell_wait.py`, `tests/integration/test_wait_for_approval.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_shell_wait.py` (no new imports):

```python
@pytest.mark.parametrize(
    ("requested", "elapsed", "expected"),
    [
        (5, 0, 5),
        (9999, 0, MAX_WAIT_S),  # clamp to MAX_WAIT_S
        (15, 10, 8),  # deadline 18 minus elapsed
        (15, 18, 0),  # deadline reached: single check
        (15, 30, 0),  # floored at 0
        (0, 0, 0),
        (-3, 0, 0),
    ],
)
def test_approval_wait_budget(requested: float, elapsed: float, expected: float) -> None:
    assert server_module._approval_wait_budget(requested, elapsed) == pytest.approx(expected)


def test_approval_wait_deadline_is_18s() -> None:
    assert server_module.APPROVAL_WAIT_DEADLINE_S == 18


def _row(**overrides: object) -> dict:
    row: dict = {
        "approve_decision": None,
        "approve_token": None,
        "consumed_at": None,
        "expires_at": "9999-12-31T00:00:00+00:00",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (None, "expired"),  # swept by cleanup
        (_row(), "waiting"),
        (_row(approve_decision="approve_once", approve_token="t"), "approved"),
        (_row(approve_decision="deny"), "denied"),
        (
            _row(approve_decision="approve_once", approve_token="t", consumed_at="2026-01-01"),
            "consumed",
        ),
        (_row(expires_at="2000-01-01T00:00:00+00:00"), "expired"),
    ],
)
def test_execute_prompt_state_mapping(row: dict | None, expected: str) -> None:
    assert server_module._execute_prompt_state(row) == expected
```

In `tests/integration/test_wait_for_approval.py`, add `import asyncio` directly before `import json` (the first line of the stdlib block), and `from collections.abc import Callable` directly before `from pathlib import Path`. Then append these helpers and tests:

```python
# --- issue #119: approval_wait_s ----------------------------------------------


def _execute(client: TestClient, cwd: Path, **extra: object) -> dict:
    body = {"command": COMMAND, "cwd": str(cwd), "agent_id": AGENT, **extra}
    resp = client.post("/execute", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _on_poll(
    monkeypatch: pytest.MonkeyPatch, action: Callable[[str], object], at: int = 1
) -> None:
    """Run action(prompt_id) once, just before the at-th get_pending_prompt read."""
    original = srv.db.get_pending_prompt
    reads = {"n": 0}

    def hooked(prompt_id: str) -> dict | None:
        reads["n"] += 1
        if reads["n"] == at:
            action(prompt_id)
        return original(prompt_id)

    monkeypatch.setattr(srv.db, "get_pending_prompt", hooked)


def _expire(prompt_id: str) -> None:
    with srv.db._write_conn() as conn:
        conn.execute(
            "UPDATE pending_prompts SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", prompt_id),
        )


def _sweep(prompt_id: str) -> None:
    with srv.db._write_conn() as conn:
        conn.execute("DELETE FROM pending_prompts WHERE id = ?", (prompt_id,))


def _spy_wait(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    seen: list[float] = []

    async def _spy(prompt_id: str, wait_s: float) -> dict | None:
        seen.append(wait_s)
        return srv.db.get_pending_prompt(prompt_id)

    monkeypatch.setattr(srv, "_bounded_prompt_wait", _spy)
    return seen


def test_omitted_or_zero_wait_returns_at_once(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _spy_wait(monkeypatch)
    for extra in ({}, {"approval_wait_s": 0}):
        prompt = _execute(client, tmp_path, **extra)["prompt"]
        assert prompt["state"] is None
        assert prompt["waited_s"] is None
        assert "shell_wait(prompt_id=" in prompt["next_steps"]
    assert seen == []


def test_wait_is_clamped_to_max_wait(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _spy_wait(monkeypatch)
    prompt = _execute(client, tmp_path, approval_wait_s=9999)["prompt"]
    assert seen == [float(srv.MAX_WAIT_S)]
    assert prompt["state"] == "waiting"


def test_exhausted_deadline_means_single_check(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(srv, "APPROVAL_WAIT_DEADLINE_S", 0)
    seen = _spy_wait(monkeypatch)
    prompt = _execute(client, tmp_path, approval_wait_s=5)["prompt"]
    assert seen == [0.0]
    assert prompt["state"] == "waiting"


def test_saturated_semaphore_returns_waiting_immediately(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(srv, "_wait_semaphore", asyncio.Semaphore(0))
    prompt = _execute(client, tmp_path, approval_wait_s=5)["prompt"]
    assert prompt["state"] == "waiting"
    assert prompt["waited_s"] < 1


def test_short_wait_without_decision_is_waiting(client: TestClient, tmp_path: Path) -> None:
    data = _execute(client, tmp_path, approval_wait_s=0.05)
    assert data["decision"] == "prompt_required"
    assert data["prompt"]["state"] == "waiting"
    assert data["prompt"]["waited_s"] >= 0


def test_wait_reports_denied(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on_poll(monkeypatch, lambda pid: srv.db.approve_prompt(prompt_id=pid, decision="deny"))
    data = _execute(client, tmp_path, approval_wait_s=5)
    assert data["decision"] == "denied"
    assert data["prompt"]["state"] == "denied"


def test_wait_reports_consumed(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def approve_and_consume(pid: str) -> None:  # a concurrent identical call used it
        srv.db.approve_prompt(prompt_id=pid, decision="approve_once")
        srv.db.consume_approval_by_command(raw_cmd=COMMAND, cwd=str(tmp_path), agent_id=AGENT)

    _on_poll(monkeypatch, approve_and_consume)
    data = _execute(client, tmp_path, approval_wait_s=5)
    assert data["decision"] == "prompt_required"
    assert data["prompt"]["state"] == "consumed"


@pytest.mark.parametrize("action", [_expire, _sweep])
def test_wait_reports_expired_and_swept_as_expired(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: Callable[[str], None],
) -> None:
    _on_poll(monkeypatch, action)
    data = _execute(client, tmp_path, approval_wait_s=5)
    assert data["decision"] == "prompt_required"
    assert data["prompt"]["state"] == "expired"
```

- [ ] **Step 2: Run them and see them fail**

Run: `pixi run --frozen -e dev pytest tests/unit/test_shell_wait.py tests/integration/test_wait_for_approval.py -v`
Expected:
- The unit tests FAIL with `AttributeError: module 'shell_runner.server' has no attribute '_approval_wait_budget'` / `'APPROVAL_WAIT_DEADLINE_S'` / `'_execute_prompt_state'`.
- In the integration file, `test_wait_is_clamped_to_max_wait` FAILS with `assert [] == [15.0]`, and the state tests FAIL with `assert None == 'denied'` etc.
- `test_exhausted_deadline_means_single_check` FAILS at `monkeypatch.setattr` (`AttributeError`, no such attribute).
- `test_omitted_or_zero_wait_returns_at_once` PASSES (guard).

- [ ] **Step 3: Minimal implementation**

`src/shell_runner/server.py`, in the `from .models import (...)` block, add `ExecutePromptState,` on its own line directly after `ExecutePromptInfo,` (before `ExecuteRequest,`).

After `_bounded_prompt_wait`, add:

```python
# issue #119 — shell_execute(approval_wait_s). The wait must fit the ~20s MCP transport
# cutoff (#46) with a 2s margin; the inline budget setting is deliberately not used.
APPROVAL_WAIT_DEADLINE_S = 18

# _prompt_wait_state reports a spent approval as "error"; shell_execute calls it "consumed".
_EXECUTE_PROMPT_STATES: dict[str, ExecutePromptState] = {
    "approved": "approved",
    "denied": "denied",
    "expired": "expired",
    "waiting": "waiting",
    "error": "consumed",
}


def _approval_wait_budget(requested_s: float, elapsed_s: float) -> float:
    """max(0, min(requested, MAX_WAIT_S, APPROVAL_WAIT_DEADLINE_S - elapsed)); 0 = one check."""
    return max(0.0, min(requested_s, float(MAX_WAIT_S), APPROVAL_WAIT_DEADLINE_S - elapsed_s))


def _execute_prompt_state(row: dict | None) -> ExecutePromptState:
    """shell_execute's view of its prompt after the wait; a swept row (None) is expired."""
    return "expired" if row is None else _EXECUTE_PROMPT_STATES[_prompt_wait_state(row)]


async def _wait_on_prompt(
    resp: ExecuteResponse, prompt: ExecutePromptInfo, requested_s: float, t0: float
) -> ExecuteResponse:
    """Opt-in bounded wait for the decision on resp's prompt (issue #119).

    Never executes: an approved command runs when the caller re-issues the identical
    call, so wait plus execution never share one call's ~20s budget.
    """
    wait_s = _approval_wait_budget(requested_s, time.monotonic() - t0)
    started = time.monotonic()
    state = _execute_prompt_state(await _bounded_prompt_wait(prompt.id, wait_s))
    waited = prompt.model_copy(update={"state": state, "waited_s": time.monotonic() - started})
    decision = "denied" if state == "denied" else "prompt_required"
    return resp.model_copy(update={"decision": decision, "prompt": waited})
```

At the end of `execute_route`, replace the whole `return ExecuteResponse(decision="prompt_required", ... prompt=ExecutePromptInfo(...),)` with the following. The `next_steps` text is unchanged and `reattached=reattached` comes from Task 5.

```python
    prompt_info = ExecutePromptInfo(
        id=prompt_id,
        raw_cmd=req.command,
        template=cls.template,
        category=cls.matched_rule.category if cls.matched_rule else None,
        tier=int(cls.tier),
        why=why,
        next_steps=(
            f"A primary session must approve prompt {prompt_id} via shell_approve_pending. "
            f"Meanwhile call shell_wait(prompt_id='{prompt_id}', agent_id='<your agent_id>') "
            "repeatedly until state is 'approved', then re-run shell_execute with the "
            "identical command, cwd and agent_id plus approve_token. "
            "Stop if state is 'denied' or 'expired'." + detach_rerun
        ),
        detach=req.detach,
        reattached=reattached,
    )
    resp = ExecuteResponse(
        decision="prompt_required",
        tier=int(cls.tier),
        matched_rule=cls.matched_rule.pattern if cls.matched_rule else None,
        exit_code=None,
        stdout="",
        stderr="",
        stdout_full_path=None,
        stderr_full_path=None,
        duration_ms=duration_ms,
        telemetry_id=telemetry_id,
        prompt=prompt_info,
    )
    if not req.approval_wait_s:  # None or 0: return at once, as before #119
        return resp
    return await _wait_on_prompt(resp, prompt_info, req.approval_wait_s, t0)
```

The audit row was already written above with `decision="prompt_required"`. The wait does not update it (Spec deviation 5).

- [ ] **Step 4: Run them and see them pass**

Run: `pixi run --frozen -e dev pytest tests/unit/test_shell_wait.py tests/integration/test_wait_for_approval.py tests/unit/test_no_sync_db_writes_in_async.py -v`
Expected: all PASS.
QG with `<task files>` = `src/shell_runner/server.py tests/unit/test_shell_wait.py tests/integration/test_wait_for_approval.py`.

- [ ] **Step 5: Commit**

```bash
git add src/shell_runner/server.py tests/unit/test_shell_wait.py tests/integration/test_wait_for_approval.py
git commit -m "feat(execute): opt-in approval_wait_s with 18s deadline and state mapping (#119)"
```

---

### Task 8: `next_steps` texts per state

**Files:**
- Modify: `src/shell_runner/server.py`: `_WAIT_NEXT_STEPS` after `_EXECUTE_PROMPT_STATES`; one statement in `_wait_on_prompt`
- Test: `tests/integration/test_wait_for_approval.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_wait_for_approval.py` (no new imports):

```python
_DECIDE: dict[str, Callable[[str], object]] = {
    "approved": lambda pid: srv.db.approve_prompt(prompt_id=pid, decision="approve_once"),
    "denied": lambda pid: srv.db.approve_prompt(prompt_id=pid, decision="deny"),
    "expired": _expire,
}


@pytest.mark.parametrize(
    ("state", "needle"),
    [
        ("approved", "Re-issue the identical shell_execute call"),
        ("denied", "Stop: do not re-issue"),
        ("expired", "expired before a decision. Stop"),
    ],
)
def test_next_steps_for_decided_states(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    needle: str,
) -> None:
    _on_poll(monkeypatch, _DECIDE[state])
    prompt = _execute(client, tmp_path, approval_wait_s=5)["prompt"]
    assert prompt["state"] == state
    assert needle in prompt["next_steps"]
    assert "shell_wait" not in prompt["next_steps"]


def test_next_steps_when_still_waiting(client: TestClient, tmp_path: Path) -> None:
    prompt = _execute(client, tmp_path, approval_wait_s=0.05)["prompt"]
    assert prompt["state"] == "waiting"
    assert f"re-attaches to prompt {prompt['id']}" in prompt["next_steps"]
    assert "approval_wait_s" in prompt["next_steps"]


def test_next_steps_when_consumed(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def approve_and_consume(pid: str) -> None:
        srv.db.approve_prompt(prompt_id=pid, decision="approve_once")
        srv.db.consume_approval_by_command(raw_cmd=COMMAND, cwd=str(tmp_path), agent_id=AGENT)

    _on_poll(monkeypatch, approve_and_consume)
    prompt = _execute(client, tmp_path, approval_wait_s=5)["prompt"]
    assert prompt["state"] == "consumed"
    assert prompt["next_steps"] == (
        "This approval was already used by an earlier identical call; re-issue to request "
        "a new approval."
    )
```

- [ ] **Step 2: Run them and see them fail**

Run: `pixi run --frozen -e dev pytest tests/integration/test_wait_for_approval.py -k next_steps -v`
Expected: FAIL, because `next_steps` is still the shell_wait text (`assert 'shell_wait' not in ...`, and `re-attaches to prompt` is missing).

- [ ] **Step 3: Minimal implementation**

After `_EXECUTE_PROMPT_STATES` in `src/shell_runner/server.py`, add:

```python
_WAIT_NEXT_STEPS: dict[ExecutePromptState, str] = {
    "approved": (
        "Prompt {id} was approved. Re-issue the identical shell_execute call (same command, "
        "cwd, agent_id and detach) once to run it; no approve_token is needed."
    ),
    "denied": "Prompt {id} was denied. Stop: do not re-issue this command.",
    "expired": "Prompt {id} expired before a decision. Stop.",
    "consumed": (
        "This approval was already used by an earlier identical call; re-issue to request "
        "a new approval."
    ),
    "waiting": (
        "No decision yet on prompt {id}. Re-issue the identical call (same command, cwd, "
        "agent_id, approval_wait_s) to keep waiting; it re-attaches to prompt {id}. "
        "Stop if state is 'denied' or 'expired'."
    ),
}
```

In `_wait_on_prompt`, replace the `waited = prompt.model_copy(...)` statement with:

```python
    waited = prompt.model_copy(
        update={
            "state": state,
            "waited_s": time.monotonic() - started,
            "next_steps": _WAIT_NEXT_STEPS[state].format(id=prompt.id),
        }
    )
```

- [ ] **Step 4: Run them and see them pass**

Run: `pixi run --frozen -e dev pytest tests/integration/test_wait_for_approval.py -v`
Expected: all PASS.
QG with `<task files>` = `src/shell_runner/server.py tests/integration/test_wait_for_approval.py`.

- [ ] **Step 5: Commit**

```bash
git add src/shell_runner/server.py tests/integration/test_wait_for_approval.py
git commit -m "feat(execute): per-state next_steps after an approval wait (#119)"
```

---

### Task 9: Reusable approvals during a wait: template (keeps running) and detach (downgraded, single-use)

**Files:**
- Modify (test helper): `tests/integration/test_wait_for_approval.py` `_decide` (~line 42)
- Test: `tests/integration/test_wait_for_approval.py`, `tests/unit/test_detach_approval.py`

**Code facts checked for this task:**
- (a) `/approve_pending` with `approve_template` issues an `approve_token`. `decide_prompt` sets `token = str(uuid.uuid4())` for every non-deny decision (persistence.py:849-850) and writes it with `approve_decision` in the non-approve_once `UPDATE` (persistence.py:932-943). The handler returns it as `approve_token` (server.py:1848, 1863). With `approve_decision` and `approve_token` both set, `_prompt_wait_state` returns `"approved"` (server.py:2021-2022). So the waiter sees `approved`.
- (b) `promote_to_tier=2` is accepted. `_validate_promotion_tier` (server.py:1608-1638; called at 1792) defaults to 2. It rejects a tier `>= command_tier` (1618), `<= DENY` (1626), and anything more permissive than AUTO_CAPPED (1631). For a T3 command (`expr 40 + 2`, command_tier 3), 2 is the only valid value; for T4, 2 or 3.
- The detach downgrade happens in the handler (`guarded_note` at server.py:1753-1757, `effective_decision = "approve_once"` at 1829), so the detach test must go through `/approve_pending`.

- [ ] **Step 1: Write the tests (helpers are new code, so TDD applies to them via the tests below)**

In `tests/integration/test_wait_for_approval.py`, add `import threading` directly after the line `import json` (before `from collections.abc import Callable`). Do not add `import time` here: nothing in this file uses it, and ruff would flag it (F401). Replace the existing `_decide` helper with this backward-compatible version:

```python
def _decide(
    client: TestClient, prompt_id: str, decision: str = "approve_once", **extra: object
) -> dict:
    resp = client.post(
        "/approve_pending",
        json={
            "prompt_id": prompt_id,
            "decision": decision,
            "approver_agent_id": "primary",
            **extra,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()
```

Append:

```python
TEMPLATE_AGENT = "focused-ghc-ci-analyzer"  # ALWAYS_APPROVE cap: a T2 template rule auto-runs


def _execute_in_thread(
    client: TestClient, cwd: Path, **extra: object
) -> tuple[threading.Thread, dict]:
    out: dict = {}
    body = {"command": COMMAND, "cwd": str(cwd), "agent_id": AGENT, **extra}

    def run() -> None:
        resp = client.post("/execute", json=body)
        out["status"], out["body"] = resp.status_code, resp.json()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, out


def _join(handle: tuple[threading.Thread, dict]) -> dict:
    thread, out = handle
    thread.join(timeout=30)
    assert out.get("status") == 200, out
    return out["body"]


def _gate_waiters(monkeypatch: pytest.MonkeyPatch, n: int) -> list[threading.Event]:
    """One Event per waiter, set when that call enters the shared wait helper."""
    entered = [threading.Event() for _ in range(n)]
    pending = list(entered)
    original = srv._bounded_prompt_wait

    async def _gated(prompt_id: str, wait_s: float) -> dict | None:
        pending.pop(0).set()
        return await original(prompt_id, wait_s)

    monkeypatch.setattr(srv, "_bounded_prompt_wait", _gated)
    return entered


def _open_prompt_after(entered: list[threading.Event]) -> str:
    assert all(e.wait(timeout=10) for e in entered), "waiter(s) never entered the wait"
    return srv.db.list_pending_prompts()[0]["id"]


def test_template_approval_during_wait_keeps_running(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = _gate_waiters(monkeypatch, 1)
    waiter = _execute_in_thread(client, tmp_path, agent_id=TEMPLATE_AGENT, approval_wait_s=5)
    approved = _decide(
        client, _open_prompt_after(entered), "approve_template", promote_to_tier=2
    )
    assert approved["template_promoted"] is True
    assert approved["approve_token"]
    assert _join(waiter)["prompt"]["state"] == "approved"

    for _ in range(2):  # nothing is consumed: later identical calls keep running
        ran = _execute(client, tmp_path, agent_id=TEMPLATE_AGENT)
        assert ran["decision"] == "executed"
        assert ran["stdout"].strip() == "42"
```

The re-calls omit `approval_wait_s`: once the template rule applies, the call executes before any prompt or wait, and `_gate_waiters` sized for one waiter would raise `IndexError` on a second wait.

In `tests/unit/test_detach_approval.py`, add `import threading` and then `import time` directly before the line `from pathlib import Path` (they become the first stdlib lines). Then append:

```python
# --- issue #119: approval_wait_s with detach --------------------------------------


def _await_open_prompt() -> str:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        rows = srv.db.list_pending_prompts()
        if rows:
            return rows[0]["id"]
        time.sleep(0.01)
    raise AssertionError("no pending prompt appeared")


def test_detach_wait_template_approval_is_single_use(
    client: TestClient,
    calls: list[dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(srv, "WAIT_POLL_INTERVAL_S", 0.01)
    out: dict[str, Any] = {}

    def wait_call() -> None:
        out["resp"] = _post(client, T3_CMD, tmp_path, detach=True, approval_wait_s=5)

    waiter = threading.Thread(target=wait_call)
    waiter.start()
    # through the approve endpoint: the downgrade lives in the handler, not decide_prompt
    approved = _approve(client, _await_open_prompt(), "approve_template")
    waiter.join(timeout=30)

    assert approved["template_promoted"] is False
    assert "downgraded to a one-shot approval" in (approved["scope_warning"] or "")
    assert out["resp"].json()["prompt"]["state"] == "approved"
    assert _post(client, T3_CMD, tmp_path, detach=True).json()["decision"] == "executed"
    assert len(calls) == 1
    assert _post(client, T3_CMD, tmp_path, detach=True).json()["decision"] == "prompt_required"
    assert len(calls) == 1
```

- [ ] **Step 2: Run them**

Run: `pixi run --frozen -e dev pytest tests/integration/test_wait_for_approval.py tests/unit/test_detach_approval.py -v`
Expected: all PASS, including the existing tests that use `_decide`. The behaviour comes from Tasks 3-8; only test helpers are new. If one fails, fix the earlier task named in the traceback and do not weaken the test. If the template re-call prompts instead of executing, check `approved["template_promoted"]` first.

- [ ] **Step 3: Implementation**

None in `src/`.

- [ ] **Step 4: Quality gate**

QG with `<task files>` = `tests/integration/test_wait_for_approval.py tests/unit/test_detach_approval.py`.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_wait_for_approval.py tests/unit/test_detach_approval.py
git commit -m "test(execute): template and detach approvals during an approval wait (#119)"
```

---

### Task 10: End-to-end outcome tests: approve_once, deny then re-call, two concurrent waiters

**Files:**
- Test: `tests/integration/test_wait_for_approval.py` (append; uses the Task 7-9 helpers, no new imports)

- [ ] **Step 1: Write the tests**

```python
def test_approve_once_wait_then_recall_runs_once(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # at=2: the first poll sees "waiting", the decision lands before the second
    _on_poll(monkeypatch, _DECIDE["approved"], at=2)
    waited = _execute(client, tmp_path, approval_wait_s=5)
    assert waited["decision"] == "prompt_required"
    assert waited["prompt"]["state"] == "approved"

    ran = _execute(client, tmp_path, approval_wait_s=5)  # identical re-call: consumes it
    assert ran["decision"] == "executed"
    assert ran["stdout"].strip() == "42"

    again = _execute(client, tmp_path)  # single use: a new prompt
    assert again["decision"] == "prompt_required"
    assert again["prompt"]["id"] != waited["prompt"]["id"]
    assert again["prompt"]["reattached"] is False


def test_deny_then_identical_recall_creates_fresh_prompt(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on_poll(monkeypatch, _DECIDE["denied"])
    denied = _execute(client, tmp_path, approval_wait_s=5)
    assert denied["decision"] == "denied"
    assert denied["prompt"]["state"] == "denied"

    again = _execute(client, tmp_path)  # no suppression window: the human may change their mind
    assert again["decision"] == "prompt_required"
    assert again["prompt"]["id"] != denied["prompt"]["id"]
    assert again["prompt"]["reattached"] is False
    assert [r["id"] for r in srv.db.list_pending_prompts()] == [again["prompt"]["id"]]


def test_two_waiters_one_approve_once_exactly_one_runs(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Deterministic: approve only after BOTH calls are inside the shared wait helper.
    entered = _gate_waiters(monkeypatch, 2)
    first = _execute_in_thread(client, tmp_path, approval_wait_s=5)
    second = _execute_in_thread(client, tmp_path, approval_wait_s=5)
    pid = _open_prompt_after(entered)
    _decide(client, pid)  # approve_once
    waited = [_join(first), _join(second)]
    assert [w["prompt"]["id"] for w in waited] == [pid, pid]
    assert sorted(w["prompt"]["reattached"] for w in waited) == [False, True]
    assert {w["prompt"]["state"] for w in waited} <= {"approved", "consumed"}

    reruns = [_execute_in_thread(client, tmp_path) for _ in waited]  # concurrent, no wait
    results = [_join(r) for r in reruns]
    executed = [r for r in results if r["decision"] == "executed"]
    assert len(executed) == 1
    other = next(r for r in results if r["decision"] != "executed")
    assert other["decision"] == "prompt_required"
    assert other["prompt"]["id"] != pid  # a consumed prompt is never re-attached
```

The re-calls in the two-waiter test omit `approval_wait_s`, so they never enter the gated helper.

- [ ] **Step 2: Run them**

Run: `pixi run --frozen -e dev pytest tests/integration/test_wait_for_approval.py -k "approve_once_wait or deny_then or two_waiters" -v`
Expected: all PASS (behaviour from Tasks 3-8; `consume_approval_by_command` is single-use under `BEGIN IMMEDIATE`). If one fails, fix the earlier task named in the traceback and do not weaken the test.

- [ ] **Step 3: Implementation**

None.

- [ ] **Step 4: Quality gate**

QG with `<task files>` = `tests/integration/test_wait_for_approval.py`.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_wait_for_approval.py
git commit -m "test(execute): approve_once, deny and two-waiter outcomes of approval_wait_s (#119)"
```

---

### Task 11: README: `approval_wait_s`, re-attach and states

**Files:**
- Modify: `README.md`: new section right before `## What auto-executes (no prompt)` (~line 138)

- [ ] **Step 1: Write the section**

Insert:

```markdown
## Waiting for approval inside `shell_execute` (`approval_wait_s`, #119)

A T3/T4 command returns `decision=prompt_required`. Agents without `shell_wait`
can instead pass `approval_wait_s` to `shell_execute`:

- `None`/`0` (default): return at once, as before.
- Otherwise the call waits up to `max(0, min(approval_wait_s, 15, 18 - elapsed))`
  seconds for the decision. 18s (`APPROVAL_WAIT_DEADLINE_S`) is the ~20s MCP
  transport cutoff (#46) minus a 2s margin; 0 means a single check. If 8 waits
  are already running (shared with `shell_wait`), it checks once and returns.
- The command never runs inside the waiting call. The agent re-issues the
  identical call (same command, cwd, agent_id, detach), and the classifier
  (template/verb rule) or the token-less one-shot approval runs it.

**Re-attach (always on).** An identical call re-uses the open prompt instead of
creating a new one (`prompt.reattached=true`, same id, expiry unchanged). It must
match on agent_id, command, cwd, detach, script pin (`script_sha256` and
`script_paths`, NULL-safe), template, tier, matched rule category and the full
decision path. Decided (approved or denied), consumed or expired prompts are
never re-used. The audit row of a re-attached call carries a `reattached` tag in
`decision_path`.

| `prompt.state` | `decision` | what to do |
|---|---|---|
| `approved` | `prompt_required` | re-issue the identical call once to run it |
| `denied` | `denied` | stop (a later identical call opens a fresh prompt) |
| `expired` | `prompt_required` | stop (also reported for a prompt swept by cleanup) |
| `consumed` | `prompt_required` | an earlier identical call used the approval; re-issue to request a new one |
| `waiting` | `prompt_required` | re-issue the identical call to keep waiting (re-attaches) |

`approve_once` (and every `detach=true` approval, since template/verb approvals
are downgraded for detached runs) is single-use: one re-call runs, the next
prompts again. `approve_template`, `approve_template_global` and `approve_verb`
keep later identical calls running.
```

- [ ] **Step 2: Check it with greps**

```bash
grep -c "approval_wait_s" README.md          # expect >= 3
grep -n "APPROVAL_WAIT_DEADLINE_S" README.md # expect 1 line, containing "18s"
grep -nE '^\| `(approved|denied|expired|consumed|waiting)` \|' README.md   # expect exactly 5 lines
grep -n "min(approval_wait_s, 15, 18 - elapsed)" README.md                # expect 1 line
```

Also check that the numbers match the code: `MAX_WAIT_S = 15`, `APPROVAL_WAIT_DEADLINE_S = 18` and `MAX_CONCURRENT_WAITS = 8` in server.py (`grep -nE "^(MAX_WAIT_S|APPROVAL_WAIT_DEADLINE_S|MAX_CONCURRENT_WAITS) =" src/shell_runner/server.py`).

- [ ] **Step 3: Quality gate**

QG with `<task files>` = `README.md` (no code change; the gate confirms nothing else moved).

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs(readme): approval_wait_s, prompt re-attach and wait states (#119)"
```

---

### Task 12: Full suite and acceptance

**Files:** none (verification only)

- [ ] **Step 1: Rebase if #118 has merged**

`git fetch origin && git rebase origin/development`. Resolve any conflicts in `server.py` without dropping the #117 guard.

- [ ] **Step 2: Full suite and gates**

```bash
pixi run --frozen -e dev test
pixi run --frozen -e quality-ci ruff check --fix src/ tests/
pixi run --frozen -e quality-ci ruff format src/ tests/
git status --short          # must show no changes; if the fixers changed files, commit them explicitly
pixi run --frozen -e quality-ci ruff check src/ tests/
pixi run --frozen -e quality-ci ruff format --check src/ tests/
pixi run --frozen -e quality-ci mypy src/
git diff --exit-code pixi.lock pyproject.toml
```

Expected: all green. The report states `git rev-parse HEAD` and the tree path `/home/memento/ClaudeCode/Servers/shell-runner/worktrees/features`.

- [ ] **Step 3: Confirm the hard constraints**

- `git diff origin/development -- pyproject.toml pixi.lock` is empty (no dependency or `[project]` edits).
- `_REATTACH_SQL` still compares `script_sha256 IS ?` and `script_paths IS ?`, and the approve handler's `UNPINNABLE` refusal (server.py ~1735-1740) is untouched (#95/#98 binding not weakened).
- `_reprompt_script_changed` still calls `db.create_pending_prompt`.

- [ ] **Step 4: Acceptance checklist (spec Part B)**

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
