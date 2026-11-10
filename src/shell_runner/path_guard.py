"""Path guard for output redirects and recursive content search (issue #75).

The catalog collapses every redirect target to `<file_arg>` and every grep
operand to `<file_arg>`, so rules such as `^echo(\\s+\\S+)*$` and the fs-search
grep rule cannot tell `echo x > notes.txt` from `echo x > ~/.ssh/authorized_keys`,
or `grep -rn foo src` from `grep -r password ~`. This module re-reads the raw
command (via write_targets.analyze) and resolves the real paths:

  * output redirect to a credential path          -> DENY
  * output redirect outside cwd / its git repo / /tmp -> APPROVE_ONCE
  * grep/rg/ag on a credential path               -> APPROVE_ONCE
  * any write target (redirect, tee, cp, mv, sed -i, ...) inside a git directory
    -> APPROVE_ONCE and flagged (issue #84: planted core.fsmonitor/hooks run later)
  * recursive search rooted at $HOME, `/`, an ancestor of $HOME, or a directory
    that directly holds credential files          -> APPROVE_ONCE

Best-effort, like write_targets: targets built from variables or command
substitution cannot be resolved and are left to the catalog.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from pathlib import Path

from .catalog import Tier
from .executor import current_cwd_roots
from .monitor import is_credential_path
from .normalizer import _resolve_abs, classify_path
from .rules import PathMatch
from .write_targets import CommandPaths, _join_base, analyze

GIT_DIR_GLOB = "builtin:.git"
_TMP_ROOT = "/tmp"
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
    # built-in git-dir write hits: surfaced as flagged protected_path matches (audit)
    git_matches: tuple[PathMatch, ...] = ()


def _candidates(path: str, cp: CommandPaths) -> list[str]:
    """The path joined under the original cwd and every tracked cd/git -C base."""
    return [_join_base(b, path) for b in ["", *cp.bases]]


def _repo_top(cwd_real: Path) -> Path | None:
    """Nearest ancestor-or-self of cwd holding `.git`, never above a cwd root."""
    roots = current_cwd_roots()
    for d in (cwd_real, *cwd_real.parents):
        if not any(d.is_relative_to(r) for r in roots):
            return None
        if (d / ".git").exists():
            return d
    return None


def _inside_safe_area(abs_p: str, cwd: str) -> bool:
    """cwd, its enclosing git repo (within the cwd roots) or /tmp. Other cwd roots
    (~/.claude, a sibling repo) are deliberately NOT safe."""
    p = Path(abs_p)
    cwd_real = Path(os.path.realpath(cwd))
    if p.is_relative_to(cwd_real):
        return True
    if abs_p == _TMP_ROOT or abs_p.startswith(_TMP_ROOT + "/"):
        return True
    top = _repo_top(cwd_real)
    return top is not None and p.is_relative_to(top)


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


def _read_first_line(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace").splitlines()[0].strip()
    except (OSError, IndexError):
        return None


def _anchor(base: Path, raw: str) -> str:
    return os.path.join(base, raw)


def _dirs_from_dot_git(d: Path) -> list[str]:
    """Git directories named by `d/.git`: the dir itself, or for a linked worktree /
    submodule the `gitdir:` target plus its `commondir`. Read, never executed."""
    dot_git = d / ".git"
    if dot_git.is_dir():
        return [str(dot_git)]
    if not dot_git.is_file():
        return []
    line = _read_first_line(dot_git)
    if line is None or not line.startswith("gitdir:"):
        return [str(dot_git)]
    gitdir = _anchor(d, line[len("gitdir:") :].strip())
    found = [str(dot_git), gitdir]
    common = _read_first_line(Path(gitdir) / "commondir")
    if common:
        found.append(_anchor(Path(gitdir), common))
    return found


def _git_dirs_for(start: str) -> list[str]:
    """Walk up from `start` to the nearest `.git` entry (no git subprocess)."""
    real = Path(os.path.realpath(start))
    for d in (real, *real.parents):
        dirs = _dirs_from_dot_git(d)
        if dirs:
            return [p for g in dirs for p in (os.path.normpath(g), os.path.realpath(g))]
    return []


def _in_git_dir(path: str, git_dirs: list[str]) -> bool:
    p = Path(path)
    return ".git" in p.parts or any(p.is_relative_to(g) for g in git_dirs)


def _expand_home(path: str, home: str) -> str:
    if path == "~":
        return home
    if path.startswith("~/"):
        return home + path[1:]
    return home + path[5:] if path.startswith("$HOME") else path


def _git_dir_matches(cp: CommandPaths, cwd: str, home: str) -> list[PathMatch]:
    """Write targets (redirects and every verb write) that resolve into a git dir.

    A target is tested both normpath'd and realpath'd (symlinks followed); a
    hit on either counts. Unparseable/dynamic targets are skipped (never lowered).
    """
    cwd_real = os.path.realpath(cwd)
    bases = [os.path.join(cwd_real, b) if b else cwd_real for b in ["", *cp.bases]]
    git_dirs = [g for b in dict.fromkeys(bases) for g in _git_dirs_for(b)]
    found: list[PathMatch] = []
    for target in cp.writes:
        if not target or any(c in target for c in _DYNAMIC_CHARS):
            continue
        for cand in _candidates(target, cp):
            try:
                expanded = os.path.join(cwd_real, _expand_home(cand, home))
                paths = (os.path.normpath(expanded), os.path.realpath(expanded))
            except (ValueError, OSError):
                continue
            if os.path.normpath(cand) in _SAFE_DEVICES:
                continue
            hit = next((p for p in paths if _in_git_dir(p, git_dirs)), None)
            if hit is not None:
                found.append(PathMatch(hit, GIT_DIR_GLOB, "flag", "write", "builtin"))
    return found


def evaluate(command: str, cwd: str, env: dict[str, str] | None = None) -> GuardFinding | None:
    """Most restrictive finding for `command`, or None when nothing is flagged."""
    env = env or {}
    home = env.get("HOME", os.path.expanduser("~"))
    worst: GuardFinding | None = None
    git_matches: list[PathMatch] = []
    for cp in analyze(command):
        git_matches += _git_dir_matches(cp, cwd, home)
        for finding in (
            _redirect_finding(cp, cwd, env, home),
            _search_finding(cp, cwd, env, home),
        ):
            if finding is None:
                continue
            if finding.tier == Tier.DENY:
                return replace(finding, git_matches=tuple(git_matches))
            worst = worst or finding
    if not git_matches:
        return worst
    if worst is not None and worst.tier == Tier.DENY:
        return worst
    first = git_matches[0]
    return GuardFinding(
        Tier.APPROVE_ONCE,
        "git_dir_write",
        f"write into git directory ({first.path}); can plant hooks/config that git later executes",
        tuple(dict.fromkeys(git_matches)),
    )
