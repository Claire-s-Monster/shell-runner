"""Script-executing commands: non-promotable and content-bound (issue #95).

Approvals match on raw_cmd/cwd/agent_id, so a script file could be rewritten between
approval and execution. Commands that run a script file are never promotable
(reusable approvals degrade to one-shot) and the script's sha256 is pinned on the
prompt: approve_once re-hashes before executing and a mismatch / missing file burns
the approval and re-prompts. Nothing real runs: _dispatch_approved is spied.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shell_runner import executor, script_pin
from shell_runner import server as srv
from shell_runner.classifier import classify
from shell_runner.models import ExecuteResponse
from shell_runner.persistence import Persistence

AGENT = "focused-code-modifier"  # AUTO_CAPPED cap
SCRIPT_CMD = "bash ./x.sh"


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Persistence:
    fresh = Persistence(db_path=tmp_path / "test.sqlite3")
    monkeypatch.setattr(srv, "db", fresh)
    return fresh


@pytest.fixture(autouse=True)
def _jail_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor, "_CWD_ROOTS", [tmp_path.resolve()])


@pytest.fixture
def db(_fresh_db: Persistence) -> Persistence:
    return _fresh_db


@pytest.fixture
def client() -> TestClient:
    return TestClient(srv.app)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    recorded: list[Any] = []

    async def _spy(req: Any, ctx: Any, *args: Any, **kwargs: Any) -> ExecuteResponse:
        recorded.append(req)
        return ExecuteResponse(
            decision="executed",
            tier=ctx.final_tier,
            matched_rule=None,
            exit_code=0,
            stdout="",
            stderr="",
            stdout_full_path=None,
            stderr_full_path=None,
            duration_ms=0,
            telemetry_id="spy",
        )

    monkeypatch.setattr(srv, "_dispatch_approved", _spy)
    return recorded


@pytest.fixture
def script(tmp_path: Path) -> Path:
    path = tmp_path / "x.sh"
    path.write_text("echo original\n")
    return path


def _preview(client: TestClient, command: str, cwd: Path) -> str:
    resp = client.post("/classify", json={"command": command, "cwd": str(cwd), "agent_id": AGENT})
    assert resp.status_code == 200
    return resp.json()["decision_preview"]


def _execute(client: TestClient, command: str, cwd: Path, **extra: str) -> dict:
    body = {"command": command, "cwd": str(cwd), "agent_id": AGENT, **extra}
    resp = client.post("/execute", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _approve(client: TestClient, prompt_id: str, decision: str = "approve_once") -> dict:
    resp = client.post(
        "/approve_pending",
        json={"prompt_id": prompt_id, "decision": decision, "approver_agent_id": "primary"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _count(db: Persistence, table: str) -> int:
    with sqlite3.connect(db.db_path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608


# --- A. shapes are non-promotable --------------------------------------------


@pytest.mark.parametrize(
    "template",
    [
        "bash ./x.sh",
        "bash ./x.sh arg",
        "bash -x x.sh",
        "sh {tmp}/x.sh",
        "python3 -u s.py",
        "node a.js",
        "./x.sh",
        "scripts/run.sh",
        ". x.sh",
    ],
)
def test_script_shapes_are_non_promotable(tmp_path: Path, template: str) -> None:
    for name in ("x.sh", "s.py", "a.js"):
        (tmp_path / name).write_text("echo hi\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run.sh").write_text("echo hi\n")
    command = template.format(tmp=tmp_path)
    assert classify(command, str(tmp_path), AGENT).non_promotable is True


# --- B. controls --------------------------------------------------------------


@pytest.mark.parametrize("command", ["bash -n x.sh", "python -I -m py_compile s.py"])
def test_syntax_checks_stay_auto_and_promotable(
    client: TestClient, tmp_path: Path, command: str
) -> None:
    (tmp_path / "x.sh").write_text("echo hi\n")
    (tmp_path / "s.py").write_text("x = 1\n")
    assert _preview(client, command, tmp_path) == "would_execute"
    assert classify(command, str(tmp_path), AGENT).non_promotable is False


def test_ordinary_t3_command_stays_promotable(client: TestClient, tmp_path: Path) -> None:
    command = "mytool_unknown --flag"
    assert _preview(client, command, tmp_path) == "would_prompt"
    assert classify(command, str(tmp_path), AGENT).non_promotable is False


def test_unresolvable_script_shape_is_non_promotable(tmp_path: Path) -> None:
    # operand is a shell variable / does not exist: cannot be pinned, so not promotable
    assert classify("bash $SCRIPT", str(tmp_path), AGENT).non_promotable is True
    assert classify("bash ./missing.sh", str(tmp_path), AGENT).non_promotable is True


# --- C. reusable approvals downgrade ------------------------------------------


@pytest.mark.parametrize(
    "decision", ["approve_template", "approve_template_global", "approve_verb"]
)
def test_reusable_approval_on_script_prompt_downgrades(
    client: TestClient, db: Persistence, script: Path, tmp_path: Path, decision: str
) -> None:
    prompt = _execute(client, SCRIPT_CMD, tmp_path)["prompt"]
    approved = _approve(client, prompt["id"], decision)

    assert approved["approve_token"] is not None
    assert approved["template_promoted"] is False
    assert approved["verb_promoted"] is False
    assert approved["promoted_tier"] is None
    assert "not pinned" in (approved["scope_warning"] or "")
    assert _count(db, "template_approvals") == 0
    assert _count(db, "verb_approvals") == 0


# --- D/E/F/G. content binding on approve_once ---------------------------------


def test_unchanged_script_tokenless_retry_executes(
    client: TestClient, calls: list[Any], script: Path, tmp_path: Path
) -> None:
    prompt = _execute(client, SCRIPT_CMD, tmp_path)["prompt"]
    _approve(client, prompt["id"])
    assert _execute(client, SCRIPT_CMD, tmp_path)["decision"] == "executed"
    assert len(calls) == 1


def test_rewritten_script_tokenless_retry_reprompts_and_burns(
    client: TestClient, calls: list[Any], script: Path, tmp_path: Path
) -> None:
    prompt = _execute(client, SCRIPT_CMD, tmp_path)["prompt"]
    _approve(client, prompt["id"])
    script.write_text("echo rewritten\n")

    first = _execute(client, SCRIPT_CMD, tmp_path)
    assert first["decision"] == "prompt_required"
    assert first["prompt"]["id"] != prompt["id"]
    assert calls == []
    assert _execute(client, SCRIPT_CMD, tmp_path)["decision"] == "prompt_required"
    assert calls == []


def test_rewritten_script_with_token_reprompts_and_burns(
    client: TestClient, calls: list[Any], script: Path, tmp_path: Path
) -> None:
    prompt = _execute(client, SCRIPT_CMD, tmp_path)["prompt"]
    token = _approve(client, prompt["id"])["approve_token"]
    script.write_text("echo rewritten\n")

    resp = client.post(
        "/execute",
        json={
            "command": SCRIPT_CMD,
            "cwd": str(tmp_path),
            "agent_id": AGENT,
            "approve_token": token,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["decision"] == "prompt_required"
    assert calls == []
    # burned: the original content does not revive the approval
    script.write_text("echo original\n")
    assert _execute(client, SCRIPT_CMD, tmp_path)["decision"] == "prompt_required"
    assert calls == []


def test_deleted_script_reprompts(
    client: TestClient, calls: list[Any], script: Path, tmp_path: Path
) -> None:
    prompt = _execute(client, SCRIPT_CMD, tmp_path)["prompt"]
    _approve(client, prompt["id"])
    script.unlink()

    assert _execute(client, SCRIPT_CMD, tmp_path)["decision"] == "prompt_required"
    assert calls == []


# --- H. stored hash -----------------------------------------------------------


def test_prompt_row_stores_script_sha256(
    client: TestClient, db: Persistence, script: Path, tmp_path: Path
) -> None:
    """CONTRACT: the pending_prompts column name `script_sha256` is fixed by this test."""
    prompt = _execute(client, SCRIPT_CMD, tmp_path)["prompt"]
    row = db.get_pending_prompt(prompt["id"])
    assert row is not None
    assert row["script_sha256"] == hashlib.sha256(script.read_bytes()).hexdigest()


# === Review follow-up (issue #95): fail closed, wrappers, -c, FIFOs, runners ==========


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(tmp_path: Path, name: str, text: str = "echo hi\n") -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _pin(command: str, cwd: Path) -> script_pin.ScriptPin:
    pin = script_pin.compute_pin(command, str(cwd))
    assert pin is not None, command
    return pin


def _approve_raw(client: TestClient, prompt_id: str, decision: str) -> Any:
    return client.post(
        "/approve_pending",
        json={"prompt_id": prompt_id, "decision": decision, "approver_agent_id": "primary"},
    )


def _rewrite_reprompts(
    client: TestClient,
    calls: list[Any],
    tmp_path: Path,
    command: str,
    target: Path,
) -> None:
    """approve_once, rewrite `target`, retry: the approval is burned and a new prompt issued."""
    first = _execute(client, command, tmp_path)
    assert first["decision"] == "prompt_required", first
    _approve(client, first["prompt"]["id"])
    target.write_text(target.read_text() + "\n# tampered\n")
    again = _execute(client, command, tmp_path)
    assert again["decision"] == "prompt_required", again
    assert again["prompt"]["id"] != first["prompt"]["id"]
    assert "changed" in again["prompt"]["why"]
    assert calls == []


# --- A. fail closed for unpinnable script commands ------------------------------


@pytest.mark.parametrize(
    "command",
    [
        'bash "$S"',
        "bash $S",
        "bash ./*.sh",
        "bash ~root/x.sh",
        "cd $D && bash x.sh",
        "ls | python3",
        "bash -s",
        'eval "$X"',
        "echo a | xargs bash",
        "xargs python3 x.py",
        ". x.sh",
        "source x.sh",
        "bash <(ls)",
    ],
)
def test_unpinnable_shapes_fail_closed(tmp_path: Path, command: str) -> None:
    cls = classify(command, str(tmp_path), AGENT)
    assert cls.script_unpinnable is True, command
    assert cls.non_promotable is True


@pytest.mark.parametrize(
    "command",
    [
        "bash x.sh",
        "bash < x.sh",
        "bash -s -- a b < x.sh",
        "cat x.sh | python3",
        "echo hi | python3",
        'printf "a\\n" | sh',
        ". ./x.sh",
        "cd /tmp && bash x.sh",
        "python3 -m json.tool",
    ],
)
def test_pinnable_shapes_not_unpinnable(tmp_path: Path, command: str) -> None:
    _write(tmp_path, "x.sh")
    assert classify(command, str(tmp_path), AGENT).script_unpinnable is False


def test_unpinnable_prompt_stores_sentinel(
    client: TestClient, db: Persistence, tmp_path: Path
) -> None:
    prompt = _execute(client, 'bash "$S"', tmp_path)["prompt"]
    row = db.get_pending_prompt(prompt["id"])
    assert row is not None
    assert row["script_sha256"] == script_pin.UNPINNABLE


@pytest.mark.parametrize("decision", ["approve_once", "approve_template", "approve_verb"])
def test_unpinnable_prompt_refuses_every_approval(
    client: TestClient, db: Persistence, calls: list[Any], tmp_path: Path, decision: str
) -> None:
    prompt = _execute(client, 'bash "$S"', tmp_path)["prompt"]
    resp = _approve_raw(client, prompt["id"], decision)
    assert resp.status_code >= 400
    assert "literal script path" in resp.text
    row = db.get_pending_prompt(prompt["id"])
    assert row is not None
    assert row["approved_at"] is None
    assert _count(db, "template_approvals") == 0
    assert _count(db, "verb_approvals") == 0
    assert _execute(client, 'bash "$S"', tmp_path)["decision"] == "prompt_required"
    assert calls == []
    # deny still works
    assert _approve_raw(client, prompt["id"], "deny").status_code == 200


def test_unpinnable_raced_approval_never_executes(
    client: TestClient, db: Persistence, calls: list[Any], tmp_path: Path
) -> None:
    prompt = _execute(client, 'bash "$S"', tmp_path)["prompt"]
    db.decide_prompt(
        prompt_id=prompt["id"], decision="approve_once", approver_agent_id="primary", rule=None
    )
    again = _execute(client, 'bash "$S"', tmp_path)
    assert again["decision"] == "prompt_required"
    assert "literal script path" in again["prompt"]["why"]
    assert calls == []


# --- A. stdin sources -----------------------------------------------------------


def test_stdin_redirect_and_cat_pin_the_file(tmp_path: Path) -> None:
    script = _write(tmp_path, "x.sh")
    for command in ("bash < x.sh", "bash -s -- a < x.sh", "cat x.sh | python3", "cat < x.sh | sh"):
        assert _pin(command, tmp_path).sha256 == _sha(script), command


def test_cat_pipe_p2_rewrite_reprompts(
    client: TestClient, calls: list[Any], tmp_path: Path
) -> None:
    script = _write(tmp_path, "x.sh")
    _rewrite_reprompts(client, calls, tmp_path, "cat x.sh | python3", script)


def test_literal_echo_into_interpreter_needs_no_pin(tmp_path: Path) -> None:
    assert script_pin.compute_pin("echo hi | python3", str(tmp_path)) is None


# --- B. wrappers and compound forms ---------------------------------------------

WRAPPED = [
    "env FOO=1 bash x.sh",
    "env -u FOO bash x.sh",
    "env -i bash x.sh",
    "/usr/bin/env bash x.sh",
    "command bash x.sh",
    "exec bash x.sh",
    "nice -n 5 bash x.sh",
    "nohup bash x.sh",
    "timeout 5 bash x.sh",
    "timeout -s KILL 5 bash x.sh",
    "setsid bash x.sh",
    "stdbuf -oL bash x.sh",
    "sudo -u root bash x.sh",
    "ionice -c 3 bash x.sh",
    "chrt 10 bash x.sh",
    "taskset -c 0 bash x.sh",
    "taskset 0x1 bash x.sh",
    "flock /tmp/l.lock bash x.sh",
    "flock -w 5 /tmp/l.lock bash x.sh",
    "{ bash x.sh; }",
    "(bash x.sh)",
    "if true; then bash x.sh; fi",
    "while false; do bash x.sh; done",
    "for i in 1 2; do bash x.sh; done",
    "! bash x.sh",
    "time bash x.sh",
    "time -p bash x.sh",
    "true && bash x.sh",
    "f() { bash x.sh; }",
    "find . -name y -exec bash x.sh \\;",
]


@pytest.mark.parametrize("command", WRAPPED)
def test_wrapped_script_is_detected_and_pinned(tmp_path: Path, command: str) -> None:
    script = _write(tmp_path, "x.sh")
    assert classify(command, str(tmp_path), AGENT).non_promotable is True
    assert _pin(command, tmp_path).sha256 == _sha(script)


@pytest.mark.parametrize(
    "command", ["nice --bogus bash x.sh", 'env -S "bash x.sh"', "xargs bash x.sh"]
)
def test_unparseable_wrapper_chain_fails_closed(tmp_path: Path, command: str) -> None:
    _write(tmp_path, "x.sh")
    assert classify(command, str(tmp_path), AGENT).script_unpinnable is True


def test_wrapper_p2_rewrite_reprompts(client: TestClient, calls: list[Any], tmp_path: Path) -> None:
    script = _write(tmp_path, "x.sh")
    _rewrite_reprompts(client, calls, tmp_path, "timeout 5 bash ./x.sh", script)


# --- C. nested -c strings -------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'bash x.sh'",
        "sh -c 'cd . && ./x.sh'",
        "bash -lc 'bash x.sh'",
        "env FOO=1 bash -c 'bash x.sh'",
        "timeout 5 bash -c 'source ./x.sh'",
        "bash -c \"bash -c 'bash x.sh'\"",
    ],
)
def test_nested_c_body_scripts_are_pinned(tmp_path: Path, command: str) -> None:
    script = _write(tmp_path, "x.sh")
    assert _pin(command, tmp_path).sha256 == _sha(script)


def test_nested_c_unparseable_or_too_deep_fails_closed(tmp_path: Path) -> None:
    assert classify('bash -c "bash \'x.sh"', str(tmp_path), AGENT).script_unpinnable
    command = "bash x.sh"
    for _ in range(5):
        command = "bash -c " + shlex.quote(command)
    assert classify(command, str(tmp_path), AGENT).script_unpinnable


def test_nested_c_p2_rewrite_reprompts(
    client: TestClient, calls: list[Any], tmp_path: Path
) -> None:
    script = _write(tmp_path, "x.sh")
    _rewrite_reprompts(client, calls, tmp_path, "bash -c 'bash ./x.sh'", script)


# --- D. FIFOs, devices, huge files ----------------------------------------------


def test_fifo_is_unreadable_and_does_not_hang(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "f.sh")
    pin = _pin("bash f.sh", tmp_path)
    assert list(pin.paths.values()) == [script_pin.UNREADABLE]
    assert not script_pin.pin_matches("bash f.sh", str(tmp_path), pin.sha256)


def test_device_and_directory_are_unreadable(tmp_path: Path) -> None:
    (tmp_path / "d.sh").mkdir()
    assert list(_pin("bash d.sh", tmp_path).paths.values()) == [script_pin.UNREADABLE]
    assert list(_pin("bash /dev/null", tmp_path).paths.values()) == [script_pin.UNREADABLE]


def test_oversized_file_is_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, "big.sh", "echo " + "a" * 100)
    monkeypatch.setattr(script_pin, "MAX_SCRIPT_BYTES", 16)
    assert list(_pin("bash big.sh", tmp_path).paths.values()) == [script_pin.UNREADABLE]


# --- E. more interpreters and runners -------------------------------------------


@pytest.mark.parametrize("command", ["ksh x.sh", "mksh x.sh", "fish x.sh", "php x.sh"])
def test_more_interpreters_pin_the_file(tmp_path: Path, command: str) -> None:
    script = _write(tmp_path, "x.sh")
    assert _pin(command, tmp_path).sha256 == _sha(script)


@pytest.mark.parametrize(
    ("command", "name"),
    [
        ("awk -f p.awk data.txt", "p.awk"),
        ("gawk -F: -f p.awk data.txt", "p.awk"),
        ("sed -f s.sed in.txt", "s.sed"),
        ("sed -n -f s.sed in.txt", "s.sed"),
    ],
)
def test_awk_and_sed_program_files_are_pinned(tmp_path: Path, command: str, name: str) -> None:
    program = _write(tmp_path, name)
    assert _pin(command, tmp_path).sha256 == _sha(program)


def test_make_pins_the_makefile_it_reads(tmp_path: Path) -> None:
    plain = _write(tmp_path, "Makefile", "all:\n")
    assert _pin("make deploy_x", tmp_path).sha256 == _sha(plain)
    assert classify("make deploy_x", str(tmp_path), AGENT).non_promotable is False
    other = _write(tmp_path, "other.mk", "all:\n\techo\n")
    assert _pin("make -f other.mk deploy_x", tmp_path).sha256 == _sha(other)
    sub = _write(tmp_path, "sub/Makefile", "sub:\n")
    assert _pin("make -C sub deploy_x", tmp_path).sha256 == _sha(sub)
    first = _write(tmp_path, "GNUmakefile", "all:\n")
    assert _pin("make deploy_x", tmp_path).sha256 == _sha(first)  # lookup order
    assert classify("make -f $F x", str(tmp_path), AGENT).script_unpinnable


def test_make_p2_rewrite_reprompts(client: TestClient, calls: list[Any], tmp_path: Path) -> None:
    makefile = _write(tmp_path, "Makefile", "deploy_x:\n\techo hi\n")
    _rewrite_reprompts(client, calls, tmp_path, "make deploy_x", makefile)


def test_pixi_run_pins_manifest_found_upward(tmp_path: Path) -> None:
    manifest = _write(tmp_path, "pixi.toml", "[tasks]\n")
    sub = tmp_path / "pkg" / "deep"
    sub.mkdir(parents=True)
    pin = _pin("pixi run build_x", sub)
    assert pin.sha256 == _sha(manifest)
    assert classify("pixi run build_x", str(sub), AGENT).non_promotable is False
    script = _write(tmp_path, "s.py")
    assert set(_pin("pixi run python s.py", tmp_path).paths) == {
        str(manifest.resolve()),
        str(script.resolve()),
    }
    assert _pin("pixi run --manifest-path pixi.toml build_x", tmp_path).sha256 == _sha(manifest)
    assert classify("pixi run --manifest-path $M x", str(tmp_path), AGENT).script_unpinnable


def test_pixi_run_p2_rewrite_reprompts(
    client: TestClient, calls: list[Any], tmp_path: Path
) -> None:
    manifest = _write(tmp_path, "pixi.toml", "[tasks]\n")
    _rewrite_reprompts(client, calls, tmp_path, "pixi run deploy_x", manifest)


@pytest.mark.parametrize(
    "command",
    [
        "make deploy_x",
        "pixi run test",
        "uv run deploy_x",
        "npm run build",
        "npm test",
        "yarn build",
        "pnpm run build",
    ],
)
def test_runners_stay_promotable(tmp_path: Path, command: str) -> None:
    for name in ("Makefile", "pixi.toml", "pyproject.toml", "package.json"):
        _write(tmp_path, name, "x\n")
    cls = classify(command, str(tmp_path), AGENT)
    assert cls.non_promotable is False
    assert cls.script_unpinnable is False
    assert _pin(command, tmp_path) is not None  # one-shot approvals still pin


@pytest.mark.parametrize(
    "command", ["uv run x.py", "pixi run python x.py", "pixi run bash x.sh", "pixi run ./x.sh"]
)
def test_runner_plus_script_is_non_promotable(tmp_path: Path, command: str) -> None:
    for name in ("pixi.toml", "pyproject.toml", "x.py", "x.sh"):
        _write(tmp_path, name, "x\n")
    assert classify(command, str(tmp_path), AGENT).non_promotable is True


def test_template_approval_for_pixi_run_is_honoured(
    client: TestClient, db: Persistence, calls: list[Any], tmp_path: Path
) -> None:
    _write(tmp_path, "pixi.toml", "[tasks]\n")
    prompt = _execute(client, "pixi run test", tmp_path)["prompt"]
    approved = _approve(client, prompt["id"], "approve_template")
    assert approved["template_promoted"] is True
    assert _count(db, "template_approvals") == 1
    assert _execute(client, "pixi run test", tmp_path)["decision"] == "executed"
    assert len(calls) == 1


def test_uv_npm_yarn_pin_their_manifests(tmp_path: Path) -> None:
    pyproject = _write(tmp_path, "pyproject.toml", "[project]\n")
    script = _write(tmp_path, "x.py")
    assert _pin("uv run deploy_x", tmp_path).sha256 == _sha(pyproject)
    assert set(_pin("uv run x.py", tmp_path).paths) == {
        str(pyproject.resolve()),
        str(script.resolve()),
    }
    package = _write(tmp_path, "pkg/package.json", "{}")
    assert _pin("npm run build", tmp_path / "pkg").sha256 == _sha(package)
    assert _pin("npm test", tmp_path / "pkg").sha256 == _sha(package)
    assert _pin("yarn build", tmp_path / "pkg").sha256 == _sha(package)
    assert _pin("npm --prefix pkg run build", tmp_path).sha256 == _sha(package)
    assert classify("npm --prefix $P run b", str(tmp_path), AGENT).script_unpinnable


def test_python_dash_m_pins_local_modules(tmp_path: Path) -> None:
    module = _write(tmp_path, "localmod.py")
    pin = _pin("python3 -m localmod", tmp_path)
    assert pin.paths[str(module.resolve())] == _sha(module)
    assert classify("python3 -m localmod", str(tmp_path), AGENT).non_promotable is True
    # an installed module is pinned as "absent locally" but does not block reuse
    assert classify("python3 -m json.tool", str(tmp_path), AGENT).non_promotable is False
    assert classify("python3 -m $M", str(tmp_path), AGENT).script_unpinnable


def test_find_exec_recurses_and_eval_is_unpinnable(tmp_path: Path) -> None:
    script = _write(tmp_path, "x.sh")
    assert _pin("find . -exec bash x.sh \\;", tmp_path).sha256 == _sha(script)
    assert classify("find . -exec bash {} \\;", str(tmp_path), AGENT).script_unpinnable
    assert classify("eval 'echo hi'", str(tmp_path), AGENT).script_unpinnable


# === Second review (issue #95): evasions, general fail-closed rules ==================


def _unpinnable(command: str, tmp_path: Path) -> bool:
    return classify(command, str(tmp_path), AGENT).script_unpinnable


# 1. unresolvable command head
@pytest.mark.parametrize(
    "command",
    [
        "$SHELL x.sh",
        "$(which bash) x.sh",
        "/usr/bin/ba* x.sh",
        "sh -c 'exec \"$@\"' _ bash x.sh",
    ],
)
def test_unresolvable_head_fails_closed(tmp_path: Path, command: str) -> None:
    assert _unpinnable(command, tmp_path)


# 2. substitution / heredoc feeding an interpreter
@pytest.mark.parametrize(
    "command",
    [
        'bash -c "$(cat x.sh)"',
        'bash <<< "$(cat x.sh)"',
        "python3 <<EOF\nprint($(cat x.sh))\nEOF",
        "bash <<EOF\n`cat x.sh`\nEOF",
    ],
)
def test_substitution_feeding_interpreter_fails_closed(tmp_path: Path, command: str) -> None:
    _write(tmp_path, "x.sh")
    assert _unpinnable(command, tmp_path)


# 3. attached option values
def test_attached_module_and_inline_operands(tmp_path: Path) -> None:
    evil = _write(tmp_path, "evil.py")
    _write(tmp_path, "x.py")
    assert _pin("python3 -mevil", tmp_path).paths[str(evil.resolve())] == _sha(evil)
    for command in ("python3 '-cprint(1)' x.py", "perl '-eprint 1' x.pl", "node --eval=1 x.js"):
        assert script_pin.compute_pin(command, str(tmp_path)) is None, command


# 4. local modules that do not exist yet; NULL pin that now yields refs
def test_missing_local_module_creation_reprompts(
    client: TestClient, calls: list[Any], tmp_path: Path
) -> None:
    first = _execute(client, "python3 -m newmod", tmp_path)
    assert first["decision"] == "prompt_required", first
    _approve(client, first["prompt"]["id"])
    _write(tmp_path, "newmod.py", "print(1)\n")
    again = _execute(client, "python3 -m newmod", tmp_path)
    assert again["decision"] == "prompt_required"
    assert calls == []


def test_manifest_appearing_after_unpinned_approval_reprompts(
    client: TestClient, calls: list[Any], tmp_path: Path
) -> None:
    first = _execute(client, "uv run deploy_x", tmp_path)
    assert first["decision"] == "prompt_required", first
    _approve(client, first["prompt"]["id"])
    _write(tmp_path, "pyproject.toml", "[project]\n")
    assert _execute(client, "uv run deploy_x", tmp_path)["decision"] == "prompt_required"
    assert calls == []


# 5. directory-changing wrappers
@pytest.mark.parametrize(
    "command",
    [
        "env -C /tmp bash x.sh",
        "env --chdir=/tmp bash x.sh",
        "sudo -D /tmp bash x.sh",
        "sudo --chdir=/tmp bash x.sh",
        "find . -execdir bash x.sh \\;",
    ],
)
def test_directory_changing_wrappers_fail_closed(tmp_path: Path, command: str) -> None:
    assert _unpinnable(command, tmp_path)


# 6. package-manager exec forms
@pytest.mark.parametrize(
    "command",
    [
        "pnpm exec bash x.sh",
        "pnpm dlx bash x.sh",
        "npm exec -- bash x.sh",
        "npx bash x.sh",
        "bunx bash x.sh",
        "yarn exec bash x.sh",
        "yarn node x.sh",
        "yarn run bash x.sh",
    ],
)
def test_package_manager_exec_forms_are_pinned(tmp_path: Path, command: str) -> None:
    script = _write(tmp_path, "x.sh")
    assert _pin(command, tmp_path).sha256 == _sha(script)
    assert classify(command, str(tmp_path), AGENT).non_promotable is True


# 7. unlisted wrappers
@pytest.mark.parametrize(
    "command",
    [
        "strace -o out bash x.sh",
        "ltrace bash x.sh",
        "watch bash x.sh",
        "parallel bash ::: a",
        "fakeroot bash x.sh",
        "unshare -r bash x.sh",
        "tmux new-session python x.py",
        "runuser -u bob -- bash x.sh",
        "busybox sh x.sh",
        "su -c 'bash x.sh'",
        "script -c 'bash x.sh' out",
        "tmux new 'bash x.sh'",
        "ssh host bash x.sh",
        "trap 'bash x.sh' EXIT",
        'alias go="bash x.sh"',
        "alias go=bash",
    ],
)
def test_unlisted_wrappers_fail_closed(tmp_path: Path, command: str) -> None:
    assert _unpinnable(command, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "grep python notes.txt",
        'git commit -m "mention python"',
        "apt install bash",
        "mytool --flag",
    ],
)
def test_mentioning_an_interpreter_as_data_is_not_unpinnable(tmp_path: Path, command: str) -> None:
    assert not _unpinnable(command, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "pixi run ruff format --check .",
        "pixi run ruff check . 2>&1 | tail -n 8",
        "pixi run -e dev pytest .",
        "conda install python",
        "mytool python",
        "mytool --flag sh",
    ],
)
def test_narrowed_unknown_head_rule_does_not_block(tmp_path: Path, command: str) -> None:
    assert not _unpinnable(command, tmp_path)


@pytest.mark.parametrize(
    "command", ["mytool -c 'bash x.sh'", "mytool --command='bash x.sh'", "pixi -C dir run x"]
)
def test_command_string_flag_and_pixi_global_fail_closed(tmp_path: Path, command: str) -> None:
    assert _unpinnable(command, tmp_path)


def test_pixi_unknown_global_option_fails_closed(tmp_path: Path) -> None:
    assert _unpinnable("pixi --bogus run x", tmp_path)
    assert not _unpinnable("pixi --manifest-path pixi.toml -e dev run x", tmp_path)


@pytest.mark.parametrize("command", ["pixi run --bogus task", "pixi run --bogus=x task"])
def test_pixi_run_unknown_option_fails_closed(tmp_path: Path, command: str) -> None:
    assert _unpinnable(command, tmp_path)


def test_pixi_run_known_options_stay_pinnable(tmp_path: Path) -> None:
    assert not _unpinnable("pixi run -e dev --frozen pytest tests/", tmp_path)
    assert not _unpinnable("pixi run --locked --no-install -- pytest tests/", tmp_path)


# systemd-run is unwrapped
def test_systemd_run_pins_the_inner_script_in_working_directory(tmp_path: Path) -> None:
    work = tmp_path / "w"
    script = _write(work, "build.sh")
    command = f"systemd-run --user --collect --unit=b --working-directory={work} bash build.sh"
    assert (
        _pin(command, tmp_path).paths.keys()
        == _pin(f"cd {work} && bash build.sh", tmp_path).paths.keys()
    )
    assert str(script) in _pin(command, tmp_path).paths
    before = _pin(command, tmp_path).sha256
    script.write_text("echo changed\n")
    assert _pin(command, tmp_path).sha256 != before


def test_systemd_run_relative_working_directory_and_same_dir(tmp_path: Path) -> None:
    _write(tmp_path / "sub", "build.sh")
    cmd = "systemd-run --working-directory sub bash build.sh"
    assert _pin(cmd, tmp_path).sha256 == _pin("cd sub && bash build.sh", tmp_path).sha256
    here = _write(tmp_path, "x.sh")
    assert _pin("systemd-run --same-dir -- bash x.sh", tmp_path).sha256 == _sha(here)
    assert _pin("systemd-run -p MemoryMax=1G -d bash x.sh", tmp_path).sha256 == _sha(here)
    assert _pin("systemd-run -d bash x.sh", tmp_path).sha256 == _sha(here)
    assert _pin("systemd-run --scope --user bash x.sh", tmp_path).sha256 == _sha(here)


def test_systemd_run_absolute_script_is_pinned_without_a_directory(tmp_path: Path) -> None:
    script = _write(tmp_path, "x.sh")
    pin = _pin(f"systemd-run --user bash {script}", tmp_path)
    assert pin.sha256 == _sha(script)
    assert not pin.unpinnable


@pytest.mark.parametrize(
    "command",
    [
        "systemd-run --user bash x.sh",
        "systemd-run --user ./x.sh",
        "systemd-run bash ./x.sh",
        "systemd-run -p MemoryMax=1G bash x.sh",
    ],
)
def test_systemd_run_service_directory_is_unknown(tmp_path: Path, command: str) -> None:
    """A transient service runs in the manager's default directory, not the caller's cwd."""
    _write(tmp_path, "x.sh")
    assert _unpinnable(command, tmp_path)


def test_systemd_run_working_directory_with_same_dir_fails_closed(tmp_path: Path) -> None:
    _write(tmp_path, "x.sh")
    assert _unpinnable(f"systemd-run --working-directory={tmp_path} --same-dir bash x.sh", tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "systemd-run -H host bash x.sh",
        "systemd-run -M mach bash x.sh",
        "systemd-run --on-active=5 bash x.sh",
        "systemd-run -p ExecStartPre=/x bash x.sh",
        "systemd-run --unknown-opt bash x.sh",
        "systemd-run --user",
        "systemd-run --shell",
        "systemd-run -S bash x.sh",
    ],
)
def test_systemd_run_fail_closed_cases(tmp_path: Path, command: str) -> None:
    assert _unpinnable(command, tmp_path)


# nits
def test_embedded_nul_does_not_crash(tmp_path: Path) -> None:
    pin = script_pin.compute_pin("bash x\x00y.sh", str(tmp_path))
    assert pin is None or pin.unpinnable or not pin.ok


@pytest.mark.parametrize("kind", ["dir", "fifo"])
def test_non_regular_script_refuses_approval(
    client: TestClient, db: Persistence, tmp_path: Path, kind: str
) -> None:
    if kind == "dir":
        (tmp_path / "x.sh").mkdir()
    else:
        os.mkfifo(tmp_path / "x.sh")
    prompt = _execute(client, "bash x.sh", tmp_path)["prompt"]
    resp = _approve_raw(client, prompt["id"], "approve_once")
    assert resp.status_code >= 400
    assert "literal script path" in resp.text


def test_hashing_budget_makes_the_command_unpinnable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("a.sh", "b.sh", "c.sh"):
        _write(tmp_path, name, "echo 12345678\n")
    command = "cat a.sh b.sh c.sh | python3"
    assert not _pin(command, tmp_path).unpinnable
    monkeypatch.setattr(script_pin, "MAX_FILES", 2)
    assert _pin(command, tmp_path).unpinnable
    monkeypatch.setattr(script_pin, "MAX_FILES", 32)
    monkeypatch.setattr(script_pin, "MAX_TOTAL_BYTES", 20)
    assert _pin(command, tmp_path).unpinnable
