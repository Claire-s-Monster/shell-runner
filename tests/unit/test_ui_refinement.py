"""Unit tests for ui.refinement."""

import pytest

from ui.refinement import redact

R = "‹REDACTED›"


@pytest.mark.parametrize(
    "raw,secret",
    [
        ('curl -H "Authorization: Bearer sk-abc1234567890TOKENVALUE" https://api.x', "sk-abc1234567890TOKENVALUE"),
        ("curl -u admin:hunter2 https://api.x", "hunter2"),
        ("curl --user=admin:hunter2 https://api.x", "hunter2"),
        ('curl --data "password=hunter2&x=1" https://api.x', "hunter2"),
        ('curl "https://api.x/v2?api_key=SECRETKEY123&page=2"', "SECRETKEY123"),
        ("gh auth login --with-token ghp_0123456789abcdef0123456789abcdef0123", "ghp_0123456789abcdef0123456789abcdef0123"),
        ("echo eyJhbGciOi.eyJzdWIiOiIxIn0.sig-part", "eyJhbGciOi.eyJzdWIiOiIxIn0.sig-part"),
        ("curl -H 'X-Company-Auth: abc123def456xyz789' https://api.x", "abc123def456xyz789"),
    ],
)
def test_redact_removes_secret(raw, secret):
    out = redact(raw)
    assert secret not in out
    assert R in out


def test_redact_preserves_benign_command():
    raw = "curl -sSL https://example.com/v2/status?page=2"
    assert redact(raw) == raw


def test_redact_keeps_query_key_names_visible():
    out = redact("curl 'https://api.x?api_key=ABC123XYZ&page=2'")
    assert "api_key=" in out and "page=2" in out and "ABC123XYZ" not in out


def test_redact_empty():
    assert redact("") == ""


from ui.refinement import build_analysis_prompt, validate_proposal


def _valid_proposal():
    return {
        "missed_reason": "x",
        "existing_lever": "none",
        "proposed_rule": {"pattern": "curl .*", "tier": "T2", "category": "net",
                          "match_target": "template", "reason": "r"},
        "catalog_section": "T2_AUTO_CAPPED",
        "confidence": 0.7,
        "dedupe_key": "curl-fam",
        "issue_title": "t",
        "risk_notes": "n",
    }


def test_validate_proposal_accepts_valid():
    assert validate_proposal(_valid_proposal())["dedupe_key"] == "curl-fam"


def test_validate_proposal_accepts_existing_lever_null_rule():
    p = _valid_proposal()
    p["existing_lever"] = "approve_verb"
    p["proposed_rule"] = None
    assert validate_proposal(p)["proposed_rule"] is None


def test_validate_proposal_missing_key_raises():
    p = _valid_proposal()
    del p["dedupe_key"]
    with pytest.raises(ValueError):
        validate_proposal(p)


def test_validate_proposal_bad_tier_raises():
    p = _valid_proposal()
    p["proposed_rule"]["tier"] = "T9"
    with pytest.raises(ValueError):
        validate_proposal(p)


def test_validate_proposal_bad_confidence_raises():
    p = _valid_proposal()
    p["confidence"] = "high"
    with pytest.raises(ValueError):
        validate_proposal(p)


def test_build_analysis_prompt_redacts_raw_input():
    p = build_analysis_prompt(
        raw_cmd='curl -H "Authorization: Bearer sk-abc1234567890SECRETVALUE" https://api.x',
        normalized_template="curl <safe_url>",
        command_tier=4,
        matched_rule_category="network-write",
        decision_path=["segment 'curl': T4 -> ALWAYS_APPROVE"],
        similar=[{"template": "curl <safe_url>", "approved_tier": 2,
                  "similarity": 0.9, "example_raw_cmd": "curl -u me:PASSWORDX https://y"}],
    )
    assert "sk-abc1234567890SECRETVALUE" not in p
    assert "PASSWORDX" not in p
    assert "‹REDACTED›" in p


def test_build_analysis_prompt_contains_context_and_schema():
    p = build_analysis_prompt("wget http://x", "wget <safe_url>", 3, None, [], [])
    assert "wget <safe_url>" in p
    assert "src/shell_runner/catalog.py" in p
    assert "normalized_template" in p
    for key in ["missed_reason", "existing_lever", "proposed_rule", "catalog_section",
                "confidence", "dedupe_key", "issue_title", "risk_notes"]:
        assert key in p


import json as _json
import sys

from ui.refinement import AnalysisError, _build_argv, _scrubbed_env, run_claude_analysis


def _write_fake_claude(tmp_path, envelope, exit_code=0):
    script = tmp_path / "fakeclaude.py"
    script.write_text(
        "import json, sys\n"
        f"sys.stdout.write(json.dumps({envelope!r}))\n"
        f"sys.exit({exit_code})\n"
    )
    launcher = tmp_path / "fakeclaude"
    launcher.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n")
    launcher.chmod(0o755)
    return str(launcher)


def _proposal_text():
    return "```json\n" + _json.dumps(_valid_proposal()) + "\n```"


def test_run_claude_analysis_parses_valid_envelope(tmp_path):
    fake = _write_fake_claude(tmp_path, {"type": "result", "result": _proposal_text(), "is_error": False})
    out = run_claude_analysis("p", tmp_path, claude_bin=fake, timeout_s=30)
    assert out["dedupe_key"] == "curl-fam"
    assert out["proposed_rule"]["tier"] == "T2"


def test_run_claude_analysis_is_error_raises(tmp_path):
    fake = _write_fake_claude(tmp_path, {"type": "result", "result": "boom", "is_error": True})
    with pytest.raises(AnalysisError):
        run_claude_analysis("p", tmp_path, claude_bin=fake, timeout_s=30)


def test_run_claude_analysis_nonzero_exit_raises(tmp_path):
    fake = _write_fake_claude(tmp_path, {"type": "result", "result": _proposal_text(), "is_error": False}, exit_code=1)
    with pytest.raises(AnalysisError):
        run_claude_analysis("p", tmp_path, claude_bin=fake, timeout_s=30)


def test_build_argv_enforces_readonly():
    argv = _build_argv("claude", "the-prompt")
    assert "the-prompt" in argv
    assert "--strict-mcp-config" in argv
    di = argv[argv.index("--disallowedTools") + 1]
    assert "mcp__*" in di and "Bash" in di
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"


def test_scrubbed_env_removes_tokens(monkeypatch):
    monkeypatch.setenv("SHELL_RUNNER_GH_TOKEN", "secrettoken")
    monkeypatch.setenv("GITHUB_TOKEN", "secrettoken2")
    env = _scrubbed_env()
    assert "SHELL_RUNNER_GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env
