"""Seed catalog of shell command rules for tier classification.

Rules operate on NORMALIZED templates produced by normalizer.py, not raw commands.
Each rule is a regex matched against the normalized template form.

Tier assignment philosophy:
- T0 DENY: hardcoded, never overridable. Match against raw command (some patterns)
  AND/OR normalized template. Forkbomb and shell-injection patterns must match raw.
- T1 AUTO_LOG: read-only, sandboxable, cwd/tmp-scoped — execute without prompt, log only.
- T2 AUTO_CAPPED: low-blast writes + safe-domain network-read — execute with caps.
- T3 APPROVE_ONCE: triggered by normalizer markers (<unsafe_url>, <home_path>,
  <abs_path>, unknown verb, new flag shape) — entry point for learn loop. NOT in static catalog.
- T4 ALWAYS_APPROVE: network-write, package install, destructive — always prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum


class Tier(IntEnum):
    DENY = 0
    AUTO_LOG = 1
    AUTO_CAPPED = 2
    APPROVE_ONCE = 3  # never appears in static rules; assigned by classifier on miss
    ALWAYS_APPROVE = 4


@dataclass(frozen=True)
class Rule:
    pattern: str  # regex source string
    tier: Tier
    category: str
    match_target: str = "template"  # "template" (default) or "raw" (for T0 patterns
    # that must catch shell-injection)
    agent_scope: str | None = None  # None = all agents; or specific agent_id
    source: str = "seed"  # "seed" | "learned" | "manual"
    reason: str = ""

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.pattern)


# ---------------------------------------------------------------------------
# T0 DENY — match against RAW command
# ---------------------------------------------------------------------------

T0_DENY: list[Rule] = [
    # Destructive recursive deletes
    Rule(
        r"^\s*rm\s+-[rRf]+\s+/\s*$",
        Tier.DENY,
        "destructive",
        match_target="raw",
        reason="recursive root delete",
    ),
    Rule(r"^\s*rm\s+-[rRf]+\s+/\*", Tier.DENY, "destructive", match_target="raw"),
    Rule(
        r"^\s*rm\s+-[rRf]+\s+(\$HOME|~)\s*$",
        Tier.DENY,
        "destructive",
        match_target="raw",
    ),
    Rule(
        r"^\s*rm\s+-[rRf]+\s+/(boot|etc|usr|bin|sbin|lib|var)\b",
        Tier.DENY,
        "destructive",
        match_target="raw",
    ),
    # Block device writes
    Rule(
        r"\bdd\b.*\bof=/dev/(sd|nvme|hd|zd)",
        Tier.DENY,
        "destructive",
        match_target="raw",
    ),
    Rule(r">\s*/dev/(sd|nvme|hd)", Tier.DENY, "destructive", match_target="raw"),
    Rule(r"^\s*mkfs\.", Tier.DENY, "destructive", match_target="raw"),
    Rule(r"^\s*(fdisk|parted|sgdisk)\b", Tier.DENY, "destructive", match_target="raw"),
    # Forkbomb
    Rule(
        r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
        Tier.DENY,
        "forkbomb",
        match_target="raw",
    ),
    # Mass perms
    Rule(
        r"\bchmod\s+-[rR]+\s+777\s+/",
        Tier.DENY,
        "destructive",
        match_target="raw",
    ),
    Rule(
        r"\bchown\s+-[rR]+\s+\w+\s+/",
        Tier.DENY,
        "destructive",
        match_target="raw",
    ),
    # Privilege escalation
    Rule(r"^\s*sudo\b", Tier.DENY, "privilege", match_target="raw"),
    Rule(r"^\s*su\b", Tier.DENY, "privilege", match_target="raw"),
    Rule(r"^\s*doas\b", Tier.DENY, "privilege", match_target="raw"),
    Rule(r"^\s*pkexec\b", Tier.DENY, "privilege", match_target="raw"),
    # Pipe-exec from any source
    Rule(
        r"\|\s*(sh|bash|zsh|ksh|fish)\b",
        Tier.DENY,
        "pipe-exec",
        match_target="raw",
        reason="piped exec",
    ),
    Rule(
        r"\$\(\s*(curl|wget|fetch)\b",
        Tier.DENY,
        "subshell-exec",
        match_target="raw",
    ),
    Rule(
        r"`\s*(curl|wget|fetch)\b",
        Tier.DENY,
        "subshell-exec",
        match_target="raw",
    ),
    Rule(r"^\s*eval\s+\$\(", Tier.DENY, "subshell-exec", match_target="raw"),
    # Critical writes
    Rule(
        r">\s*/etc/(passwd|shadow|sudoers|fstab)",
        Tier.DENY,
        "destructive",
        match_target="raw",
    ),
    Rule(r">\s*/boot/", Tier.DENY, "destructive", match_target="raw"),
    # Process kill targeting init
    Rule(r"^\s*kill\s+-9?\s*-?1\b", Tier.DENY, "destructive", match_target="raw"),
    # Subshell-exec marker from normalizer (when normalized template contains the marker)
    Rule(
        r"<subshell_exec_unsafe>",
        Tier.DENY,
        "subshell-exec",
        reason="normalizer-detected curl/wget in subshell",
    ),
]

# ---------------------------------------------------------------------------
# T1 AUTO_LOG — match against TEMPLATE
# ---------------------------------------------------------------------------

T1_AUTO_LOG: list[Rule] = [
    # FS-read
    # <_path> group matches any path placeholder produced by normalizer, including
    # <file_arg> which is emitted by READ_ONLY_FILE_VERBS post-pass.
    Rule(
        r"^ls(\s+-[a-zA-Z]+)?(\s+<(cwd|tmp)_path>)*$",
        Tier.AUTO_LOG,
        "fs-read",
    ),
    Rule(r"^cat(\s+(<(cwd|tmp)_path>|<file_arg>))+$", Tier.AUTO_LOG, "fs-read"),
    Rule(
        r"^head(\s+-n\s+<n>|\s+<n>)?(\s+(<(cwd|tmp)_path>|<file_arg>))+$",
        Tier.AUTO_LOG,
        "fs-read",
    ),
    Rule(
        r"^tail(\s+-[nf]+\s+<n>)?(\s+(<(cwd|tmp)_path>|<file_arg>))+$",
        Tier.AUTO_LOG,
        "fs-read",
    ),
    Rule(
        r"^wc(\s+-[lwc]+)?(\s+(<(cwd|tmp)_path>|<file_arg>))+$",
        Tier.AUTO_LOG,
        "fs-read",
    ),
    Rule(r"^stat(\s+<(cwd|tmp)_path>)+$", Tier.AUTO_LOG, "fs-read"),
    Rule(r"^file(\s+(<(cwd|tmp)_path>|<file_arg>))+$", Tier.AUTO_LOG, "fs-read"),
    Rule(
        r"^du(\s+-[sh]+)?(\s+<(cwd|tmp)_path>)?$",
        Tier.AUTO_LOG,
        "fs-read",
    ),
    Rule(r"^df(\s+-h)?$", Tier.AUTO_LOG, "fs-read"),
    # FS-search
    Rule(
        r"^find\s+(<(cwd|tmp)_path>|<file_arg>)(\s+-(name|type|iname|maxdepth)\s+(<arg>|<n>|\w+))*$",
        Tier.AUTO_LOG,
        "fs-search",
    ),
    Rule(
        r"^grep(\s+-[a-zA-Z]+)?\s+<arg>(\s+(<(cwd|tmp)_path>|<file_arg>))+$",
        Tier.AUTO_LOG,
        "fs-search",
    ),
    Rule(
        r"^rg(\s+-[a-zA-Z]+)?\s+<arg>(\s+<(cwd|tmp)_path>)?$",
        Tier.AUTO_LOG,
        "fs-search",
    ),
    Rule(r"^which\s+\w+$", Tier.AUTO_LOG, "fs-search"),
    Rule(r"^whereis\s+\w+$", Tier.AUTO_LOG, "fs-search"),
    Rule(r"^type\s+\w+$", Tier.AUTO_LOG, "fs-search"),
    # System info
    Rule(r"^pwd$", Tier.AUTO_LOG, "system-info"),
    Rule(r"^whoami$", Tier.AUTO_LOG, "system-info"),
    Rule(r"^hostname$", Tier.AUTO_LOG, "system-info"),
    Rule(r"^uname(\s+-[ar]+)?$", Tier.AUTO_LOG, "system-info"),
    Rule(r"^date(\s+\+<arg>)?$", Tier.AUTO_LOG, "system-info"),
    Rule(r"^ps(\s+aux)?$", Tier.AUTO_LOG, "system-info"),
    Rule(r"^echo(\s+\S+)*$", Tier.AUTO_LOG, "system-info"),
    Rule(r"^sleep\s+<n>$", Tier.AUTO_LOG, "process"),
    Rule(r"^env$", Tier.AUTO_LOG, "system-info"),
    Rule(r"^printenv(\s+\w+)?$", Tier.AUTO_LOG, "system-info"),
    # Pure pipeline (no fs-write — sed -i is T2)
    Rule(
        r"^awk\s+<arg>(\s+(<(cwd|tmp)_path>|<file_arg>))?$",
        Tier.AUTO_LOG,
        "text-transform",
    ),
    Rule(
        r"^sed(\s+-e\s+<arg>)+(\s+(<(cwd|tmp)_path>|<file_arg>))?$",
        Tier.AUTO_LOG,
        "text-transform",
    ),
    Rule(
        r"^sed\s+<arg>(\s+(<(cwd|tmp)_path>|<file_arg>))?$",
        Tier.AUTO_LOG,
        "text-transform",
    ),
    Rule(
        r"^sort(\s+-[krnu]+)?(\s+(<(cwd|tmp)_path>|<file_arg>))?$",
        Tier.AUTO_LOG,
        "text-transform",
    ),
    Rule(r"^uniq(\s+-c)?$", Tier.AUTO_LOG, "text-transform"),
    Rule(
        r"^cut\s+-[df]\s+\S+(\s+(<(cwd|tmp)_path>|<file_arg>))?$",
        Tier.AUTO_LOG,
        "text-transform",
    ),
    Rule(r"^tr\s+<arg>\s+<arg>$", Tier.AUTO_LOG, "text-transform"),
    Rule(
        r"^jq\s+<arg>(\s+(<(cwd|tmp)_path>|<file_arg>))?$",
        Tier.AUTO_LOG,
        "text-transform",
    ),
    Rule(r"^tee\s+/dev/null$", Tier.AUTO_LOG, "text-transform"),
    Rule(r"^xargs\s+\S+$", Tier.AUTO_LOG, "text-transform"),
    # Pre-collapsed safe pipe filter (normalizer collapsed the segment to <safe_pipe>)
    Rule(
        r"^<safe_pipe>$",
        Tier.AUTO_LOG,
        "text-transform",
        match_target="template",
        reason="Pre-collapsed safelisted pipe filter (head/tail/grep/wc/awk/sed/cut/sort/uniq/jq/tr/less/more/cat) — read-only by construction",
    ),
]

# ---------------------------------------------------------------------------
# T2 AUTO_CAPPED — match against TEMPLATE
# ---------------------------------------------------------------------------

T2_AUTO_CAPPED: list[Rule] = [
    # FS-write (cwd/tmp only)
    Rule(
        r"^mkdir(\s+-p)?\s+<(cwd|tmp)_path>(\s+<(cwd|tmp)_path>)*$",
        Tier.AUTO_CAPPED,
        "fs-write",
    ),
    Rule(r"^touch(\s+<(cwd|tmp)_path>)+$", Tier.AUTO_CAPPED, "fs-write"),
    Rule(r"^chmod\s+\+x\s+<(cwd|tmp)_path>$", Tier.AUTO_CAPPED, "fs-write"),
    Rule(
        r"^chmod\s+0?[0-7]{3}\s+<(cwd|tmp)_path>$",
        Tier.AUTO_CAPPED,
        "fs-write",
    ),
    Rule(
        r"^mv\s+<(cwd|tmp)_path>\s+<(cwd|tmp)_path>$",
        Tier.AUTO_CAPPED,
        "fs-write",
    ),
    Rule(
        r"^cp(\s+-r)?\s+<(cwd|tmp)_path>\s+<(cwd|tmp)_path>$",
        Tier.AUTO_CAPPED,
        "fs-write",
    ),
    Rule(
        r"^ln\s+-s\s+<(cwd|tmp)_path>\s+<(cwd|tmp)_path>$",
        Tier.AUTO_CAPPED,
        "fs-write",
    ),
    Rule(
        r"^sed\s+-i(\.\w+)?\s+<arg>\s+<(cwd|tmp)_path>$",
        Tier.AUTO_CAPPED,
        "fs-write",
    ),
    # Network-read (safe domains only — placeholder <safe_url>)
    Rule(
        r"^curl(\s+-[sLfk]+)*(\s+(GET|HEAD))?\s+<safe_url>$",
        Tier.AUTO_CAPPED,
        "network-read",
    ),
    Rule(r"^wget\s+-q\s+<safe_url>$", Tier.AUTO_CAPPED, "network-read"),
    # Safe git read-ops
    Rule(
        r"^git\s+(status|log|diff|show|fetch|branch|tag|remote|rev-parse|describe|stash)(\s+\S+)*$",
        Tier.AUTO_CAPPED,
        "git-read",
    ),
    Rule(r"^git\s+config\s+--get\s+\S+$", Tier.AUTO_CAPPED, "git-read"),
    # Safe gh CLI read-ops
    Rule(
        r"^gh\s+(pr|issue|run|workflow|repo)\s+(view|list|status)(\s+\S+)*$",
        Tier.AUTO_CAPPED,
        "git-read",
    ),
    Rule(r"^gh\s+api\s+\S+$", Tier.AUTO_CAPPED, "git-read"),
    Rule(r"^gh\s+auth\s+status$", Tier.AUTO_CAPPED, "git-read"),
    # Pipe-segment composites (left side already T1, but aggregated pipe forms commonly used)
    # These are not strictly necessary if classifier evaluates segment-by-segment;
    # included as redundancy for common patterns.
    Rule(
        r"^cat\s+(<(cwd|tmp)_path>|<file_arg>)\s*\|\s*jq\s+<arg>$",
        Tier.AUTO_CAPPED,
        "fs-read",
    ),
]

# ---------------------------------------------------------------------------
# T4 ALWAYS_APPROVE — match against TEMPLATE
# ---------------------------------------------------------------------------

T4_ALWAYS_APPROVE: list[Rule] = [
    # Network-write
    Rule(
        r"^curl\s+(-X\s+(POST|PUT|DELETE|PATCH)\b|--data\b|-d\s+)",
        Tier.ALWAYS_APPROVE,
        "network-write",
    ),
    Rule(r"^wget\s+--post-", Tier.ALWAYS_APPROVE, "network-write"),
    Rule(
        r"^gh\s+api\s+-X\s+(POST|PUT|DELETE|PATCH)\b",
        Tier.ALWAYS_APPROVE,
        "network-write",
    ),
    Rule(
        r"^gh\s+(pr|issue)\s+(create|close|merge|edit|reopen)\b",
        Tier.ALWAYS_APPROVE,
        "network-write",
    ),
    Rule(
        r"^gh\s+workflow\s+(run|enable|disable)\b",
        Tier.ALWAYS_APPROVE,
        "network-write",
    ),
    Rule(
        r"^gh\s+release\s+(create|delete|edit)\b",
        Tier.ALWAYS_APPROVE,
        "network-write",
    ),
    Rule(r"^git\s+push\b", Tier.ALWAYS_APPROVE, "git-write"),
    Rule(
        r"^git\s+(commit|merge|rebase|reset|cherry-pick|revert)\b",
        Tier.ALWAYS_APPROVE,
        "git-write",
    ),
    # Package management
    Rule(
        r"^(pip|pip3)\s+(install|uninstall|upgrade)\b",
        Tier.ALWAYS_APPROVE,
        "package-mgmt",
    ),
    Rule(
        r"^npm\s+(install|uninstall|publish|update)\b",
        Tier.ALWAYS_APPROVE,
        "package-mgmt",
    ),
    Rule(
        r"^(apt|apt-get|yum|dnf)\s+(install|remove|update|upgrade)\b",
        Tier.ALWAYS_APPROVE,
        "package-mgmt",
    ),
    Rule(
        r"^brew\s+(install|uninstall|tap|upgrade)\b",
        Tier.ALWAYS_APPROVE,
        "package-mgmt",
    ),
    Rule(r"^pixi\s+(add|remove|update)\b", Tier.ALWAYS_APPROVE, "package-mgmt"),
    # Network ops
    Rule(r"^ssh\b", Tier.ALWAYS_APPROVE, "network-write"),
    Rule(r"^scp\b", Tier.ALWAYS_APPROVE, "network-write"),
    Rule(r"^rsync\b", Tier.ALWAYS_APPROVE, "network-write"),
    Rule(r"^(nc|ncat)\b", Tier.ALWAYS_APPROVE, "network-write"),
    Rule(r"^ssh-keygen\b", Tier.ALWAYS_APPROVE, "network-write"),
    # Destructive (allowed but always prompt — not in T0)
    Rule(r"^rm\b", Tier.ALWAYS_APPROVE, "fs-write"),
    Rule(
        r"^chmod\s+\+x\s+<(home|etc|abs)_path>$",
        Tier.ALWAYS_APPROVE,
        "fs-write",
    ),
    Rule(
        r"^chmod\s+0?[0-7]{3}\s+<(home|etc|abs)_path>$",
        Tier.ALWAYS_APPROVE,
        "fs-write",
    ),
    Rule(r"^chown\s+\w+\s+\S+$", Tier.ALWAYS_APPROVE, "fs-write"),
    Rule(r"^kill(\s+-9)?\s+<n>$", Tier.ALWAYS_APPROVE, "process"),
    # Process / code exec
    Rule(r"^bash\s+-c\b", Tier.ALWAYS_APPROVE, "process"),
    Rule(r"^sh\s+-c\b", Tier.ALWAYS_APPROVE, "process"),
    Rule(r"^python3?\s+-c\b", Tier.ALWAYS_APPROVE, "process"),
    Rule(r"^source\s+\S+$", Tier.ALWAYS_APPROVE, "process"),
]

# ---------------------------------------------------------------------------
# Aggregation + helper functions
# ---------------------------------------------------------------------------


def all_rules() -> list[Rule]:
    """All seed rules in classification order (T0 first to short-circuit)."""
    return T0_DENY + T1_AUTO_LOG + T2_AUTO_CAPPED + T4_ALWAYS_APPROVE


def rules_by_tier(tier: Tier) -> list[Rule]:
    return [r for r in all_rules() if r.tier == tier]


def rules_by_category(category: str) -> list[Rule]:
    return [r for r in all_rules() if r.category == category]


# Module-level pre-compiled lookup for performance
_COMPILED_BY_RULE: dict[int, re.Pattern[str]] = {}


def compiled_for(rule: Rule) -> re.Pattern[str]:
    rid = id(rule)
    if rid not in _COMPILED_BY_RULE:
        _COMPILED_BY_RULE[rid] = re.compile(rule.pattern)
    return _COMPILED_BY_RULE[rid]
