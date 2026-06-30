"""Default seed approvals.

These templates represent common read-only / low-risk command shapes that are
inserted into template_approvals at T2 (AUTO_CAPPED) on startup, so a fresh
shell-runner deployment doesn't burn one approval per command shape the user
hits in their workflow.

User-promoted approvals are NEVER overwritten by this list — see
Persistence.seed_approvals().

To extend the list, add a new SeedApproval entry and restart shell-runner.
To opt OUT entirely, set SHELL_RUNNER_LOAD_SEED_APPROVALS=0.
"""

from __future__ import annotations

from dataclasses import dataclass

from .catalog import Tier


@dataclass(frozen=True)
class SeedApproval:
    template: str
    tier: Tier
    category: str
    reason: str


# Curated list. Keep tight — each entry widens the auto-execute surface.
DEFAULT_SEED_APPROVALS: tuple[SeedApproval, ...] = (
    # ---- HTTP fetch shapes (safe-domain only) ----
    SeedApproval(
        template="curl <safe_url>",
        tier=Tier.AUTO_CAPPED,
        category="http_read",
        reason="Bare curl to safe-domain URL",
    ),
    SeedApproval(
        template="curl <safe_url> <file_arg>",
        tier=Tier.AUTO_CAPPED,
        category="http_read",
        reason="Download to file from safe domain (-o destination)",
    ),
    SeedApproval(
        template="curl <safe_url> <file_arg> && wc <file_arg>",
        tier=Tier.AUTO_CAPPED,
        category="http_read",
        reason="Download + line count",
    ),
    SeedApproval(
        template="curl <safe_url> > <file_arg>",
        tier=Tier.AUTO_CAPPED,
        category="http_read",
        reason="Download via shell redirect",
    ),
    SeedApproval(
        template="curl <safe_url> > <file_arg> && wc <file_arg>",
        tier=Tier.AUTO_CAPPED,
        category="http_read",
        reason="Download via redirect + line count",
    ),
    SeedApproval(
        template="curl <safe_url> | <safe_pipe>",
        tier=Tier.AUTO_CAPPED,
        category="http_read",
        reason="Stream curl output through one safe filter",
    ),
    SeedApproval(
        template="curl <safe_url> | <safe_pipe> | <safe_pipe>",
        tier=Tier.AUTO_CAPPED,
        category="http_read",
        reason="Stream curl output through two safe filters",
    ),
    SeedApproval(
        template="curl <safe_url> | <safe_pipe> | <safe_pipe> | <safe_pipe>",
        tier=Tier.AUTO_CAPPED,
        category="http_read",
        reason="Stream curl output through three safe filters",
    ),
    SeedApproval(
        template="curl <safe_url> <arg>",
        tier=Tier.AUTO_CAPPED,
        category="http_read",
        reason="curl with one positional arg (e.g. -H value collapsed to <arg>)",
    ),
    SeedApproval(
        template="curl <safe_url> <arg> | <safe_pipe>",
        tier=Tier.AUTO_CAPPED,
        category="http_read",
        reason="curl with arg + one filter",
    ),
    SeedApproval(
        template="curl <safe_url> <arg> | <safe_pipe> | <safe_pipe>",
        tier=Tier.AUTO_CAPPED,
        category="http_read",
        reason="curl with arg + two filters",
    ),
)
