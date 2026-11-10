"""Read-only git metadata probes for the `git worktree add|remove` T2 check.

Never runs git: parses `.git`, `commondir`, `worktrees/*/gitdir` and config files with
plain file reads. Every probe fails closed (unreadable / unexpected means "unsafe").
Used by path_guard.worktree_target_allowed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_FORBIDDEN_RE = re.compile(
    r"hookspath|\[\s*include|\[\s*filter|filter\.|fsmonitor|attributesfile|sshcommand"
    r"|pager|editor|askpass"
)
_SECTION_RE = re.compile(r"^\[\s*([a-z0-9.-]+)(?:\s+\"[^\"]*\")?\s*\](.*)$")
_KEY_RE = re.compile(r"^([a-z][a-z0-9-]*)\s*(?:=.*)?$")
_DRIVER_KEYS = frozenset({"driver", "command", "external"})
_LFS_HEADER_RE = re.compile(r'^\[\s*(?i:filter)\s+"lfs"\s*\]$')
_LFS_KEY_RE = re.compile(r"^([A-Za-z]+)\s*=\s*(.*?)\s*$")
# the lines `git lfs install` writes; keys are case-insensitive, values exact
_STOCK_LFS = {
    "clean": "git-lfs clean -- %f",
    "smudge": "git-lfs smudge -- %f",
    "process": "git-lfs filter-process",
    "required": "true",
}

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


def _line_unsafe(line: str, section: str) -> tuple[bool, str]:
    """(unsafe, section) for one stripped config line, handling `[sec] key = v` forms."""
    while line and line[0] not in "#;":
        m = _SECTION_RE.match(line)
        if m:
            section, line = m.group(1).split(".")[0], m.group(2).strip()
            continue
        k = _KEY_RE.match(line)
        if k is None or line.endswith("\\"):
            return True, section  # not cleanly parseable (or a continued value)
        return section in ("diff", "merge") and k.group(1) in _DRIVER_KEYS, section
    return False, section


def _strip_stock_lfs(text: str) -> str | None:
    """`text` without `[filter "lfs"]` sections, or None unless each is the stock
    `git lfs install` section (only the four stock key/value lines, nothing else)."""
    out: list[str] = []
    in_lfs = False
    for raw in text.splitlines():
        line = raw.strip()
        if _LFS_HEADER_RE.match(line):
            in_lfs = True
            continue
        if in_lfs and line.startswith("["):
            in_lfs = False
        if in_lfs:
            m = _LFS_KEY_RE.match(line) if line else None
            if m is None and line:
                return None
            if m is not None and _STOCK_LFS.get(m.group(1).lower()) != m.group(2):
                return None
            continue
        out.append(raw)
    return "\n".join(out)


def _text_unsafe(text: str, *, user_level: bool = False) -> bool:
    """Git config text that could make checkout/worktree-add run something.

    Substring hits (hooksPath, includes, filters, fsmonitor, attributesFile, sshCommand,
    pager, editor, askpass) anywhere, a diff/merge section with driver/command/external,
    or any line that is not cleanly `[section]` / `key` / `key = value` / comment.
    The one exception: in user/system-level config (never the repo's), the exact stock
    git-lfs `[filter "lfs"]` section is inert and ignored.
    """
    if user_level:
        stripped = _strip_stock_lfs(text)
        if stripped is None:
            return True
        text = stripped
    low = text.lower()
    if _FORBIDDEN_RE.search(low):
        return True
    section = ""
    for raw in low.splitlines():
        bad, section = _line_unsafe(raw.strip(), section)
        if bad:
            return True
    return False


def _config_unsafe(repo_files: list[Path], user_files: list[Path]) -> bool:
    """True when any config file is risky (see _text_unsafe) or can't be read.

    A missing file is fine; any other read error is unsafe.
    """
    for user_level, files in ((False, repo_files), (True, user_files)):
        for f in files:
            try:
                text = f.read_text(errors="replace")
            except FileNotFoundError:
                continue
            except OSError:
                return True
            if _text_unsafe(text, user_level=user_level):
                return True
    return False


def _hooks_dir_unsafe(hooks: Path) -> bool:
    """True unless `hooks` is missing or holds only regular `*.sample` files."""
    try:
        if hooks.is_symlink():
            return True
        entries = list(hooks.iterdir())
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return any(e.is_symlink() or not e.is_file() or not e.name.endswith(".sample") for e in entries)


def _git_env_set() -> bool:
    return any(
        k in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE") or k.startswith("GIT_CONFIG")
        for k in os.environ
    )


def hooks_or_config_possible(top: Path, home: str, env: dict[str, str]) -> bool:
    """True unless NOTHING configured can run code during `git worktree add` from `top`.

    Safe means: `<common>/hooks` (and `<private>/hooks`) missing or only regular
    `*.sample` files (any other entry, symlink or listing error is unsafe), and every
    scanned config clean (see _text_unsafe). Defense in depth; fails closed.
    """
    if _git_env_set():
        return True
    dirs = git_dirs(top)
    if dirs is None:
        return True
    priv, common = dirs
    if any(_hooks_dir_unsafe(d / "hooks") for d in {priv, common}):
        return True
    xdg = env.get("XDG_CONFIG_HOME") or os.environ.get("XDG_CONFIG_HOME") or f"{home}/.config"
    return _config_unsafe(
        [common / "config", common / "config.worktree", priv / "config.worktree"],
        [Path(home) / ".gitconfig", Path(xdg) / "git" / "config", SYSTEM_GITCONFIG],
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
