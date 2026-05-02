"""Tier classifier for shell-runner MCP server.

Combines normalizer output with catalog rules to assign a final tier to a shell
command. Handles per-segment pipe evaluation and per-agent caps.

Public API:
    classify(command, cwd, agent_id, *, env=None) -> ClassificationResult
    lookup_agent_cap(agent_id) -> Tier
    PERMISSIVENESS: dict[Tier, int]
"""

from __future__ import annotations

from dataclasses import dataclass

from .catalog import (
    T0_DENY,
    T1_AUTO_LOG,
    T2_AUTO_CAPPED,
    T4_ALWAYS_APPROVE,
    Rule,
    Tier,
    compiled_for,
)
from .normalizer import Segment, normalize

# ---------------------------------------------------------------------------
# Permissiveness map — higher = more permissive (more autonomous, less oversight)
# ---------------------------------------------------------------------------

PERMISSIVENESS: dict[Tier, int] = {
    Tier.AUTO_LOG: 4,      # most permissive: auto-execute, log only
    Tier.AUTO_CAPPED: 3,   # auto-execute with resource caps
    Tier.APPROVE_ONCE: 2,  # interactive once, then learn
    Tier.ALWAYS_APPROVE: 1,  # interactive every time
    Tier.DENY: 0,          # never execute
}

# Sentinel permissiveness for ALWAYS_APPROVE agent cap: no restriction.
# When an agent cap is ALWAYS_APPROVE it means "fully trusted — no ceiling."
# Using a value above all PERMISSIVENESS values ensures min() never picks the cap.
_CAP_ALWAYS_APPROVE_PERM = 999


def _cap_permissiveness(tier: Tier) -> int:
    """Return the permissiveness value for cap-combine purposes.

    ALWAYS_APPROVE cap is treated as uncapped (perm=999) so it never restricts
    a command's own tier. All other tiers use the standard PERMISSIVENESS map.
    """
    if tier == Tier.ALWAYS_APPROVE:
        return _CAP_ALWAYS_APPROVE_PERM
    return PERMISSIVENESS[tier]


# ---------------------------------------------------------------------------
# Agent cap registry
# ---------------------------------------------------------------------------

DEFAULT_AGENT_CAP = Tier.AUTO_CAPPED

KNOWN_AGENT_CAPS: dict[str, Tier] = {
    "primary": Tier.DENY,
    "focused-shell-runner": Tier.APPROVE_ONCE,
    # Domain-specialized agents that need full access for their core function
    "focused-ghc-ci-analyzer": Tier.ALWAYS_APPROVE,
    "focused-deployment-manager": Tier.ALWAYS_APPROVE,
    "focused-project-generator": Tier.ALWAYS_APPROVE,
    "focused-patch-generator": Tier.ALWAYS_APPROVE,
    "comprehensive-mcp-generator": Tier.ALWAYS_APPROVE,
    "comprehensive-conda-ci-orchestrator": Tier.ALWAYS_APPROVE,
    # Code modification agents
    "focused-code-modifier": Tier.AUTO_CAPPED,
    # META agents
    "META-agent-creator-master": Tier.ALWAYS_APPROVE,
    "META-agent-manager-master": Tier.ALWAYS_APPROVE,
}


def lookup_agent_cap(agent_id: str | None) -> Tier:
    """Return the tier cap for the given agent_id.

    None, empty string, or "primary" all resolve to Tier.DENY.
    Unrecognized agent IDs fall back to DEFAULT_AGENT_CAP.
    """
    if not agent_id or agent_id == "primary":
        return Tier.DENY
    return KNOWN_AGENT_CAPS.get(agent_id, DEFAULT_AGENT_CAP)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SegmentClassification:
    """Classification result for a single pipeline segment."""

    segment: Segment
    tier: Tier
    matched_rule: Rule | None
    decision_step: str  # "T0_raw" | "T0_template" | "T4" | "T2" | "T1" | "T3_fallthrough"


@dataclass(frozen=True)
class ClassificationResult:
    """Final classification result including agent cap application."""

    tier: Tier  # effective tier after agent cap
    command_tier: Tier  # tier from segments alone (before cap)
    agent_cap: Tier
    matched_rule: Rule | None  # rule from the deciding (lowest-permissiveness) segment
    template: str  # normalized template
    segments: tuple[SegmentClassification, ...]
    normalizer_warnings: tuple[str, ...]
    decision_path: tuple[str, ...]


# ---------------------------------------------------------------------------
# Segment classifier (internal)
# ---------------------------------------------------------------------------


def _classify_template(template: str) -> tuple[Tier, Rule | None, str]:
    """Evaluate a normalized template against catalog rules (T0-template, T4, T2, T1).

    Returns (tier, matched_rule, decision_step).
    Evaluation order: T0 template -> T4 -> T2 -> T1 -> T3 fallthrough.
    """
    # T0 template-target (catches normalizer markers like <subshell_exec_unsafe>)
    for rule in T0_DENY:
        if rule.match_target == "template" and compiled_for(rule).search(template):
            return Tier.DENY, rule, "T0_template"

    # T4 before T2 -- prevents T2's broad `gh api \S+` matching T4-worthy POST/DELETE
    for rule in T4_ALWAYS_APPROVE:
        if compiled_for(rule).match(template):
            return Tier.ALWAYS_APPROVE, rule, "T4"

    # T2
    for rule in T2_AUTO_CAPPED:
        if compiled_for(rule).match(template):
            return Tier.AUTO_CAPPED, rule, "T2"

    # T1
    for rule in T1_AUTO_LOG:
        if compiled_for(rule).match(template):
            return Tier.AUTO_LOG, rule, "T1"

    # No match -> T3 fall-through (unknown command; discovery entry point)
    return Tier.APPROVE_ONCE, None, "T3_fallthrough"


def _classify_segment(template: str) -> tuple[Tier, Rule | None, str]:
    """Classify a single normalized segment template.

    Extends _classify_template with:
    - T0 raw-target rules applied per-segment (catches `sudo` in pipe position)
    - xargs sub-command escalation (classifies the xargs argument as a sub-command)

    Returns (tier, matched_rule, decision_step).
    """
    # Run T0 raw-target rules against the segment template.
    # Each segment is an independent command unit, so `^\s*sudo\b` etc. apply
    # to the segment's own template regardless of its position in a pipeline.
    for rule in T0_DENY:
        if rule.match_target == "raw" and compiled_for(rule).search(template):
            return Tier.DENY, rule, "T0_raw"

    # xargs sub-command escalation: `xargs <verb> [args]` -- classify the
    # sub-verb to determine the effective tier of the composed command.
    # Without this, `xargs rm` would match T1 `^xargs\s+\S+$` (too permissive).
    parts = template.split()
    if parts and parts[0] == "xargs" and len(parts) >= 2:
        sub_verb = parts[1]
        sub_tier, sub_rule, sub_step = _classify_template(sub_verb)
        if sub_tier != Tier.AUTO_LOG and sub_step != "T3_fallthrough":
            # Sub-command has a known tier that is not T1 -- use it
            return sub_tier, sub_rule, sub_step
        if sub_step == "T3_fallthrough":
            # Unknown sub-command -- escalate to T3
            return Tier.APPROVE_ONCE, None, "T3_fallthrough"

    return _classify_template(template)


# ---------------------------------------------------------------------------
# Stub segment helper for T0_raw short-circuit
# ---------------------------------------------------------------------------


def _stub_segment(command: str) -> Segment:
    """Create a minimal Segment when no normalized segments are available."""
    verb = command.strip().split()[0] if command.strip() else ""
    return Segment(
        template=command,
        verb=verb,
        operator_to_next=None,
        is_subshell=False,
        is_background=False,
    )


# ---------------------------------------------------------------------------
# Public classify function
# ---------------------------------------------------------------------------


def classify(
    command: str,
    cwd: str,
    agent_id: str | None,
    *,
    env: dict[str, str] | None = None,
) -> ClassificationResult:
    """Classify a shell command and return a ClassificationResult.

    Steps:
      1. Normalize the command.
      2. Resolve agent cap.
      3. T0 raw scan -- short-circuit if matched.
      4. Classify each segment.
      5. Pipe-combine (least permissive segment wins).
      6. Apply agent cap (least permissive of command_tier and cap).
         ALWAYS_APPROVE cap is treated as uncapped (no restriction).
    """
    decision_path: list[str] = []

    # Step 1: Normalize
    normalized = normalize(command, cwd, env=env)
    decision_path.append(f"normalized: {normalized.template!r}")

    # Step 2: Resolve agent cap
    agent_cap = lookup_agent_cap(agent_id)
    decision_path.append(f"agent_cap({agent_id!r}) = {agent_cap.name}")

    # Step 3: T0 raw scan -- short-circuit on first match
    for t0_rule in T0_DENY:
        if t0_rule.match_target == "raw" and compiled_for(t0_rule).search(command):
            decision_path.append(f"T0_raw match: {t0_rule.pattern!r}")
            stub = normalized.segments[0] if normalized.segments else _stub_segment(command)
            return ClassificationResult(
                tier=Tier.DENY,
                command_tier=Tier.DENY,
                agent_cap=agent_cap,
                matched_rule=t0_rule,
                template=normalized.template,
                segments=(
                    SegmentClassification(
                        segment=stub,
                        tier=Tier.DENY,
                        matched_rule=t0_rule,
                        decision_step="T0_raw",
                    ),
                ),
                normalizer_warnings=normalized.parse_warnings,
                decision_path=tuple(decision_path),
            )

    # Step 4: Per-segment classification
    seg_classifications: list[SegmentClassification] = []
    for seg in normalized.segments:
        tier, rule, step = _classify_segment(seg.template)
        seg_classifications.append(
            SegmentClassification(segment=seg, tier=tier, matched_rule=rule, decision_step=step)
        )
        decision_path.append(f"segment {seg.verb!r}: {step} -> {tier.name}")

    # Step 5: Pipe-combine -- least permissive segment wins
    command_tier_seg = min(seg_classifications, key=lambda s: PERMISSIVENESS[s.tier])
    command_tier = command_tier_seg.tier
    matched_rule = command_tier_seg.matched_rule
    decision_path.append(
        f"pipe combined -> {command_tier.name}"
        f" (from segment {command_tier_seg.segment.verb!r})"
    )

    # Step 6: Apply agent cap -- least permissive of (command_tier, agent_cap).
    # ALWAYS_APPROVE cap means the agent is fully trusted: the cap never restricts.
    # We use PERMISSIVENESS for the command side and _cap_permissiveness for the cap
    # side, taking the minimum to find the more restrictive of the two.
    cmd_perm = PERMISSIVENESS[command_tier]
    cap_perm = _cap_permissiveness(agent_cap)
    final_tier = command_tier if cmd_perm <= cap_perm else agent_cap
    if final_tier != command_tier:
        decision_path.append(f"agent cap escalated: {command_tier.name} -> {final_tier.name}")

    return ClassificationResult(
        tier=final_tier,
        command_tier=command_tier,
        agent_cap=agent_cap,
        matched_rule=matched_rule,
        template=normalized.template,
        segments=tuple(seg_classifications),
        normalizer_warnings=normalized.parse_warnings,
        decision_path=tuple(decision_path),
    )
