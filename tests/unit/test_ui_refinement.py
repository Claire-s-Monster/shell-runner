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
