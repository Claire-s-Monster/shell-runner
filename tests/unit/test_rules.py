"""Tests for user-configurable deny / protected-path rules (rules.py)."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from shell_runner import rules
from shell_runner.catalog import Tier
from shell_runner.classifier import classify, denial_reason, lookup_agent_cap
from shell_runner.persistence import Persistence

AGENT = "trial-agent"

PROJECT_TOML = """
roots = ["{root}"]

[[protected]]
glob = "tests/acceptance/**"
reason = "acceptance specs are frozen"

[[protected]]
glob = "tests/acceptance.sha256"
reason = "acceptance hash is frozen"

[[deny]]
pattern = 'pixi\\s+run\\s+acceptance-record'
reason = "do not re-record acceptance"
"""


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "cfg"
    d.mkdir()
    monkeypatch.setenv(rules.CONFIG_DIR_ENV, str(d))
    return d


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    (p / "tests").mkdir(parents=True)
    return p


def write_global(cfg: Path, text: str) -> Path:
    path = cfg / "rules.toml"
    path.write_text(text)
    return path


def write_project(cfg: Path, name: str, text: str) -> Path:
    (cfg / "projects").mkdir(exist_ok=True)
    path = cfg / "projects" / f"{name}.toml"
    path.write_text(text)
    return path


@pytest.fixture
def guarded(cfg: Path, proj: Path) -> Path:
    write_project(cfg, "p", PROJECT_TOML.format(root=proj))
    return proj


def kinds(command: str, cwd: Path) -> set[str]:
    return {m.kind for m in rules.evaluate(command, str(cwd)).matches}


# ---------------------------------------------------------------------------
# glob translation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("glob", "path", "expected"),
    [
        ("/r/tests/acceptance/**", "/r/tests/acceptance/a.py", True),
        ("/r/tests/acceptance/**", "/r/tests/acceptance/deep/er/a.py", True),
        ("/r/tests/acceptance/**", "/r/tests/acceptance", True),
        ("/r/tests/acceptance/**", "/r/tests/acceptance2/a.py", False),
        ("/r/tests/*.py", "/r/tests/a.py", True),
        ("/r/tests/*.py", "/r/tests/sub/a.py", False),
        ("/r/**/spec.py", "/r/spec.py", True),
        ("/r/**/spec.py", "/r/a/b/spec.py", True),
        ("/r/**/spec.py", "/r/a/b/other.py", False),
        ("/r/tests/acceptance.sha256", "/r/tests/acceptance.sha256", True),
        ("/r/tests/acceptance.sha256", "/r/tests/acceptanceXsha256", False),
        ("/r/t?.py", "/r/ta.py", True),
        ("/r/t?.py", "/r/t/.py", False),
        ("/r/[ab].py", "/r/a.py", True),
        ("/r/[!ab].py", "/r/a.py", False),
        ("/r/[!ab].py", "/r/c.py", True),
        ("/r/a[b.py", "/r/a[b.py", True),
    ],
)
def test_glob_to_regex(glob: str, path: str, expected: bool) -> None:
    assert bool(rules.glob_to_regex(glob).fullmatch(path)) is expected


# ---------------------------------------------------------------------------
# parsing, merging, roots
# ---------------------------------------------------------------------------


def test_missing_config_dir_means_no_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rules.CONFIG_DIR_ENV, str(tmp_path / "nope"))
    assert rules.evaluate("rm -rf x", str(tmp_path)) == rules.RulesVerdict()
    assert rules.configured_default_agent_cap() is None


def test_config_dir_env_override_and_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(rules.CONFIG_DIR_ENV, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert rules.config_dir() == tmp_path / "xdg" / "shell-runner"
    monkeypatch.setenv(rules.CONFIG_DIR_ENV, str(tmp_path / "x"))
    assert rules.config_dir() == tmp_path / "x"


def test_project_rules_apply_only_under_roots(cfg: Path, proj: Path, tmp_path: Path) -> None:
    write_project(cfg, "p", PROJECT_TOML.format(root=proj))
    sibling = tmp_path / "proj2"  # shares the string prefix, is not under root
    sibling.mkdir()
    sub = proj / "tests"
    cmd = "rm tests/acceptance/a.py"
    assert kinds(cmd, proj) == {"write"}
    assert kinds("rm acceptance/a.py", sub) == {"write"}
    assert kinds(cmd, sibling) == set()
    assert rules.evaluate("pixi run acceptance-record", str(sibling)).deny is None
    assert rules.evaluate("pixi run acceptance-record", str(proj)).deny is not None


def test_roots_match_through_symlinks(cfg: Path, proj: Path, tmp_path: Path) -> None:
    write_project(cfg, "p", PROJECT_TOML.format(root=proj))
    link = tmp_path / "link"
    link.symlink_to(proj)
    assert kinds("rm tests/acceptance/a.py", link) == {"write"}


def test_global_and_multiple_projects_are_merged(cfg: Path, proj: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    write_global(
        cfg,
        f"""
[[deny]]
pattern = "forbidden-global"
reason = "g"

[[protected]]
glob = "{proj}/tests/acceptance/**"
mode = "flag"
""",
    )
    write_project(cfg, "a", PROJECT_TOML.format(root=proj))
    write_project(
        cfg,
        "b",
        f'roots = ["{proj}"]\n[[deny]]\npattern = "forbidden-b"\nreason = "b"\n',
    )
    write_project(
        cfg, "c", f'roots = ["{other}"]\n[[deny]]\npattern = "forbidden-c"\nreason = "c"\n'
    )
    assert rules.evaluate("forbidden-global", str(other)).deny is not None
    assert rules.evaluate("forbidden-b", str(proj)).deny is not None
    assert rules.evaluate("forbidden-b", str(other)).deny is None
    assert rules.evaluate("forbidden-c", str(proj)).deny is None
    verdict = rules.evaluate("rm tests/acceptance/a.py", str(proj))
    assert {(m.mode, m.source_file.rsplit("/", 1)[-1]) for m in verdict.write_matches} == {
        ("flag", "rules.toml"),
        ("deny", "a.toml"),
    }


def test_global_glob_with_tilde(cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    write_global(cfg, '[[protected]]\nglob = "~/secrets/**"\n')
    assert kinds("touch ~/secrets/key", tmp_path) == {"write"}
    assert kinds("touch ../home/secrets/key", home) == {"write"}


def test_relative_glob_resolves_against_matching_root(
    cfg: Path, proj: Path, tmp_path: Path
) -> None:
    write_project(cfg, "p", PROJECT_TOML.format(root=proj))
    # A same-named relative path in a cwd that is not under the root never matches.
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "tests" / "acceptance").mkdir(parents=True)
    assert kinds("rm tests/acceptance/a.py", elsewhere) == set()
    # `..` is normalised without requiring the file to exist.
    assert kinds("rm tests/x/../acceptance/a.py", proj) == {"write"}
    # An absolute path to the protected file matches from anywhere under the root.
    assert kinds(f"rm {proj}/tests/acceptance/a.py", proj / "tests") == {"write"}


# ---------------------------------------------------------------------------
# fail closed
# ---------------------------------------------------------------------------


BAD_FILES = {
    "bad_toml": ("[[deny", "rules.toml"),
    "bad_regex": ('[[deny]]\npattern = "("\n', "rules.toml"),
    "bad_mode": ('[[protected]]\nglob = "/a/**"\nmode = "warn"\n', "rules.toml"),
    "bad_tier": ('default_agent_cap = "NOPE"\n', "rules.toml"),
    "relative_global_glob": ('[[protected]]\nglob = "tests/**"\n', "rules.toml"),
    "unknown_key": ('[[protectd]]\nglob = "/a/**"\n', "rules.toml"),
    "missing_glob": ("[[protected]]\nmode = 'deny'\n", "rules.toml"),
}


@pytest.mark.parametrize("name", sorted(BAD_FILES))
def test_bad_global_file_fails_closed(cfg: Path, tmp_path: Path, name: str) -> None:
    text, fname = BAD_FILES[name]
    write_global(cfg, text)
    result = classify("ls", str(tmp_path), AGENT)
    assert result.tier == Tier.DENY
    assert result.matched_rule is not None
    assert result.matched_rule.category == "rules_config_error"
    reason = denial_reason(result.command_tier, result.matched_rule, AGENT)
    assert fname in reason


def test_bad_project_file_fails_closed_everywhere(cfg: Path, tmp_path: Path) -> None:
    write_project(cfg, "broken", 'roots = ["relative/path"]\n')
    other = tmp_path / "unrelated"
    other.mkdir()
    result = classify("ls", str(other), AGENT)
    assert result.tier == Tier.DENY
    assert result.matched_rule is not None
    assert result.matched_rule.category == "rules_config_error"
    assert "broken.toml" in denial_reason(result.command_tier, result.matched_rule, AGENT)


def test_config_error_makes_default_agent_cap_fall_back(cfg: Path) -> None:
    write_global(cfg, "[[deny")
    assert lookup_agent_cap("some-agent") == Tier.AUTO_CAPPED


def test_mtime_change_reloads_and_recovers(cfg: Path, tmp_path: Path) -> None:
    path = write_global(cfg, "[[deny")
    assert classify("ls", str(tmp_path), AGENT).tier == Tier.DENY
    path.write_text("")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert classify("ls", str(tmp_path), AGENT).tier != Tier.DENY


def test_new_project_file_is_picked_up(cfg: Path, proj: Path) -> None:
    assert rules.evaluate("pixi run acceptance-record", str(proj)).deny is None
    write_project(cfg, "p", PROJECT_TOML.format(root=proj))
    assert rules.evaluate("pixi run acceptance-record", str(proj)).deny is not None
    (cfg / "projects" / "p.toml").unlink()
    assert rules.evaluate("pixi run acceptance-record", str(proj)).deny is None


# ---------------------------------------------------------------------------
# default_agent_cap
# ---------------------------------------------------------------------------


def test_default_agent_cap_override(cfg: Path) -> None:
    assert lookup_agent_cap("brand-new-agent") == Tier.AUTO_CAPPED
    write_global(cfg, 'default_agent_cap = "APPROVE_ONCE"\n')
    assert lookup_agent_cap("brand-new-agent") == Tier.APPROVE_ONCE
    # Known agents and the primary identity are unaffected.
    assert lookup_agent_cap("focused-code-modifier") == Tier.AUTO_CAPPED
    assert lookup_agent_cap("primary") == Tier.DENY


def test_default_agent_cap_is_global_file_only(cfg: Path, proj: Path) -> None:
    write_project(cfg, "p", f'roots = ["{proj}"]\ndefault_agent_cap = "DENY"\n')
    result = classify("ls", str(proj), AGENT)
    assert result.matched_rule is not None
    assert result.matched_rule.category == "rules_config_error"


# ---------------------------------------------------------------------------
# config deny
# ---------------------------------------------------------------------------


def test_config_deny_pattern_denies_with_reason(guarded: Path) -> None:
    result = classify("pixi run acceptance-record --all", str(guarded), AGENT)
    assert result.tier == Tier.DENY
    assert result.command_tier == Tier.DENY
    assert result.matched_rule is not None
    assert result.matched_rule.category == "config_deny"
    assert "do not re-record acceptance" in denial_reason(
        result.command_tier, result.matched_rule, AGENT
    )


def test_t0_wins_over_config_deny(cfg: Path, tmp_path: Path) -> None:
    write_global(cfg, '[[deny]]\npattern = "rm"\nreason = "no rm"\n')
    result = classify("rm -rf /", str(tmp_path), AGENT)
    assert result.tier == Tier.DENY
    assert result.matched_rule is not None
    assert result.matched_rule.category == "destructive"


# ---------------------------------------------------------------------------
# write-target vectors (each must be a WRITE match on a protected file)
# ---------------------------------------------------------------------------

A = "tests/acceptance/a.py"
H = "tests/acceptance.sha256"

WRITE_VECTORS = {
    "redirect": f"echo x > {A}",
    "redirect_append": f"echo x >> {A}",
    "redirect_amp": f"echo x &> {H}",
    "redirect_stderr": f"cmd 2> {A}",
    "redirect_glued": f"echo x >{A}",
    "redirect_in_pipeline": f"echo x | cat > {A}",
    "tee": f"echo x | tee {A}",
    "tee_append": f"echo x | tee -a {H}",
    "sed_i": f"sed -i s/a/b/ {A}",
    "sed_i_suffix": f"sed -i.bak -e s/a/b/ {A}",
    "sed_in_place": f"sed --in-place -e s/a/b/ {A}",
    "cp": f"cp foo {A}",
    "cp_t": "cp -t tests/acceptance foo",
    "cp_target_directory": "cp --target-directory=tests/acceptance foo",
    "mv": f"mv foo {A}",
    "mv_source": f"mv {A} /tmp/elsewhere",
    "install": f"install -m 644 foo {A}",
    "ln": f"ln -sf foo {A}",
    "rm": f"rm -f {A}",
    "rm_dir": "rm -rf tests/acceptance",
    "rmdir": "rmdir tests/acceptance",
    "truncate": f"truncate -s 0 {H}",
    "touch": f"touch {A}",
    "chmod": f"chmod 644 {A}",
    "chown": f"chown me {A}",
    "shred": f"shred -u {A}",
    "unlink": f"unlink {A}",
    "dd": f"dd if=/dev/zero of={H}",
    "git_apply": f"git apply {A}",
    "git_apply_directory": "git apply --directory=tests/acceptance p.patch",
    "git_apply_directory_sep": "git apply --directory tests/acceptance p.patch",
    "git_checkout_dashdash": f"git checkout -- {A}",
    "git_checkout_head": f"git checkout HEAD -- {A}",
    "git_checkout_noopt": f"git checkout {A}",
    "git_restore": f"git restore --source=HEAD~1 {A}",
    "git_rm": f"git rm -f {A}",
    "git_mv": f"git mv foo {A}",
    "git_dash_C": f"git -C . checkout -- {A}",
    "python_c": "python3 -c \"open('tests/acceptance/a.py', 'w').write('x')\"",
    "perl_e": "perl -e 'unlink q(tests/acceptance.sha256)'",
    "ruby_e": 'ruby -e \'File.write("tests/acceptance.sha256", "")\'',
    "perl_i": f"perl -pi -e s/a/b/ {A}",
    "bash_c": f"bash -c 'echo x > {A}'",
    "env_prefix": f"FOO=1 rm {A}",
    "after_and": f"true && rm {A}",
    "after_newline": f"true\nrm {A}",
    "dotdot": "rm tests/x/../acceptance/a.py",
    "absolute_binary": f"/bin/rm {A}",
}


@pytest.mark.parametrize("name", sorted(WRITE_VECTORS))
def test_write_vector_is_detected(guarded: Path, name: str) -> None:
    verdict = rules.evaluate(WRITE_VECTORS[name], str(guarded))
    assert verdict.write_matches, WRITE_VECTORS[name]
    assert verdict.flagged
    assert verdict.denying_match is not None
    for m in verdict.write_matches:
        assert Path(m.path).is_relative_to(guarded)


@pytest.mark.parametrize("name", ["redirect", "cp", "rm", "git_restore", "python_c"])
def test_deny_mode_write_denies_via_classify(guarded: Path, name: str) -> None:
    result = classify(WRITE_VECTORS[name], str(guarded), AGENT)
    assert result.tier == Tier.DENY
    assert result.command_tier == Tier.DENY
    assert result.flagged is True
    assert result.matched_rule is not None
    assert result.matched_rule.category == "protected_path"
    assert "acceptance specs are frozen" in denial_reason(
        result.command_tier, result.matched_rule, AGENT
    ) or "acceptance hash is frozen" in denial_reason(
        result.command_tier, result.matched_rule, AGENT
    )


@pytest.mark.parametrize(
    "command",
    [
        "ls tests",
        "cat tests/other.py > out.txt",
        "cp tests/acceptance/a.py /tmp/copy",  # source of a copy is a read
        "echo x > tests/acceptance_notes.txt",
        "sed s/a/b/ tests/acceptance/a.py",  # no -i: read only
        "git checkout main",
        "python3 -c 'print(1)'",
    ],
)
def test_non_writes_do_not_match_as_write(guarded: Path, command: str) -> None:
    verdict = rules.evaluate(command, str(guarded))
    assert not verdict.write_matches
    assert not verdict.flagged


@pytest.mark.parametrize(
    "command",
    [
        "cat tests/acceptance/a.py",
        "grep -n foo tests/acceptance/a.py",
        "cp tests/acceptance/a.py /tmp/copy",
        "sed s/a/b/ tests/acceptance/a.py",
        "wc -l tests/acceptance.sha256",
    ],
)
def test_touch_is_recorded_but_never_denies(guarded: Path, command: str) -> None:
    verdict = rules.evaluate(command, str(guarded))
    assert {m.kind for m in verdict.matches} == {"touch"}
    assert not verdict.flagged
    plain = classify(command, str(guarded / "tests" / ".."), AGENT)
    assert plain.tier != Tier.DENY
    assert plain.flagged is False
    assert plain.protected_matches
    assert plain.matched_rule is None or plain.matched_rule.category != "protected_path"


def test_write_and_touch_reported_separately(guarded: Path) -> None:
    verdict = rules.evaluate(f"cp {A} tests/acceptance/b.py", str(guarded))
    by_kind = {(m.kind, Path(m.path).name) for m in verdict.matches}
    assert by_kind == {("write", "b.py"), ("touch", "a.py")}


def test_same_path_write_is_not_also_reported_as_touch(guarded: Path) -> None:
    verdict = rules.evaluate(f"rm {A}", str(guarded))
    assert [m.kind for m in verdict.matches] == ["write"]


# ---------------------------------------------------------------------------
# flag mode vs deny mode
# ---------------------------------------------------------------------------


def test_flag_mode_leaves_tier_unchanged_but_flags(cfg: Path, proj: Path) -> None:
    flag_toml = f'roots = ["{proj}"]\n[[protected]]\nglob = "tests/acceptance/**"\nmode = "flag"\n'
    command = f"touch {A}"
    baseline = classify(command, str(proj), AGENT)
    write_project(cfg, "p", flag_toml)
    flagged = classify(command, str(proj), AGENT)
    assert flagged.tier == baseline.tier != Tier.DENY
    assert flagged.command_tier == baseline.command_tier
    assert flagged.flagged is True
    assert flagged.protected_matches[0].mode == "flag"
    assert flagged.protected_matches[0].kind == "write"


def test_deny_mode_is_default(cfg: Path, proj: Path) -> None:
    write_project(cfg, "p", f'roots = ["{proj}"]\n[[protected]]\nglob = "tests/**"\n')
    verdict = rules.evaluate("rm tests/x", str(proj))
    assert verdict.denying_match is not None
    assert verdict.denying_match.mode == "deny"


BASE_DIR_EVASIONS = {
    "cd_and": "cd tests && rm acceptance/x",
    "cd_semicolon": "cd tests; echo y > acceptance/x",
    "pushd": "pushd tests && sed -i s/a/b/ acceptance/x",
    "git_C": "git -C tests checkout -- acceptance/x",
    "tar_C": "tar -xf a.tar -C tests/acceptance",
    "chained_cd": "cd tests && cd acceptance && rm x",
    "chained_cd_from_parent": "cd a && cd tests && rm acceptance/x",
    "cd_or_pipe": "false || cd tests | rm acceptance/x",
    "cd_then_git_C": "cd tests && git -C acceptance checkout -- x",
}


@pytest.mark.parametrize("mode", ["deny", "flag"])
@pytest.mark.parametrize("name", sorted(BASE_DIR_EVASIONS))
def test_base_dir_changes_do_not_evade_protection(
    cfg: Path, proj: Path, name: str, mode: str
) -> None:
    toml = f'roots = ["{proj}"]\n[[protected]]\nglob = "tests/acceptance/**"\nmode = "{mode}"\n'
    write_project(cfg, "p", toml)
    verdict = rules.evaluate(BASE_DIR_EVASIONS[name], str(proj))
    assert verdict.flagged
    assert (verdict.denying_match is not None) == (mode == "deny")


def test_make_C_is_only_a_touch(guarded: Path) -> None:
    verdict = rules.evaluate("make -C tests/acceptance all", str(guarded))
    assert verdict.matches
    assert not verdict.write_matches


def test_git_C_base_applies_to_its_own_segment_only(guarded: Path) -> None:
    assert not rules.evaluate("git -C tests status && rm x", str(guarded)).matches
    assert not rules.evaluate("git -C other checkout -- x", str(guarded)).matches


def test_cd_into_unrelated_dir_has_no_false_matches(guarded: Path) -> None:
    for command in ("cd src && rm foo.py", "cd /tmp && rm acceptance_x", "pushd lib; touch a/b"):
        assert not rules.evaluate(command, str(guarded)).matches, command


@pytest.mark.parametrize("command", ["cd - && rm x", "cd && rm x", "cd $D && rm x"])
def test_untrackable_cd_warns_without_adding_base(guarded: Path, command: str) -> None:
    verdict = rules.evaluate(command, str(guarded))
    assert verdict.warnings
    assert not verdict.matches


def test_protected_path_json_shape(guarded: Path) -> None:
    result = classify(f"rm {A}", str(guarded), AGENT)
    assert result.protected_path_json is not None
    (entry,) = json.loads(result.protected_path_json)
    assert set(entry) == {"path", "glob", "mode", "kind", "source_file"}
    assert entry["kind"] == "write"
    assert entry["mode"] == "deny"
    assert entry["glob"] == "tests/acceptance/**"
    assert entry["source_file"].endswith("p.toml")
    assert entry["path"] == str(Path(os.path.realpath(guarded)) / A)
    assert classify("ls", str(guarded), AGENT).protected_path_json is None


# ---------------------------------------------------------------------------
# audit columns / migration
# ---------------------------------------------------------------------------

OLD_SCHEMA = """
CREATE TABLE shell_calls (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    raw_cmd TEXT NOT NULL,
    normalized_template TEXT NOT NULL,
    command_tier INTEGER NOT NULL,
    final_tier INTEGER NOT NULL,
    decision TEXT NOT NULL,
    matched_rule_pattern TEXT,
    matched_rule_category TEXT,
    exit_code INTEGER,
    output_bytes_stdout INTEGER,
    output_bytes_stderr INTEGER,
    duration_ms INTEGER,
    decision_path_json TEXT,
    normalizer_warnings_json TEXT
);
"""

CALL = {
    "agent_id": AGENT,
    "cwd": "/tmp",
    "raw_cmd": "rm x",
    "normalized_template": "rm x",
    "command_tier": 0,
    "final_tier": 0,
    "decision": "denied",
    "matched_rule_pattern": None,
    "matched_rule_category": "protected_path",
    "exit_code": None,
    "stdout_bytes": 0,
    "stderr_bytes": 0,
    "duration_ms": 1,
    "decision_path": [],
    "normalizer_warnings": [],
}


def test_old_schema_db_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    raw = sqlite3.connect(path)
    raw.executescript(OLD_SCHEMA)
    raw.execute(
        "INSERT INTO shell_calls VALUES ('old','2024-01-01T00:00:00+00:00','a','/','ls','ls',"
        "1,1,'executed',NULL,NULL,0,0,0,1,'[]','[]')"
    )
    raw.commit()
    raw.close()

    db = Persistence(db_path=path)
    Persistence(db_path=path)  # second init is a no-op
    match = json.dumps([{"path": "/p", "glob": "g", "mode": "deny", "kind": "write"}])
    db.record_call(**CALL, call_id="new", protected_path_match=match, flagged=1)  # type: ignore[arg-type]

    rows = {r["id"]: r for r in db.telemetry_query()}
    assert rows["old"]["flagged"] == 0
    assert rows["old"]["protected_path_match"] is None
    assert rows["new"]["flagged"] == 1
    assert json.loads(rows["new"]["protected_path_match"])[0]["path"] == "/p"


def test_fresh_db_has_audit_columns_with_defaults(tmp_path: Path) -> None:
    db = Persistence(db_path=tmp_path / "new.sqlite3")
    db.record_call(**CALL, call_id="c")  # type: ignore[arg-type]
    (row,) = db.telemetry_query()
    assert row["flagged"] == 0
    assert row["protected_path_match"] is None
