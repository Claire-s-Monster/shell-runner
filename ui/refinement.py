"""Backend helpers for the pending-prompt rule-refinement feature.

Streamlit-free so every function here is unit-testable in isolation. The
redaction pass is the load-bearing safety property: anything sent to the
analysis subprocess or written into a GitHub issue must pass through redact().
"""

from __future__ import annotations

import re

REDACTED = "‹REDACTED›"

# Rule 6 — known secret token shapes, redacted anywhere they appear.
_TOKEN_SHAPES = [
    re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),  # JWT
    re.compile(r"sk-[A-Za-z0-9]{20,}"),                                 # OpenAI-style
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),                          # GitHub tokens
    re.compile(r"AKIA[0-9A-Z]{16}"),                                    # AWS access key id
]

# Rules 1&2 — redact the value of EVERY -H/--header (over-redaction: header
# values are never needed to analyse a normalized template, and a custom
# secret header name would otherwise slip through). Keeps "Name:" visible.
_HEADER_QUOTED = re.compile(r"((?:-H|--header)[\s=]+['\"][^'\":]+:\s*)([^'\"]*)(['\"])")
_HEADER_BARE = re.compile(r"((?:-H|--header)[\s=]+)([^\s'\"]+:[^\s'\"]+)")

# Rule 3 — -u/--user credentials.
_USER = re.compile(r"((?:-u|--user)[\s=]+)(['\"]?)([^\s'\"]+)(\2)")

# Rule 4 — request bodies.
_DATA = re.compile(
    r"((?:--data-raw|--data-binary|--data-urlencode|--data|-d)[\s=]+)(['\"]?)(.+?)(\2)(?=\s|$)"
)

# Rule 5 — sensitive query/kv values (keep the key name visible). Longer keys
# are listed first so the alternation prefers them.
_KV = re.compile(
    r"(?i)(api[-_]?key|access[-_]?token|secret|password|token|signature|sig|key)(=)([^&\s'\"]+)"
)


def redact(text: str) -> str:
    """Replace credential-bearing substrings with a sentinel. Over-redacts by design."""
    if not text:
        return text
    out = text
    out = _HEADER_QUOTED.sub(lambda m: m.group(1) + REDACTED + m.group(3), out)
    out = _HEADER_BARE.sub(lambda m: m.group(1) + REDACTED, out)
    out = _USER.sub(lambda m: m.group(1) + REDACTED, out)
    out = _DATA.sub(lambda m: m.group(1) + REDACTED, out)
    out = _KV.sub(lambda m: m.group(1) + m.group(2) + REDACTED, out)
    for _pat in _TOKEN_SHAPES:
        out = _pat.sub(REDACTED, out)
    return out
