"""Read-only git metadata probes for the `git worktree add|remove` T2 check.

Never runs git: parses `.git`, `commondir`, `worktrees/*/gitdir` and config files with
plain file reads. Every probe fails closed (unreadable / unexpected means "unsafe").
Used by path_guard.worktree_target_allowed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_INCLUDE_RE = re.compile(r"\[\s*include")

# Patched by tests only; deliberately not configurable from the environment.
SYSTEM_GITCONFIG = Path("/etc/gitconfig")


def git_dirs(top: Path) -> tuple[Path, Path] | None:
    """(private gitdir, common dir) of the checkout rooted at `top`, or None.

    `.git` is a directory (main checkout: both are it) or a `gitdir: <path>` file
    (linked worktree: follow it, then its `commondir`).
    """
    dot = top / ".git"
    try:
        if dot.is_dir():
            return dot, dot
        text = dot.read_text()
        if not text.startswith("gitdir:"):
            return None
        priv = Path(os.path.normpath(top / text[len("gitdir:") :].strip()))
        common = priv
        marker = priv / "commondir"
        if marker.exists():
            common = Path(os.path.normpath(priv / marker.read_text().strip()))
        return (priv, common) if common.is_dir() else None
    except (OSError, ValueError):
        return None


def _config_unsafe(files: list[Path]) -> bool:
    """True when any config file sets/might set core.hooksPath (or can't be read).

    Plain text scan: any `hookspath` (case-insensitive) or `[include*]` section counts.
    A missing file is fine; any other read error is unsafe.
    """
    for f in files:
        try:
            low = f.read_text(errors="replace").lower()
        except FileNotFoundError:
            continue
        except OSError:
            return True
        if "hookspath" in low or _INCLUDE_RE.search(low):
            return True
    return False


def _git_env_set() -> bool:
    return any(
        k in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE") or k.startswith("GIT_CONFIG")
        for k in os.environ
    )


def post_checkout_hook_possible(top: Path, home: str, env: dict[str, str]) -> bool:
    """True unless NO post-checkout hook can run for `git worktree add` from `top`."""
    if _git_env_set():
        return True
    dirs = git_dirs(top)
    if dirs is None:
        return True
    priv, common = dirs
    # lexists: a non-executable file or a dangling symlink counts as present
    if os.path.lexists(common / "hooks" / "post-checkout"):
        return True
    xdg = env.get("XDG_CONFIG_HOME") or os.environ.get("XDG_CONFIG_HOME") or f"{home}/.config"
    return _config_unsafe(
        [
            common / "config",
            common / "config.worktree",
            priv / "config.worktree",
            Path(home) / ".gitconfig",
            Path(xdg) / "git" / "config",
            SYSTEM_GITCONFIG,
        ]
    )


def registered_worktrees(common: Path) -> list[str] | None:
    """Paths of the linked worktrees in `<common>/worktrees/*/gitdir`; None on any error."""
    wdir = common / "worktrees"
    try:
        entries = sorted(wdir.iterdir())
    except FileNotFoundError:
        return []
    except OSError:
        return None
    out: list[str] = []
    for entry in entries:
        try:
            line = (entry / "gitdir").read_text().strip()
        except OSError:
            return None
        if not line:
            return None
        out.append(os.path.dirname(line))  # gitdir holds <worktree>/.git
    return out


def remove_target_unambiguous(common: Path, arg: str, candidates: tuple[str, ...]) -> bool:
    """`git worktree remove <arg>` resolves to exactly one registered worktree.

    Git matches <arg> as a path-component suffix of registered worktrees before treating
    it as a path, so require: every resolved candidate equals a registered worktree path,
    and no OTHER registered worktree path ends with "/" + normalized <arg>.
    """
    regs = registered_worktrees(common)
    if not regs:
        return False
    known = {os.path.normpath(r) for r in regs} | {os.path.realpath(r) for r in regs}
    if not all(c in known for c in candidates):
        return False
    target = os.path.realpath(candidates[-1])
    tail = "/" + os.path.normpath(arg)
    for r in regs:
        if os.path.realpath(r) != target and (
            os.path.normpath(r).endswith(tail) or os.path.realpath(r).endswith(tail)
        ):
            return False
    return True
