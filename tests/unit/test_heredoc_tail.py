"""Heredoc opener-line tail (#85 follow-up).

Only the heredoc BODY is stripped; the rest of the opener line (`&& cmd`, `; cmd`, `| cmd`,
redirects) stays normal shell text, so the catalog classifies every segment and the
template carries the tail (`cat <heredoc> && python3 -c <arg>`).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shell_runner import executor, path_guard
from shell_runner.catalog import Tier
from shell_runner.classifier import PERMISSIVENESS, _classify_catalog, classify
from shell_runner.normalizer import normalize
from shell_runner.persistence import Persistence
from shell_runner.server import app
from shell_runner.write_targets import analyze

AGENT = "focused-code-modifier"
T3, T4 = Tier.APPROVE_ONCE, Tier.ALWAYS_APPROVE


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Persistence:
    fresh = Persistence(db_path=tmp_path / "test.sqlite3")
    monkeypatch.setattr("shell_runner.server.db", fresh)
    monkeypatch.setattr(executor, "_CWD_ROOTS", [tmp_path.resolve()])
    monkeypatch.setenv("SHELL_RUNNER_CONFIG_DIR", str(tmp_path / "no-rules"))
    return fresh


def _cls(command: str, cwd: Path):  # noqa: ANN202
    return classify(command, str(cwd), AGENT, env={"HOME": "/nonexistent-home"})


def _tier(command: str, cwd: Path) -> Tier:
    return _classify_catalog(command, str(cwd), AGENT).command_tier


def test_template_carries_the_tail() -> None:
    assert normalize("cat <<EOF\nx\nEOF", "/tmp").template == "cat <heredoc>"
    got = normalize("cat <<EOF && python3 -c 'x'\nbody\nEOF", "/tmp")
    assert got.template == "cat <heredoc> && python3 -c <arg>"
    assert len(got.segments) == 2
    assert normalize("cat <<EOF; rm -rf x\nb\nEOF", "/tmp").template.startswith("cat <heredoc> ;")
    assert normalize("cat <<EOF | sh\nb\nEOF", "/tmp").template == "cat <heredoc> | sh"
    assert normalize("cat <<'EOF' > notes.txt\nb\nEOF", "/tmp").template.startswith(
        "cat <heredoc> >"
    )


def test_inline_code_after_a_heredoc_is_t4(tmp_path: Path) -> None:
    command = "cat <<EOF && python3 -c 'print(1)'\nbody\nEOF"
    assert _tier(command, tmp_path) == T4
    assert _cls(command, tmp_path).command_tier == T4


def test_command_after_semicolon_gets_its_own_tier(tmp_path: Path) -> None:
    command = "cat <<EOF; rm -rf x\nbody\nEOF"
    rm = _tier("rm -rf x", tmp_path)
    got = _tier(command, tmp_path)
    assert PERMISSIVENESS[got] <= PERMISSIVENESS[rm]


def test_pipe_to_a_shell_takes_the_shell_tier(tmp_path: Path) -> None:
    command = "cat <<EOF | sh\nbody\nEOF"
    got = _tier(command, tmp_path)
    assert PERMISSIVENESS[got] <= PERMISSIVENESS[_tier("sh", tmp_path)]
    assert PERMISSIVENESS[got] <= PERMISSIVENESS[T3]


def test_plain_quoted_heredoc_redirect_keeps_its_tier(tmp_path: Path) -> None:
    for command in (
        "cat <<'EOF' > notes.txt\nhello\nEOF",
        "cat <<EOF > notes.txt\nhello\nEOF",
        "cat <<EOF\nhello\nEOF",
    ):
        cls = _cls(command, tmp_path)
        assert cls.command_tier == T3, command
        assert not cls.path_guarded, command


def test_two_heredocs_on_one_line_strip_each_body_in_order(tmp_path: Path) -> None:
    command = "cat <<A <<B\nit's a\nA\nb body\nB\nawk 'BEGIN{system(\"id\")}'"
    assert [cp.verb for cp in analyze(command)] == ["cat", "awk"]
    got = normalize(command, str(tmp_path))
    assert [s.template for s in got.segments] == ["cat <heredoc> <heredoc>", "awk <arg>"]
    assert _cls(command, tmp_path).command_tier == T4


def test_tail_after_two_heredocs_is_classified(tmp_path: Path) -> None:
    command = "cat <<A <<B && python3 -c 'x'\nA body\nA\nB body\nB"
    assert _tier(command, tmp_path) == T4


def test_missing_terminator_is_guarded_unparseable(tmp_path: Path) -> None:
    command = "cat <<EOF && python3 -c 'x'\nno terminator here"
    (cp,) = analyze(command)
    assert cp.unparseable
    cls = _cls(command, tmp_path)
    assert cls.path_guarded
    assert PERMISSIVENESS[cls.command_tier] <= PERMISSIVENESS[T3]
    finding = path_guard.evaluate(command, str(tmp_path), {})
    assert finding is not None
    assert finding.category == "unparseable"
    assert finding.guarded


def test_unterminated_second_heredoc_is_unparseable(tmp_path: Path) -> None:
    (cp,) = analyze("cat <<A <<B\na\nA\nb never ends")
    assert cp.unparseable


def test_here_string_tail_is_classified(tmp_path: Path) -> None:
    command = "cat <<< hello && python3 -c 'x'"
    assert _tier(command, tmp_path) == T4


def test_approving_plain_heredoc_template_does_not_unlock_a_tail(tmp_path: Path) -> None:
    client = TestClient(app)
    plain = "cat <<EOF\nx\nEOF"
    first = client.post(
        "/execute", json={"command": plain, "cwd": str(tmp_path), "agent_id": AGENT}
    ).json()
    assert first["decision"] == "prompt_required"
    resp = client.post(
        "/approve_pending",
        json={
            "prompt_id": first["prompt"]["id"],
            "decision": "approve_template",
            "approver_agent_id": "primary",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["template_promoted"] is True

    def preview(command: str) -> str:
        body = {"command": command, "cwd": str(tmp_path), "agent_id": AGENT}
        return client.post("/classify", json=body).json()["decision_preview"]

    assert preview(plain) == "would_execute"
    for tail in (
        "cat <<EOF && python3 -c x\nx\nEOF",
        "cat <<EOF; rm -rf x\nx\nEOF",
        "cat <<EOF | sh\nx\nEOF",
        "cat <<EOF > notes.txt\nx\nEOF",
    ):
        # never would_execute: a prompt, or a deny when the tail is itself denied (`| sh`)
        assert preview(tail) in ("would_prompt", "would_deny"), tail
