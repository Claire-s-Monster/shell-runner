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
import shlex
from dataclasses import dataclass, replace
from pathlib import Path

from . import exec_guard, rules, worktree_git
from .catalog import Tier
from .executor import current_cwd_roots
from .monitor import is_credential_path
from .normalizer import _resolve_abs, classify_path
from .rules import PathMatch
from .shellprep import SENTINEL_PREFIX
from .write_targets import CommandPaths, _join_base, _split, _tokenize, analyze

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
    # False: raise the tier only; persisted template/verb approvals may still apply
    guarded: bool = True


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
    structural = [str(d) for d in (real, *real.parents) if _is_git_structure(d)]
    for d in (real, *real.parents):
        dirs = _dirs_from_dot_git(d)
        if dirs:
            found = [*dirs, *structural]
            return [p for g in found for p in (os.path.normpath(g), os.path.realpath(g))]
    return [p for g in structural for p in (os.path.normpath(g), os.path.realpath(g))]


def _is_git_structure(d: Path) -> bool:
    """A bare repo / separate git dir signature: HEAD file + objects/ + refs/."""
    try:
        return (d / "HEAD").is_file() and (d / "objects").is_dir() and (d / "refs").is_dir()
    except OSError:
        return False


def _in_git_dir(path: str, git_dirs: list[str]) -> bool:
    p = Path(path)
    if ".git" in p.parts or any(p.is_relative_to(g) for g in git_dirs):
        return True
    return any(_is_git_structure(a) for a in (p, *p.parents))


def _expand_home(path: str, home: str) -> str:
    if path == "~":
        return home
    if path.startswith("~/"):
        return home + path[1:]
    return home + path[5:] if path.startswith("$HOME") else path


_DYNAMIC_PIECE_RE = re.compile(
    r"(\$\{[^}]*\}?|\$\([^)]*\)?|\$\w*|`[^`]*`?|\{[^}]*\}?|\[[^\]]*\]?|[*?])"
)
_PURE_VAR_RE = re.compile(r"\$\w+|\$\{\w+\}|\$\([^)]*\)|`[^`]*`")


def _is_dynamic(target: str) -> bool:
    return any(c in target for c in _DYNAMIC_CHARS)


def _component_regex(component: str) -> re.Pattern[str]:
    """Over-approximate regex for a shell word: every expansion/glob piece is `.*`."""
    parts = _DYNAMIC_PIECE_RE.split(component)
    return re.compile("".join(".*" if i % 2 else re.escape(p) for i, p in enumerate(parts)))


def _could_be_dot_git(target: str) -> bool:
    """True when an unresolvable target may still name a `.git` component.

    A literal `.git` component counts; so does a component whose globs/expansions
    could spell `.git`. A leading pure `$VAR` / `$(..)` / backtick component followed
    by more path (`$TMPDIR/f`) is taken as a base directory, not as a `.git`
    component; a target that is nothing but an expansion (`$F`) could be anything.
    """
    comps = [c for c in target.split("/") if c]
    for i, c in enumerate(comps):
        if c == ".git":
            return True
        if not _is_dynamic(c):
            continue
        if i == 0 and len(comps) > 1 and _PURE_VAR_RE.fullmatch(c):
            continue
        if _component_regex(c).fullmatch(".git"):
            return True
    return False


def _unresolved_targets(cp: CommandPaths) -> list[str]:
    return [t for t in dict.fromkeys(cp.writes) if t and _is_dynamic(t)]


_STRICTNESS = {
    Tier.DENY: 0,
    Tier.ALWAYS_APPROVE: 1,
    Tier.APPROVE_ONCE: 2,
    Tier.AUTO_CAPPED: 3,
    Tier.AUTO_LOG: 4,
}


def _unresolved_finding(cp: CommandPaths) -> GuardFinding | None:
    """A write target built from `$`, backticks, globs or braces cannot be resolved:
    fail closed to T3, approval-proof (the template is shared with resolvable variants)."""
    targets = _unresolved_targets(cp)
    if not targets:
        return None
    return GuardFinding(
        Tier.APPROVE_ONCE,
        "unresolved_write_target",
        f"write target {targets[0]!r} cannot be resolved statically",
    )


def _exec_finding(cp: CommandPaths) -> GuardFinding | None:
    """awk/sed/git forms that run code or write files (issue #85). Hazard findings are
    approval-proof (the template is shared with the safe forms); git remote/branch/tag/
    stash writes are ordinary approvable T3s (verdict.guarded is False)."""
    verdict = exec_guard.check(cp)
    if verdict is None:
        return None
    tier, category, reason, guarded = verdict
    return GuardFinding(tier, category, reason, guarded=guarded)


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
        if not target:
            continue
        if _is_dynamic(target):
            if _could_be_dot_git(target):
                found.append(PathMatch(target, GIT_DIR_GLOB, "flag", "write", "builtin"))
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


def _bare_repo_finding(cp: CommandPaths, cwd: str) -> GuardFinding | None:
    """git run where its repo dir (cwd, after cd / `git -C` bases) has no `.git` component
    but sits inside a structural git dir (HEAD + objects/ + refs/): git can discover it as
    a bare repo and run config planted before the structure existed. Every subcommand is
    guarded T3 (narrow: agents rarely work in bare repos)."""
    if cp.verb != "git":
        return None
    cwd_real = os.path.realpath(cwd)
    for base in dict.fromkeys(
        os.path.join(cwd_real, b) if b else cwd_real for b in ["", *cp.bases]
    ):
        for repo_dir in (os.path.normpath(base), os.path.realpath(base)):
            p = Path(repo_dir)
            if ".git" not in p.parts and any(_is_git_structure(a) for a in (p, *p.parents)):
                return GuardFinding(
                    Tier.APPROVE_ONCE,
                    "git_in_bare_repo",
                    f"git run inside a bare/structural git directory ({repo_dir})",
                )
    return None


def _unparseable_finding(cps: list[CommandPaths], segment_count: int | None) -> GuardFinding | None:
    """The command could not be tokenized, or write_targets and the normalizer disagree on
    how many commands it holds: neither view can be trusted (fail closed, approval-proof)."""
    if any(cp.unparseable for cp in cps):
        reason = "command could not be parsed (unbalanced quote/heredoc)"
    elif segment_count is not None and len(cps) != segment_count:
        reason = f"segment count mismatch ({len(cps)} vs {segment_count} in the normalizer)"
    else:
        return None
    return GuardFinding(Tier.APPROVE_ONCE, "unparseable", reason)


def evaluate(
    command: str,
    cwd: str,
    env: dict[str, str] | None = None,
    *,
    segment_count: int | None = None,
    folded_command: str | None = None,
) -> GuardFinding | None:
    """Most restrictive finding for `command`, or None when nothing is flagged.

    `segment_count` is the number of simple commands the normalizer found, in
    `folded_command` when it folded a leading `cd DIR &&` away; a mismatch with
    write_targets' count is an `unparseable` finding."""
    env = env or {}
    home = env.get("HOME", os.path.expanduser("~"))
    findings: list[GuardFinding] = []
    git_matches: list[PathMatch] = []
    cps = analyze(command)
    counted = analyze(folded_command) if folded_command else cps
    unparseable = _unparseable_finding(counted, segment_count)
    if unparseable is not None:
        findings.append(unparseable)
    for cp in cps:
        git_matches += _git_dir_matches(cp, cwd, home)
        findings += [
            f
            for f in (
                _redirect_finding(cp, cwd, env, home),
                _search_finding(cp, cwd, env, home),
                _unresolved_finding(cp),
                _exec_finding(cp),
                _bare_repo_finding(cp, cwd),
                _worktree_finding(cp, command, cwd, env, sole=len(cps) == 1),
                _mkdir_finding(cp, cwd, home),
            )
            if f is not None
        ]
    if git_matches:
        first = git_matches[0]
        findings.insert(
            0,
            GuardFinding(
                Tier.APPROVE_ONCE,
                "git_dir_write",
                f"write into git directory ({first.path}); can plant hooks/config that "
                "git later executes",
            ),
        )
    if not findings:
        return None
    # strictest tier first; at a tie a guarded (approval-proof) finding beats an unguarded one
    best = min(findings, key=lambda f: (_STRICTNESS[f.tier], not f.guarded))
    return replace(best, git_matches=tuple(dict.fromkeys(git_matches)))


# ---------------------------------------------------------------------------
# git worktree add / remove
# ---------------------------------------------------------------------------
#
# `git worktree add|remove` is T2 only when ALL of these hold (otherwise it keeps its
# catalog tier, T3 or higher):
#   * the whole command is exactly one plain `git worktree add|remove ...` (no `-C`,
#     cd, env prefix, pipe, `;`/`&&`, redirect, variable, glob or substitution)
#   * only these options: add -b <branch>, --detach/-d, -q/--quiet, --checkout,
#     --no-checkout, --track, --no-track; remove takes none. Never --force/-f/--lock.
#   * the target, resolved against the real cwd both lexically (`..` collapsed) and
#     through symlinks, is a strict descendant of an allowed `worktrees/` root, which is
#     derived from the enclosing repo top (nearest `.git` within the cwd roots, found
#     without running git):
#       <repo parent>/worktrees      e.g. <project>/development -> <project>/worktrees
#       <repo>/.claude/worktrees
#       the cwd's own parent when it is itself a `worktrees/` dir (cwd is a worktree)
#   * and it is not $HOME, a credential path, a system path, or any path matching a
#     protected-path rule (a rules config error also fails the check).
# Any `git worktree add|remove` that is not allowed is marked path-guarded (T3 floor,
# no persisted template/verb approval can lower it): the shared template
# `git worktree add <cwd_path>` covers allowed and disallowed targets alike.

_WT_SAFE_FLAGS = frozenset(
    {"--detach", "-d", "-q", "--quiet", "--checkout", "--no-checkout", "--track", "--no-track"}
)
_WT_BRANCH_OPTS = frozenset({"-b"})
_WT_META_CHARS = frozenset("$`;&|<>()*?[]{}!~\\\n\r#")
_WT_SYSTEM_PREFIXES = (
    "/etc",
    "/usr",
    "/var",
    "/boot",
    "/root",
    "/proc",
    "/sys",
    "/dev",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/opt",
)


<<<<<<< HEAD
def _plain_words(command: str) -> list[str] | None:
    """Words of `command` when, after the shared shellprep preprocessing, it is exactly one
    simple command with no redirect (so no cd, env prefix, pipe, list or hidden substitution)."""
    tokens = _tokenize(command)
    if tokens is None:
        return None
    segments = _split(tokens)
    if len(segments) != 1 or segments[0][1]:
        return None
    return segments[0][0]


def _worktree_target(command: str) -> tuple[str, str] | None:
    """(subcommand, target path) of a plain `git worktree add|remove` using only safe
    options; `-b <branch>` only (`-B` resets an existing branch, so it keeps its tier)."""
    tokens = _plain_words(command)
    if tokens is None:
        return None
    if any(c in _WT_META_CHARS or t.startswith(SENTINEL_PREFIX) for t in tokens for c in t):
        return None
    if tokens[:2] != ["git", "worktree"] or len(tokens) < 3 or tokens[2] not in ("add", "remove"):
        return None
    sub, args = tokens[2], tokens[3:]
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if sub == "add" and a in _WT_BRANCH_OPTS:
            if i + 1 >= len(args) or args[i + 1].startswith("-"):
                return None
            i += 2
            continue
        if sub == "add" and a in _WT_SAFE_FLAGS:
            i += 1
            continue
        if not a or a.startswith("-"):
            return None
        positional.append(a)
        i += 1
    if not 1 <= len(positional) <= (2 if sub == "add" else 1):
        return None
    return sub, positional[0]


def _worktree_roots(top: Path, home_real: Path) -> list[Path]:
    """Allowed worktrees/ roots for repo `top`, only those strictly inside $HOME."""
    roots = [top.parent / "worktrees", top / ".claude" / "worktrees"]
    if top.parent.name == "worktrees":
        roots.append(top.parent)
    return [r for r in roots if r != home_real and r.is_relative_to(home_real)]


def _worktree_path_ok(
    path: str, roots: list[Path], cwd: str, env: dict[str, str], home: str
) -> bool:
    if path == os.path.realpath(home) or path.startswith(_WT_SYSTEM_PREFIXES):
        return False
    if classify_path(path, cwd, env) in ("<cred_path>", "<etc_path>", "<system_path>"):
        return False
    p = Path(path)
    if p.parent not in roots:  # direct child of an allowed root, no nesting
        return False
    verdict = rules.evaluate(f"touch {shlex.quote(path)}", cwd)
    return verdict.config_error is None and not verdict.matches


def worktree_target_allowed(command: str, cwd: str, env: dict[str, str] | None = None) -> bool:
    """True iff `command` is a plain `git worktree add|remove` passing every check:
    direct child of an allowed root, repo top not $HOME/ancestor, no post-checkout hook
    possible (add), unambiguous registered worktree (remove). Fail closed on any doubt."""
    env = env or {}
    home = env.get("HOME", os.path.expanduser("~"))
    parsed = _worktree_target(command)
    if parsed is None:
        return False
    sub, target = parsed
    cwd_real = os.path.realpath(cwd)
    top = _repo_top(Path(cwd_real))
    home_real = Path(os.path.realpath(home))
    if top is None or top == home_real or top in home_real.parents:
        return False
    roots = _worktree_roots(top, home_real)
    joined = os.path.join(cwd_real, target)
    candidates = (os.path.normpath(joined), os.path.realpath(joined))
    if not all(_worktree_path_ok(c, roots, cwd_real, env, home) for c in candidates):
        return False
    # remove checks the worktree is clean (status-like), so it needs the same inert
    # hooks/config as add
    if worktree_git.hooks_or_config_possible(top, home, env):
        return False
    if sub == "add":
        return True
    dirs = worktree_git.git_dirs(top)
    return dirs is not None and worktree_git.remove_target_unambiguous(dirs[1], target, candidates)


def _worktree_finding(
    cp: CommandPaths, command: str, cwd: str, env: dict[str, str], *, sole: bool
) -> GuardFinding | None:
    if cp.verb != "git" or not any(
        a == "worktree" and b in ("add", "remove")
        for a, b in zip(cp.args, cp.args[1:], strict=False)
    ):
        return None
    if sole and worktree_target_allowed(command, cwd, env):
        return None
    return GuardFinding(
        Tier.APPROVE_ONCE,
        "worktree_path",
        "git worktree add/remove needs approval unless it is a plain, non-forced command "
        "targeting an allowed worktrees/ directory",
    )


def _mkdir_target_ok(path: str, cwd: str, home: str) -> bool:
    """In cwd (or /tmp, left to the catalog's scratch rules) and not protected."""
    abs_p = _resolve_abs(path, cwd, home)
    cwd_real = Path(os.path.realpath(cwd))
    p = Path(abs_p)
    if not (p.is_relative_to(cwd_real) or abs_p == _TMP_ROOT or abs_p.startswith(_TMP_ROOT + "/")):
        return False
    verdict = rules.evaluate(f"touch {shlex.quote(abs_p)}", cwd)
    return verdict.config_error is None and not verdict.matches


def _mkdir_finding(cp: CommandPaths, cwd: str, home: str) -> GuardFinding | None:
    """mkdir stays T2 only when every target resolves inside cwd (or /tmp scratch) and
    is not a protected path: `..` hops are classified <cwd_path> by the normalizer."""
    if cp.verb != "mkdir":
        return None
    for target in cp.args:
        dynamic = any(c in target for c in _DYNAMIC_CHARS)
        if dynamic or not all(_mkdir_target_ok(c, cwd, home) for c in _candidates(target, cp)):
            return GuardFinding(
                Tier.APPROVE_ONCE,
                "mkdir_outside_cwd",
                f"mkdir {target} resolves outside cwd or onto a protected path",
            )
    return None


def write_labels(command: str) -> list[str]:
    """`fs-write: ...` audit labels for output redirects and `tee` targets.

    /dev/null-style devices are not writes and fd duplications (2>&1, >&2) never
    reach `redirects`.
    """
    labels: list[str] = []
    for cp in analyze(command):
        labels += [
            f"fs-write: redirect {t}"
            for t in cp.redirects
            if os.path.normpath(t) not in _SAFE_DEVICES
        ]
        if cp.verb == "tee":
            labels += [
                f"fs-write: tee {t}"
                for t in cp.writes[len(cp.redirects) :]
                if os.path.normpath(t) not in _SAFE_DEVICES
            ]
    return labels
