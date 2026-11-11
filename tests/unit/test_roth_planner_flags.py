"""Protected-path flag coverage for a roth_planner-style project rules file.

Mirrors a live user rules file (projects/roth_planner.toml) using a temp config dir and
a temp project root; nothing outside tmp_path is read or written.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from shell_runner import rules
from shell_runner.catalog import Tier
from shell_runner.classifier import PERMISSIVENESS, _classify_catalog, classify

AGENT = "trial-agent"

DENY_PATTERN = (
    r"pixi\s+run\b.*(?<![\w-])acceptance-record(?![\w-])"
    r"|(?<![\w-])acceptance_checksum(?![\w-]).*(?<![\w-])record(?![\w-])"
)

PROJECT_TOML = """
roots = ["{root}"]

[[protected]]
glob = "tests/acceptance/**"
mode = "flag"
reason = "acceptance specs are frozen"

[[protected]]
glob = "tests/acceptance.sha256"
mode = "flag"
reason = "acceptance hash is frozen"

[[deny]]
pattern = '{pattern}'
reason = "do not re-record acceptance"
"""

SHAPES = {
    "redirect_sha256sum": (
        "sha256sum tests/acceptance/x.feature > tests/acceptance.sha256",
        True,
    ),
    "redirect_append": ("echo x >> tests/acceptance/foo.feature", True),
    "tee": ("cat a | tee tests/acceptance.sha256", True),
    "cp": ("cp /tmp/x tests/acceptance/foo.feature", False),
    "mv": ("mv /tmp/x tests/acceptance/foo.feature", False),
    "sed_i": ("sed -i s/a/b/ tests/acceptance/foo.feature", False),
}


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "cfg"
    (cfg / "projects").mkdir(parents=True)
    monkeypatch.setenv(rules.CONFIG_DIR_ENV, str(cfg))
    proj = tmp_path / "roth_planner"
    (proj / "tests" / "acceptance").mkdir(parents=True)
    (cfg / "projects" / "roth_planner.toml").write_text(
        PROJECT_TOML.format(root=proj, pattern=DENY_PATTERN)
    )
    return proj


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_write_into_acceptance_is_flagged_not_denied(root: Path, name: str) -> None:
    command, labelled = SHAPES[name]
    verdict = rules.evaluate(command, str(root))
    assert verdict.flagged
    assert verdict.denying_match is None

    result = classify(command, str(root), AGENT)
    assert result.flagged
    assert result.protected_path_json is not None
    assert all(m["mode"] == "flag" for m in json.loads(result.protected_path_json))
    assert any(m["kind"] == "write" for m in json.loads(result.protected_path_json))
    assert result.tier != Tier.DENY

    # never lowered by the mkdir/labelling code
    base = _classify_catalog(command, str(root), AGENT)
    assert PERMISSIVENESS[result.command_tier] <= PERMISSIVENESS[base.command_tier]
    assert bool([p for p in result.decision_path if p.startswith("fs-write")]) == labelled


@pytest.mark.parametrize(
    "command", ["pixi run acceptance-record", "pixi run -e dev acceptance-record"]
)
def test_deny_pattern_still_denies(root: Path, command: str) -> None:
    assert re.search(DENY_PATTERN, command)
    assert classify(command, str(root), AGENT).tier == Tier.DENY


def test_write_outside_protected_paths_is_not_flagged(root: Path) -> None:
    for command in (
        "echo x > tests/other/foo",
        "cp /tmp/x tests/other/foo",
        "touch tests/other/foo",
    ):
        result = classify(command, str(root), AGENT)
        assert not result.flagged, command
        assert result.protected_path_json is None
