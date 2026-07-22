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


import httpx as _httpx

from ui.refinement import build_issue_body, file_github_issue, find_open_issue


def test_build_issue_body_has_marker_and_labels():
    title, body, labels = build_issue_body(_valid_proposal(), "curl https://x", "curl <safe_url>")
    assert "<!-- dedupe:curl-fam -->" in body
    assert labels == ["classifier", "rule-enhancement", "ai-proposed", "needs-review"]
    assert "curl <safe_url>" in body
    assert title == "t"


def test_build_issue_body_redacts_defensively():
    _, body, _ = build_issue_body(_valid_proposal(), "curl -u me:PASSWORDX https://x", "curl <safe_url>")
    assert "PASSWORDX" not in body
    assert "‹REDACTED›" in body


def test_find_open_issue_hit_and_miss():
    def handler(request):
        assert request.url.path == "/search/issues"
        if "hitkey" in request.url.params["q"]:
            return _httpx.Response(200, json={"items": [{"html_url": "https://gh/issues/1"}]})
        return _httpx.Response(200, json={"items": []})

    t = _httpx.MockTransport(handler)
    assert find_open_issue("hitkey", "o/r", "tok", transport=t) == "https://gh/issues/1"
    assert find_open_issue("misskey", "o/r", "tok", transport=t) is None


def test_file_github_issue_creates_when_no_duplicate():
    posted = {}

    def handler(request):
        if request.url.path == "/search/issues":
            return _httpx.Response(200, json={"items": []})
        if request.method == "POST" and request.url.path == "/repos/o/r/issues":
            posted["auth"] = request.headers.get("authorization")
            posted["body"] = _json.loads(request.content)
            return _httpx.Response(201, json={"html_url": "https://gh/issues/2"})
        return _httpx.Response(404)

    t = _httpx.MockTransport(handler)
    out = file_github_issue(_valid_proposal(), "curl https://x", "curl <safe_url>", "o/r", "tok", transport=t)
    assert out == {"status": "created", "url": "https://gh/issues/2"}
    assert posted["auth"] == "Bearer tok"
    assert posted["body"]["labels"] == ["classifier", "rule-enhancement", "ai-proposed", "needs-review"]


def test_file_github_issue_dedupes_without_posting():
    calls = {"post": 0}

    def handler(request):
        if request.url.path == "/search/issues":
            return _httpx.Response(200, json={"items": [{"html_url": "https://gh/issues/9"}]})
        if request.method == "POST":
            calls["post"] += 1
        return _httpx.Response(201, json={"html_url": "https://gh/nope"})

    t = _httpx.MockTransport(handler)
    out = file_github_issue(_valid_proposal(), "curl https://x", "curl <safe_url>", "o/r", "tok", transport=t)
    assert out["status"] == "duplicate"
    assert out["url"] == "https://gh/issues/9"
    assert calls["post"] == 0


def test_redact_url_userinfo():
    out = redact("curl https://user:s3cr3tPASS@host.example/x")
    assert "s3cr3tPASS" not in out
    assert "host.example" in out
    assert R in out


def test_redact_json_body_flag():
    out = redact('curl --json \'{"token":"SECRETJSONVAL"}\' https://api.x')
    assert "SECRETJSONVAL" not in out
    assert R in out


def test_build_issue_body_neutralizes_fence_injection():
    p = _valid_proposal()
    p["proposed_rule"] = None
    p["existing_lever"] = "approve_verb"
    p["risk_notes"] = "safe ``` breakout ``` attempt"
    p["missed_reason"] = "``` also here"
    _, body, _ = build_issue_body(p, "curl https://x", "curl <safe_url>")
    assert "```" not in body


def test_build_analysis_prompt_frames_untrusted():
    prompt = build_analysis_prompt("ls", "ls", 3, None, [], [])
    assert "UNTRUSTED" in prompt
    assert "Do NOT" in prompt or "do not" in prompt.lower()
