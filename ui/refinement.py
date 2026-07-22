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


class ProposalError(ValueError):
    """Raised when a Claude proposal fails schema validation."""


_TIERS = {"T1", "T2"}
_LEVERS = {"approve_verb", "approve_template_global", "none"}
_SECTIONS = {"T0_DENY", "T1_AUTO_LOG", "T2_AUTO_CAPPED", "T4_ALWAYS_APPROVE"}
_RULE_KEYS = {"pattern", "tier", "category", "match_target", "reason"}
_MATCH_TARGETS = {"template", "raw"}


def validate_proposal(obj: dict) -> dict:
    """Validate a Claude rule-proposal dict; raise ProposalError (a ValueError) on any problem."""
    if not isinstance(obj, dict):
        raise ProposalError("proposal is not an object")
    required = {
        "missed_reason", "existing_lever", "proposed_rule", "catalog_section",
        "confidence", "dedupe_key", "issue_title", "risk_notes",
    }
    missing = required - obj.keys()
    if missing:
        raise ProposalError(f"missing keys: {sorted(missing)}")
    for k in ("missed_reason", "dedupe_key", "issue_title", "risk_notes"):
        if not isinstance(obj[k], str) or not obj[k].strip():
            raise ProposalError(f"{k} must be a non-empty string")
    if obj["existing_lever"] not in _LEVERS:
        raise ProposalError(f"existing_lever must be one of {sorted(_LEVERS)}")
    if obj["catalog_section"] not in _SECTIONS:
        raise ProposalError(f"catalog_section must be one of {sorted(_SECTIONS)}")
    conf = obj["confidence"]
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not (0.0 <= conf <= 1.0):
        raise ProposalError("confidence must be a number in [0.0, 1.0]")
    rule = obj["proposed_rule"]
    if rule is None:
        if obj["existing_lever"] == "none":
            raise ProposalError("proposed_rule may be null only when existing_lever != 'none'")
    else:
        if not isinstance(rule, dict) or set(rule.keys()) != _RULE_KEYS:
            raise ProposalError(f"proposed_rule must have exactly keys {sorted(_RULE_KEYS)}")
        if rule["tier"] not in _TIERS:
            raise ProposalError(f"proposed_rule.tier must be one of {sorted(_TIERS)}")
        if rule["match_target"] not in _MATCH_TARGETS:
            raise ProposalError(f"proposed_rule.match_target must be one of {sorted(_MATCH_TARGETS)}")
        for k in ("pattern", "category", "reason"):
            if not isinstance(rule[k], str) or not rule[k].strip():
                raise ProposalError(f"proposed_rule.{k} must be a non-empty string")
    return obj


_SCHEMA_KEYS = (
    "missed_reason, existing_lever, proposed_rule, catalog_section, "
    "confidence, dedupe_key, issue_title, risk_notes"
)


def build_analysis_prompt(
    raw_cmd: str,
    normalized_template: str,
    command_tier: int,
    matched_rule_category: str | None,
    decision_path: list[str],
    similar: list[dict],
) -> str:
    """Build the read-only analysis prompt. Redacts raw_cmd and similar examples IN-FUNCTION."""
    safe_cmd = redact(raw_cmd)
    safe_similar = []
    for s in similar:
        ex = s.get("example_raw_cmd")
        safe_similar.append(
            f"- {s.get('template')!r} (T{s.get('approved_tier')}, sim={s.get('similarity')}): "
            f"{redact(ex) if ex else '(no example)'}"
        )
    similar_block = "\n".join(safe_similar) if safe_similar else "(none)"
    path_block = "\n".join(f"  {step}" for step in decision_path) if decision_path else "  (none)"
    return f"""You are analysing why a shell command required manual approval in the shell-runner classifier, to propose a rule enhancement.

Read these files to ground your analysis:
- src/shell_runner/catalog.py  (the Rule dataclass and the four tier lists T0_DENY/T1_AUTO_LOG/T2_AUTO_CAPPED/T4_ALWAYS_APPROVE)
- src/shell_runner/classifier.py
- src/shell_runner/normalizer.py

The pending command (secrets already redacted):
  command:             {safe_cmd}
  normalized_template: {normalized_template}
  command_tier:        T{command_tier}
  matched_rule_category: {matched_rule_category or "(none — T3 fallthrough)"}
  decision_path:
{path_block}

Similar past approvals (for reference):
{similar_block}

Explain why `normalized_template` did not match an auto-execute rule. Prefer recommending an EXISTING lever (approve_verb, or approve_template_global) when one would generalise this safely; only propose a NEW catalog Rule if no lever fits.

Emit ONLY a single JSON object (no prose, no markdown fences) as your final message, with exactly these keys: {_SCHEMA_KEYS}. Use existing_lever="none" and a non-null proposed_rule when proposing a rule; set proposed_rule=null and existing_lever to the lever name when recommending a lever. tier must be "T1" or "T2"; match_target "template" or "raw"; confidence a number 0..1; dedupe_key a stable slug for this command family."""
