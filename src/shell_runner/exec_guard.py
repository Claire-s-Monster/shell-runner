"""Raw-token checks for awk, sed and git commands that run code or write files (issue #85).

The normalizer collapses an awk/sed script to `<arg>`, so the catalog's T1/T2 rules for
`awk <arg> <file>`, `sed <arg> <file>` and `git <read-verb> ...` cannot tell
`awk '{print $1}'` from `awk 'BEGIN{system("id")}'`, nor `git fetch origin` from
`git fetch --upload-pack=CMD origin`. This module re-reads the raw words of one simple
command (write_targets.CommandPaths.words) and returns a (tier, category, reason)
verdict, or None when the command is one of the known-safe shapes.

FAIL CLOSED: anything that does not parse as a known-safe shape is T3.
"""

from __future__ import annotations

import re

from .catalog import Tier
from .write_targets import CommandPaths

# (tier, category, reason, guarded). guarded=True: persisted template/verb approvals must
# not lower the tier (the approved template is shared with a code-executing variant).
# guarded=False: an ordinary T3, approvable like any other (git remote/branch/tag/stash writes).
Verdict = tuple[Tier, str, str, bool]

_T3 = Tier.APPROVE_ONCE
_T4 = Tier.ALWAYS_APPROVE


def check(cp: CommandPaths) -> Verdict | None:
    """Verdict for one simple command, or None when it is not awk/sed/git or is safe."""
    if cp.verb in _AWK_VERBS:
        return _awk(cp.words)
    if cp.verb == "sed":
        return _sed(cp.words)
    if cp.verb == "git":
        return _git(cp.words)
    return None


def _prefix_of(name: str, options: tuple[str, ...], *, min_len: int = 3) -> bool:
    """True when `name` (`--opt`, no value) abbreviates one of `options` (git/GNU accept
    unique prefixes, so `--upload-p` is `--upload-pack`)."""
    return len(name) >= min_len and any(o.startswith(name) for o in options)


# ---------------------------------------------------------------------------
# awk
# ---------------------------------------------------------------------------

_AWK_VERBS = frozenset({"awk", "gawk", "mawk", "nawk"})
_AWK_HAZARD_LONG = ("--file", "--include", "--load", "--exec")
_AWK_HAZARD_SHORT = frozenset("filE")
_AWK_SYSTEM_RE = re.compile(r"(?<![A-Za-z0-9_])system(?![A-Za-z0-9_])")
_AWK_HAZARD_TEXT = ("system", "getline", "close", "fflush", "|", ">", "@")


def _awk_scripts(words: list[str]) -> tuple[list[str], bool]:
    """(program texts, hazardous_option) from awk's raw words."""
    scripts: list[str] = []
    has_source = False
    i = 0
    while i < len(words):
        w = words[i]
        if w == "--":
            i += 1
            break
        if w.startswith("--"):
            name, eq, value = w.partition("=")
            if _prefix_of(name, _AWK_HAZARD_LONG):
                return scripts, True
            if _prefix_of(name, ("--source",), min_len=4):
                has_source = True
                scripts.append(value if eq else words[i + 1] if i + 1 < len(words) else "")
                i += 1 if eq else 2
                continue
            i += 2 if (not eq and _prefix_of(name, ("--assign",), min_len=4)) else 1
            continue
        if w.startswith("-") and len(w) > 1:
            if w[1] in _AWK_HAZARD_SHORT:
                return scripts, True
            if w[1] == "e":
                has_source = True
                scripts.append(w[2:] or (words[i + 1] if i + 1 < len(words) else ""))
                i += 1 if len(w) > 2 else 2
                continue
            i += 2 if (w[1] in "Fv" and len(w) == 2) else 1
            continue
        break
    if not has_source and i < len(words):
        scripts.append(words[i])
    return scripts, False


def _awk(words: list[str]) -> Verdict | None:
    scripts, hazardous = _awk_scripts(words)
    if hazardous:
        msg = "awk loads a program file/extension (-f/-i/-l/-E/--file/...)"
        return (_T3, "awk_unsafe", msg, True)
    for script in scripts:
        if _AWK_SYSTEM_RE.search(script):
            msg = "awk program calls system(): executes shell commands"
            return (_T4, "awk_system", msg, True)
        hit = next((t for t in _AWK_HAZARD_TEXT if t in script), None)
        if hit is not None:
            msg = f"awk program contains {hit!r} (pipe/redirect/getline)"
            return (_T3, "awk_unsafe", msg, True)
    return None


# ---------------------------------------------------------------------------
# sed
# ---------------------------------------------------------------------------

_SED_HAZARD_LONG = ("--file", "--separate", "--null-data")
_SED_SAFE_LONG = (
    "--quiet",
    "--silent",
    "--regexp-extended",
    "--debug",
    "--posix",
    "--follow-symlinks",
    "--sandbox",
    "--binary",
    "--unbuffered",
    "--in-place",
    "--line-length",
    "--help",
    "--version",
)
_SED_SAFE_SHORT = frozenset("nErub")
_SED_S_FLAGS = frozenset("gpIi0123456789")
_SED_SIMPLE_CMDS = frozenset("pdqQnNgGhHxlzF=")
_SED_TEXT_CMDS = frozenset("aic")
_SED_BRANCH_CMDS = frozenset("btT")
_SED_LABEL_RE = re.compile(r"[A-Za-z0-9_.-]*")


def _sed_long_option(words: list[str], i: int, scripts: list[str]) -> int | None:
    """Handle one `--long` option at words[i]; next index, or None if hazardous/unknown."""
    name, eq, value = words[i].partition("=")
    if _prefix_of(name, _SED_HAZARD_LONG):
        return None
    if _prefix_of(name, ("--expression",), min_len=4):
        scripts.append(value if eq else words[i + 1] if i + 1 < len(words) else "")
        return i + (1 if eq else 2)
    if not _prefix_of(name, _SED_SAFE_LONG):
        return None
    takes_value = _prefix_of(name, ("--line-length",), min_len=4)
    return i + (2 if takes_value and not eq else 1)


def _sed_short_option(words: list[str], i: int, scripts: list[str]) -> int | None:
    """Handle one short option cluster at words[i]; next index, or None if hazardous."""
    w = words[i]
    for k in range(1, len(w)):
        c = w[k]
        if c == "e":
            scripts.append(w[k + 1 :] or (words[i + 1] if i + 1 < len(words) else ""))
            return i + (1 if w[k + 1 :] else 2)
        if c == "i":
            return i + 1  # the rest of the cluster is the backup suffix
        if c == "l":
            return i + (1 if w[k + 1 :] else 2)
        if c not in _SED_SAFE_SHORT:
            return None  # -f, -s, -z, unknown
    return i + 1


def _sed_scripts(words: list[str]) -> list[str] | None:
    """Script texts from sed's raw words, or None when an option is unsafe/unknown."""
    scripts: list[str] = []
    has_expr = False
    i = 0
    while i < len(words):
        w = words[i]
        if w == "--":
            i += 1
            break
        if w.startswith("--"):
            before = len(scripts)
            nxt = _sed_long_option(words, i, scripts)
            has_expr = has_expr or len(scripts) > before
        elif w.startswith("-") and len(w) > 1:
            before = len(scripts)
            nxt = _sed_short_option(words, i, scripts)
            has_expr = has_expr or len(scripts) > before
        else:
            break
        if nxt is None:
            return None
        i = nxt
    if not has_expr and i < len(words):
        scripts.append(words[i])
    return scripts


def _scan_delimited(s: str, i: int, delim: str) -> int:
    """Index just past the next unescaped `delim` at/after i; -1 when absent or a raw
    newline intervenes (a multi-line s/// part is not in the safe grammar)."""
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "\n":
            return -1
        if c == delim:
            return i + 1
        i += 1
    return -1


def _sed_address(s: str, i: int) -> int:
    """Index after one address at s[i], i itself when there is none, -1 if malformed."""
    n = len(s)
    if i >= n:
        return i
    if s[i].isdigit():
        while i < n and s[i].isdigit():
            i += 1
        return i
    if s[i] == "$":
        return i + 1
    if s[i] == "/":
        j = _scan_delimited(s, i + 1, "/")
        if j < 0:
            return -1
        return j + 1 if j < n and s[j] == "I" else j
    return i


def _sed_addresses(s: str, i: int) -> int:
    """Skip `[addr[,addr]][!]` starting at i; -1 when malformed."""
    n = len(s)
    j = _sed_address(s, i)
    if j < 0:
        return -1
    if j > i and j < n and s[j] == ",":
        k = _sed_address(s, j + 1)
        if k <= j + 1:
            return -1
        j = k
    while j < n and s[j] in " \t":
        j += 1
    if j < n and s[j] == "!":
        j += 1
        while j < n and s[j] in " \t":
            j += 1
    return j


def _sed_command(s: str, i: int) -> int:
    """Skip one safe command at s[i]; index after it, or -1 when not in the safe grammar."""
    n = len(s)
    if i >= n:
        return -1
    c = s[i]
    if c in "sy":
        if i + 1 >= n:
            return -1
        delim = s[i + 1]
        if delim.isalnum() or delim.isspace() or delim == "\\":
            return -1
        j = _scan_delimited(s, i + 2, delim)
        j = _scan_delimited(s, j, delim) if j >= 0 else -1
        if j < 0:
            return -1
        while c == "s" and j < n and s[j] in _SED_S_FLAGS:
            j += 1
        return j
    if c in _SED_SIMPLE_CMDS:
        j = i + 1
        if c in "qQl":
            while j < n and s[j] in " \t":
                j += 1
            while j < n and s[j].isdigit():
                j += 1
        return j
    return -1


def _sed_text(s: str, i: int) -> int:
    """Skip the literal text of an a/i/c command starting right after the command letter.

    Forms: `a text`, `a\\text`, and `a\\` + newline + text. The text runs to an unescaped
    newline or the end of the script; backslash-newline continues it. Any other backslash
    (GNU escape processing) or a missing text is ambiguous -> -1. Returns the index just
    past the consumed newline (or the script end)."""
    n = len(s)
    while i < n and s[i] in " \t":
        i += 1
    if i < n and s[i] == "\\":
        i += 1
        while i < n and s[i] in " \t":
            i += 1
        if i < n and s[i] == "\n":
            i += 1
    if i >= n:
        return -1
    while i < n:
        if s[i] == "\n":
            return i + 1
        if s[i] == "\\":
            if i + 1 < n and s[i + 1] == "\n":
                i += 2
                continue
            return -1
        i += 1
    return i


def _sed_label(s: str, i: int, *, required: bool) -> int:
    """Skip an optional-space-prefixed label; -1 when required and empty."""
    n = len(s)
    while i < n and s[i] in " \t":
        i += 1
    m = _SED_LABEL_RE.match(s, i)
    end = m.end() if m else i
    return -1 if required and end == i else end


def _sed_command_end(s: str, i: int) -> int:
    """After a command: only blanks, then `;`, newline, `}` or the end may follow."""
    n = len(s)
    while i < n and s[i] in " \t":
        i += 1
    return -1 if i < n and s[i] not in ";\n}" else i


def _sed_item(s: str, i: int) -> tuple[int, int]:
    """Parse one command item at s[i] (not a comment, `}` or label definition).
    Returns (next index, block depth change); next index -1 when outside the grammar."""
    j = _sed_addresses(s, i)
    if j < 0 or j >= len(s):
        return -1, 0
    c = s[j]
    if c == "{":
        return j + 1, 1
    if c in _SED_TEXT_CMDS:
        return _sed_text(s, j + 1), 0
    if c in _SED_BRANCH_CMDS:
        k = _sed_label(s, j + 1, required=False)
        return (_sed_command_end(s, k) if k >= 0 else -1), 0
    k = _sed_command(s, j)
    return (_sed_command_end(s, k) if k >= 0 else -1), 0


def _safe_sed_script(s: str) -> bool:
    """True only for items joined by `;`/newlines: `[addr[,addr]][!]cmd` with cmd one of
    `s<d>re<d>repl<d>[gpIi0-9]*`, `y<d>..<d>..<d>`, `p d q Q n N g G h H x l z F =`,
    `a/i/c` text, `b/t/T [label]`, or a `{ ... }` block of such; plus `:label` and `#`
    comments. w W r R e (and everything else) are never accepted, inside blocks too."""
    n = len(s)
    i = 0
    depth = 0
    while True:
        while i < n and s[i] in " \t;\n":
            i += 1
        if i >= n:
            return depth == 0
        c = s[i]
        if c == "#":
            while i < n and s[i] != "\n":
                i += 1
            continue
        if c == "}":
            depth -= 1
            i = _sed_command_end(s, i + 1)
        elif c == ":":
            i = _sed_label(s, i + 1, required=True)
        else:
            i, delta = _sed_item(s, i)
            depth += delta
        if i < 0 or depth < 0:
            return False


def _sed(words: list[str]) -> Verdict | None:
    scripts = _sed_scripts(words)
    if scripts is None:
        msg = "sed option can read/write files or is not recognised"
        return (_T3, "sed_unsafe", msg, True)
    # GNU joins the script fragments (-e chunks) with newlines before parsing
    if _safe_sed_script("\n".join(scripts)):
        return None
    msg = "sed script is outside the safe grammar (e/w/r/R/W, w or e flag, or unparseable)"
    return (_T3, "sed_unsafe", msg, True)


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------

_GIT_HAZARD_LONG = (
    "--upload-pack",
    "--receive-pack",
    "--exec",
    "--output",
    "--ext-diff",
    "--textconv",
    "--config-env",
    "--git-dir",
    "--work-tree",
    "--namespace",
)
_GIT_U_SUBCOMMANDS = frozenset({"fetch", "ls-remote"})
_GIT_REF_SUBCOMMANDS = frozenset({"branch", "tag"})  # -c/-C/-u are ref operations there
_GIT_REF_WRITE_LONG = (
    "--delete",
    "--force",
    "--move",
    "--copy",
    "--set-upstream-to",
    "--unset-upstream",
    "--edit-description",
)
_GIT_REF_WRITE_SHORT = frozenset("dDfmMuCcF")
_GIT_STASH_READ = frozenset({"list", "show"})


def _git_subcommand_index(words: list[str]) -> int:
    """Index of the subcommand word (after global options; `-C <dir>` takes a value)."""
    i = 0
    while i < len(words):
        if not words[i].startswith("-"):
            return i
        i += 2 if words[i] == "-C" else 1
    return len(words)


def _git_hazard_option(word: str, *, in_global: bool, sub: str) -> str | None:
    if word.startswith("--"):
        name = word.partition("=")[0]
        return name if _prefix_of(name, _GIT_HAZARD_LONG) else None
    if in_global and word.startswith("-c"):
        return "-c"
    if word == "-c" and sub not in _GIT_REF_SUBCOMMANDS:
        return "-c"
    if word == "-u" and sub in _GIT_U_SUBCOMMANDS:
        return "-u"
    return None


def _git_option_hazard(words: list[str], sub_idx: int) -> str | None:
    sub = words[sub_idx] if sub_idx < len(words) else ""
    skip = False
    for k, w in enumerate(words):
        if skip:
            skip = False
            continue
        if w == "--":
            return None
        skip = w == "-C" and k < sub_idx
        hit = _git_hazard_option(w, in_global=k < sub_idx, sub=sub)
        if hit is not None:
            return hit
    return None


def _git_remote_safe(rest: list[str]) -> bool:
    if rest in ([], ["-v"], ["--verbose"]):
        return True
    return len(rest) == 2 and rest[0] in ("show", "get-url") and not rest[1].startswith("-")


def _git_ref_write(rest: list[str]) -> bool:
    for w in rest:
        if w == "--":
            return False
        if w.startswith("--"):
            if _prefix_of(w.partition("=")[0], _GIT_REF_WRITE_LONG):
                return True
        elif w.startswith("-") and any(c in _GIT_REF_WRITE_SHORT for c in w[1:]):
            return True
    return False


def _git_stash_safe(rest: list[str]) -> bool:
    action = next((w for w in rest if not w.startswith("-")), "")
    return action in _GIT_STASH_READ


def _git(words: list[str]) -> Verdict | None:
    sub_idx = _git_subcommand_index(words)
    hit = _git_option_hazard(words, sub_idx)
    if hit is not None:
        msg = f"git option {hit} can run commands or write files"
        return (_T3, "git_unsafe_args", msg, True)
    # No hazard option from here on: ordinary, approvable T3s (guarded=False).
    sub = words[sub_idx] if sub_idx < len(words) else ""
    rest = words[sub_idx + 1 :]
    if sub == "remote" and not _git_remote_safe(rest):
        return (_T3, "git_remote_write", "git remote subcommand changes remotes", False)
    if sub in _GIT_REF_SUBCOMMANDS and _git_ref_write(rest):
        msg = f"git {sub} option deletes, moves or forces refs"
        return (_T3, "git_ref_write", msg, False)
    if sub == "stash" and not _git_stash_safe(rest):
        msg = "git stash other than list/show changes the worktree"
        return (_T3, "git_stash_write", msg, False)
    return None
