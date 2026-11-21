"""Issues #117 / #122: verb rules that came from sentinel-template prompts never promote.

Before the #117 guard, approving a prompt whose normalized template held a leaked
``__TKSENTINEL_<n>`` could create a ``verb_approvals`` row. Such a row must stay inert
whatever the normalizer does later (the stored prompt template keeps the sentinel), and
the schema setup removes it.

Known limit, tested below: a row with no originating prompt (NULL approved_via_prompt_id,
a seeded row, or a prompt that was purged) cannot be attributed, so it is left as is.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from shell_runner.catalog import Tier
from shell_runner.classifier import classify
from shell_runner.persistence import Persistence
from shell_runner.server import _apply_template_approval
from shell_runner.shellprep import SENTINEL_PREFIX, template_has_sentinel

AGENT = "focused-code-modifier"
SENTINEL_TEMPLATE = f"kill -9 pre{SENTINEL_PREFIX}0suffix"
PLAIN_TEMPLATE = "kill -9 <int>"


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Persistence:
    fresh = Persistence(db_path=tmp_path / "test.sqlite3")
    monkeypatch.setattr("shell_runner.server.db", fresh)
    return fresh


@pytest.fixture
def db(_fresh_db: Persistence) -> Persistence:
    return _fresh_db


def _prompt(db: Persistence, template: str, cwd: Path) -> str:
    return db.create_pending_prompt(
        agent_id=AGENT,
        cwd=str(cwd),
        raw_cmd="kill -9 12345",
        normalized_template=template,
        command_tier=int(Tier.ALWAYS_APPROVE),
        matched_rule_category=None,
        decision_path=[],
    )


def _verb_row(db: Persistence, cwd: Path, prompt_id: str | None, verb: str = "kill") -> None:
    with sqlite3.connect(db.db_path) as conn:
        conn.execute(
            "INSERT INTO verb_approvals "
            "(verb, cwd_prefix, agent_id, approved_tier, approved_at, approved_via_prompt_id) "
            "VALUES (?, ?, ?, 2, CURRENT_TIMESTAMP, ?)",
            (verb, str(cwd.resolve()), AGENT, prompt_id),
        )


def _verbs(db: Persistence) -> list[str]:
    with sqlite3.connect(db.db_path) as conn:
        return [r[0] for r in conn.execute("SELECT verb FROM verb_approvals ORDER BY id")]


def _match(db: Persistence, cwd: Path) -> int | None:
    return db.match_verb_approval("kill", str(cwd.resolve()), AGENT).tier


def test_row_from_sentinel_prompt_does_not_promote_plain_command(
    db: Persistence, tmp_path: Path
) -> None:
    _verb_row(db, tmp_path, _prompt(db, SENTINEL_TEMPLATE, tmp_path))
    assert _match(db, tmp_path) is None

    cls = classify("kill -9 12345", str(tmp_path), AGENT)
    assert not template_has_sentinel(cls.template)
    result = _apply_template_approval(cls, AGENT, str(tmp_path))
    assert not any(p.startswith("approved_") for p in result.decision_path)
    assert result.tier == cls.tier


def test_post_normalizer_fix_state_does_not_promote(
    db: Persistence, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #117 server guard no longer sees a sentinel in the current template (as after
    step 2 restores embedded placeholders) and the verb lookup is reached; the old row's
    prompt template still has one. The exclusion must not depend on the current template."""
    _verb_row(db, tmp_path, _prompt(db, SENTINEL_TEMPLATE, tmp_path))
    cls = classify("kill -9 x$FOO", str(tmp_path), AGENT)
    assert template_has_sentinel(cls.template)
    assert not cls.path_guarded
    monkeypatch.setattr("shell_runner.server.template_has_sentinel", lambda _t: False)
    result = _apply_template_approval(cls, AGENT, str(tmp_path))
    assert not any(p.startswith("approved_") for p in result.decision_path)
    assert result.tier == cls.tier

    # Control: the same setup with a row from a plain prompt does promote.
    other = tmp_path / "ctl"
    other.mkdir()
    _verb_row(db, other, _prompt(db, PLAIN_TEMPLATE, other))
    ctl = _apply_template_approval(classify("kill -9 x$FOO", str(other), AGENT), AGENT, str(other))
    assert any(p.startswith("approved_verb") for p in ctl.decision_path)


def test_startup_cleanup_removes_only_sentinel_rows_and_is_idempotent(
    db: Persistence, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sub_a, sub_b = tmp_path / "a", tmp_path / "b"
    sub_a.mkdir()
    sub_b.mkdir()
    _verb_row(db, sub_a, _prompt(db, SENTINEL_TEMPLATE, sub_a), verb="kill")
    _verb_row(db, sub_b, _prompt(db, PLAIN_TEMPLATE, sub_b), verb="pkill")
    _verb_row(db, sub_b, None, verb="skill")
    assert _verbs(db) == ["kill", "pkill", "skill"]

    with caplog.at_level("WARNING", logger="shell_runner.persistence"):
        Persistence(db.db_path)
    assert _verbs(db) == ["pkill", "skill"]
    assert "removed 1 verb approval(s)" in caplog.text
    assert "sentinel" in caplog.text

    caplog.clear()
    with caplog.at_level("WARNING", logger="shell_runner.persistence"):
        Persistence(db.db_path)
    assert _verbs(db) == ["pkill", "skill"]
    assert "sentinel" not in caplog.text


def test_normal_row_still_promotes(db: Persistence, tmp_path: Path) -> None:
    _verb_row(db, tmp_path, _prompt(db, PLAIN_TEMPLATE, tmp_path))
    assert _match(db, tmp_path) == 2
    Persistence(db.db_path)  # startup cleanup keeps it
    assert _match(db, tmp_path) == 2
    cls = classify("kill -9 12345", str(tmp_path), AGENT)
    promoted = _apply_template_approval(cls, AGENT, str(tmp_path))
    assert any(p.startswith("approved_verb_T2") for p in promoted.decision_path)


def test_unattributable_rows_are_left_and_still_promote(db: Persistence, tmp_path: Path) -> None:
    """NULL approved_via_prompt_id, or a prompt id with no prompt row: no evidence about
    the origin, so the row is neither excluded nor cleaned up (documented limit)."""
    _verb_row(db, tmp_path, None)
    assert _match(db, tmp_path) == 2
    Persistence(db.db_path)
    assert _verbs(db) == ["kill"]
    assert _match(db, tmp_path) == 2

    other = tmp_path / "gone"
    other.mkdir()
    _verb_row(db, other, "no-such-prompt")
    Persistence(db.db_path)
    assert _verbs(db) == ["kill", "kill"]
    assert _match(db, other) == 2


def test_cleanup_leaves_template_approvals_alone(db: Persistence, tmp_path: Path) -> None:
    """Template rows are #117 step 3, not this change."""
    with sqlite3.connect(db.db_path) as conn:
        conn.execute(
            "INSERT INTO template_approvals (template, agent_id, approved_tier, approved_at) "
            "VALUES (?, NULL, 2, CURRENT_TIMESTAMP)",
            (SENTINEL_TEMPLATE,),
        )
    Persistence(db.db_path)
    with sqlite3.connect(db.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM template_approvals").fetchone()[0] == 1
