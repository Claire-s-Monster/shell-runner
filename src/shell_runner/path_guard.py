"""Path guard for output redirects and recursive content search (issue #75).

The catalog collapses every redirect target to `<file_arg>` and every grep
operand to `<file_arg>`, so rules such as `^echo(\\s+\\S+)*$` and the fs-search
grep rule cannot tell `echo x > notes.txt` from `echo x > ~/.ssh/authorized_keys`,
or `grep -rn foo src` from `grep -r password ~`. This module re-reads the raw
command (via write_targets.analyze) and resolves the real paths:

  * output redirect to a credential path          -> DENY
  * output redirect outside cwd, cwd roots, /tmp  -> APPROVE_ONCE
  * grep/rg/ag on a credential path               -> APPROVE_ONCE
  * recursive search rooted at $HOME, `/`, an ancestor of $HOME, or a directory
    that directly holds credential files          -> APPROVE_ONCE

Best-effort, like write_targets: targets built from variables or command
substitution cannot be resolved and are left to the catalog.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .catalog import Tier
from .executor import current_cwd_roots
from .monitor import is_credential_path
from .normalizer import _resolve_abs, classify_path
from .write_targets import CommandPaths, _join_base, analyze

_SAFE_DEVICES = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})
_SEARCH_VERBS = frozenset({"grep", "egrep", "fgrep", "rgrep", "rg", "ag"})
_ALWAYS_RECURSIVE = frozenset({"rg", "ag"})
_RECURSIVE_SHORT_RE = re.compile(r"^-[A-Za-z]*[rR]")
_DYNAMIC_CHARS = "$`*?[{"
_PATTERN_OPTS = frozenset({"-e", "-f", "--regexp", "--file"})


@dataclass(frozen=True)
class GuardFinding:
    tier: Tier
    category: str
    reason: str


def _candidates(path: str, cp: CommandPaths) -> list[str]:
    """The path joined under the original cwd and every tracked cd/git -C base."""
    return [_join_base(b, path) for b in ["", *cp.bases]]


def _inside_safe_area(abs_p: str, cwd: str) -> bool:
    p = Path(abs_p)
    if p.is_relative_to(os.path.realpath(cwd)):
        return True
    if abs_p == "/tmp" or abs_p.startswith("/tmp/"):
        return True
    return any(p.is_relative_to(r) for r in current_cwd_roots())


def _redirect_tier(path: str, cwd: str, env: dict[str, str], home: str) -> Tier | None:
    if os.path.normpath(path) in _SAFE_DEVICES:
        return None
    if classify_path(path, cwd, env) == "<cred_path>":
        return Tier.DENY
    abs_p = _resolve_abs(path, cwd, home)
    return None if _inside_safe_area(abs_p, cwd) else Tier.APPROVE_ONCE


def _redirect_finding(
    cp: CommandPaths, cwd: str, env: dict[str, str], home: str
) -> GuardFinding | None:
    worst: GuardFinding | None = None
    for target in cp.redirects:
        if any(c in target for c in _DYNAMIC_CHARS):
            continue
        for cand in _candidates(target, cp):
            tier = _redirect_tier(cand, cwd, env, home)
            if tier is None:
                continue
            if tier == Tier.DENY:
                return GuardFinding(
                    Tier.DENY, "redirect_credential", f"output redirect to credential path {target}"
                )
            worst = GuardFinding(
                tier, "redirect_outside_cwd", f"output redirect to {target} outside cwd/tmp roots"
            )
    return worst


def _is_recursive(cp: CommandPaths) -> bool:
    if cp.verb in _ALWAYS_RECURSIVE:
        return True
    return any(
        o in ("--recursive", "--dereference-recursive")
        or (not o.startswith("--") and _RECURSIVE_SHORT_RE.match(o))
        for o in cp.options
    )


def _holds_credentials(p: Path) -> bool:
    try:
        return p.is_dir() and any(is_credential_path(c) for c in p.iterdir())
    except OSError:
        return False


def _broad_search_root(abs_p: str, cwd: str, home: str) -> bool:
    """True for $HOME, `/`, an ancestor of $HOME, or a dir directly holding credentials."""
    p = Path(abs_p)
    home_p = Path(os.path.realpath(home))
    if p == home_p or p == Path("/") or p in home_p.parents:
        return True
    return not _inside_safe_area(abs_p, cwd) and _holds_credentials(p)


def _search_finding(
    cp: CommandPaths, cwd: str, env: dict[str, str], home: str
) -> GuardFinding | None:
    if cp.verb not in _SEARCH_VERBS:
        return None
    has_pattern_opt = any(o in _PATTERN_OPTS for o in cp.options)
    operands = cp.args if has_pattern_opt else cp.args[1:]
    recursive = _is_recursive(cp)
    for operand in operands:
        if any(c in operand for c in _DYNAMIC_CHARS):
            continue
        for cand in _candidates(operand, cp):
            if classify_path(cand, cwd, env) == "<cred_path>":
                return GuardFinding(
                    Tier.APPROVE_ONCE,
                    "search_credential",
                    f"{cp.verb} on credential path {operand}",
                )
            if recursive and _broad_search_root(_resolve_abs(cand, cwd, home), cwd, home):
                return GuardFinding(
                    Tier.APPROVE_ONCE,
                    "recursive_search_broad",
                    f"recursive {cp.verb} rooted at {operand} (home, /, or credential parent)",
                )
    return None


def evaluate(command: str, cwd: str, env: dict[str, str] | None = None) -> GuardFinding | None:
    """Most restrictive finding for `command`, or None when nothing is flagged."""
    env = env or {}
    home = env.get("HOME", os.path.expanduser("~"))
    worst: GuardFinding | None = None
    for cp in analyze(command):
        for finding in (
            _redirect_finding(cp, cwd, env, home),
            _search_finding(cp, cwd, env, home),
        ):
            if finding is None:
                continue
            if finding.tier == Tier.DENY:
                return finding
            worst = worst or finding
    return worst
