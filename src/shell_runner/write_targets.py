"""Best-effort extraction of file write targets from a raw shell command.

Used by the rules engine (rules.py) to decide whether a command writes to, or
merely mentions, a protected path. This is an accident guardrail: it tokenizes
with shlex and understands a fixed list of common verbs. eval, variables,
scripts, interpreters and here-docs can evade it.

Public API:
    analyze(command) -> list[CommandPaths]
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

_PUNCT = frozenset("();<>|&")
_WRAPPERS = frozenset({"env", "nohup", "time", "command", "exec", "nice", "sudo", "doas"})
_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")
_INTERPRETER_RE = re.compile(r"^(?:python[\d.]*|perl|ruby|node|sh|bash|zsh|dash)$")
_INLINE_FLAG_RE = re.compile(r"^-[A-Za-z]*[ceE]$")

_DELETE_VERBS = frozenset({"rm", "rmdir", "truncate", "touch", "shred", "unlink"})
_COPY_VERBS = frozenset({"cp", "mv", "install", "ln"})


@dataclass
class CommandPaths:
    """Raw (unresolved) path strings for one simple command."""

    verb: str = ""
    writes: list[str] = field(default_factory=list)
    redirects: list[str] = field(default_factory=list)  # output redirect targets only
    options: list[str] = field(default_factory=list)  # option words (before `--`)
    args: list[str] = field(default_factory=list)  # every non-option argument
    inline_code: list[str] = field(default_factory=list)  # -c/-e one-liner texts
    # Extra directories (relative to the evaluation cwd, or absolute) that
    # relative paths may resolve against: earlier cd/pushd targets and git -C.
    bases: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _tokenize(command: str) -> list[str]:
    text = command.replace("\n", " ; ")
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""
        return list(lex)
    except ValueError:
        return text.split()


def _is_punct(tok: str) -> bool:
    return bool(tok) and all(c in _PUNCT for c in tok)


def _split(tokens: list[str]) -> list[tuple[list[str], list[str]]]:
    """Split into simple commands: (words, redirect_targets) pairs."""
    result: list[tuple[list[str], list[str]]] = []
    words: list[str] = []
    redirects: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _is_punct(tok) and ">" in tok:
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            is_fd_dup = tok.endswith("&") and (nxt.isdigit() or nxt == "-")
            if nxt and not _is_punct(nxt) and not is_fd_dup:
                redirects.append(nxt)
            i += 2 if nxt and not _is_punct(nxt) else 1
            continue
        if _is_punct(tok):
            if any(c in tok for c in "|;&()"):
                if words or redirects:
                    result.append((words, redirects))
                words, redirects = [], []
            i += 1
            continue
        words.append(tok)
        i += 1
    if words or redirects:
        result.append((words, redirects))
    return result


def _strip_prefix(words: list[str]) -> list[str]:
    """Drop leading VAR=val assignments and transparent wrappers (env, nohup, ...)."""
    idx = 0
    while idx < len(words):
        w = words[idx]
        if _ASSIGN_RE.match(w) or w in _WRAPPERS or (w.startswith("-") and idx > 0):
            idx += 1
            continue
        break
    return words[idx:]


def _positional(args: list[str]) -> list[str]:
    """Non-option args; everything after `--` is positional."""
    out: list[str] = []
    after_dd = False
    for a in args:
        if after_dd:
            out.append(a)
        elif a == "--":
            after_dd = True
        elif not a.startswith("-") or a == "-":
            out.append(a)
    return out


def _option_values(args: list[str]) -> list[str]:
    """Values of `--opt=value` options (they may name paths)."""
    return [a.split("=", 1)[1] for a in args if a.startswith("--") and "=" in a]


def _target_dir(args: list[str]) -> str | None:
    for i, a in enumerate(args):
        if a in ("-t", "--target-directory") and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--target-directory="):
            return a.split("=", 1)[1]
        if a.startswith("-t") and len(a) > 2 and not a.startswith("--"):
            return a[2:]
    return None


def _skip_target_dir_value(args: list[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a in ("-t", "--target-directory"):
            skip = True
            continue
        out.append(a)
    return out


def _is_sed_inplace(args: list[str]) -> bool:
    for a in args:
        if a == "--in-place" or a.startswith("--in-place="):
            return True
        if re.match(r"^-[A-Za-z]*i", a) and not a.startswith("--"):
            return True
    return False


def _sed_files(args: list[str]) -> list[str]:
    pos = _positional(args)
    has_script_opt = any(
        a in ("-e", "-f", "--expression", "--file") or a.startswith(("--expression=", "--file="))
        for a in args
    )
    return pos if has_script_opt else pos[1:]


def _git_subcommand(args: list[str]) -> tuple[str, list[str]]:
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a, args[i + 1 :]
    return "", []


def _git_c_dirs(args: list[str]) -> list[str]:
    """Values of `-C DIR` given before the git subcommand."""
    dirs: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            if a == "-C" and i + 1 < len(args):
                dirs.append(args[i + 1])
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        break
    return dirs


def _tar_dirs(args: list[str]) -> list[str]:
    """Directories tar writes into: -C DIR, -CDIR, --directory DIR / =DIR."""
    dirs: list[str] = []
    for i, a in enumerate(args):
        if a in ("-C", "--directory") and i + 1 < len(args):
            dirs.append(args[i + 1])
        elif a.startswith("--directory="):
            dirs.append(a.split("=", 1)[1])
        elif a.startswith("-C") and len(a) > 2:
            dirs.append(a[2:])
    return dirs


def _join_base(base: str, directory: str) -> str:
    if directory.startswith(("/", "~")):
        return directory
    return f"{base}/{directory}" if base else directory


def _extend_bases(bases: list[str], directory: str) -> list[str]:
    """Add `directory` under every known base (and the original cwd), chained."""
    out = list(bases)
    for b in ["", *bases]:
        joined = _join_base(b, directory)
        if joined not in out:
            out.append(joined)
    return out


def _cd_directory(args: list[str]) -> str | None:
    """The directory a cd/pushd moves to, or None when it cannot be known."""
    pos = [a for a in args if a == "-" or not a.startswith(("-", "+"))]
    if len(pos) != 1:
        return None
    target = pos[0]
    if target == "-" or any(c in target for c in "$`"):
        return None
    return target


def _track_cd(verb: str, args: list[str], bases: list[str], cp: CommandPaths) -> list[str]:
    target = _cd_directory(args)
    if target is None:
        cp.warnings.append(f"{verb}: target directory not statically known; paths not tracked")
        return bases
    return _extend_bases(bases, target)


def _git_writes(sub: str, args: list[str]) -> list[str]:
    if sub == "apply":
        directory = [
            args[i + 1] for i, a in enumerate(args) if a == "--directory" and i + 1 < len(args)
        ]
        directory += [a.split("=", 1)[1] for a in args if a.startswith("--directory=")]
        return _positional(_skip_value_opts(args, {"--directory"})) + directory
    if sub in ("checkout", "restore"):
        if "--" in args:
            return args[args.index("--") + 1 :]
        return _positional(args)
    if sub in ("rm", "mv"):
        return _positional(args)
    return []


def _skip_value_opts(args: list[str], opts: set[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a in opts:
            skip = True
            continue
        out.append(a)
    return out


def _inline_texts(args: list[str]) -> list[str]:
    """Code strings following -c/-e style flags (also clustered: -ne, -lc)."""
    return [
        args[i + 1] for i, a in enumerate(args) if i + 1 < len(args) and _INLINE_FLAG_RE.match(a)
    ]


def _copy_writes(verb: str, args: list[str]) -> list[str]:
    """cp/mv/install/ln: the destination (or -t dir); mv also removes its sources."""
    tdir = _target_dir(args)
    pos = _positional(_skip_target_dir_value(args))
    sources = pos if verb == "mv" else []
    if tdir is not None:
        return [tdir, *sources]
    return pos if verb == "mv" else pos[-1:]


def _verb_writes(verb: str, args: list[str]) -> list[str]:
    if verb == "tee" or verb in _DELETE_VERBS:
        return _positional(args)
    if verb == "sed":
        return _sed_files(args) if _is_sed_inplace(args) else []
    if verb in _COPY_VERBS:
        return _copy_writes(verb, args)
    if verb in ("chmod", "chown"):
        return _positional(args)[1:]
    if verb == "dd":
        return [a[3:] for a in args if a.startswith("of=")]
    if verb == "git":
        return _git_writes(*_git_subcommand(args))
    if verb == "tar":
        return _tar_dirs(args)
    if verb in ("perl", "ruby") and any(re.match(r"^-[A-Za-z]*i", a) for a in args):
        return _positional(args)
    return []


def analyze(command: str) -> list[CommandPaths]:
    """Return one CommandPaths per simple command found in `command`."""
    results: list[CommandPaths] = []
    cd_bases: list[str] = []
    for words, redirects in _split(_tokenize(command)):
        words = _strip_prefix(words)
        cp = CommandPaths(writes=list(redirects), redirects=list(redirects), bases=list(cd_bases))
        if words:
            verb = words[0].rsplit("/", 1)[-1]
            args = words[1:]
            cp.verb = verb
            if verb in ("cd", "pushd"):
                cd_bases = _track_cd(verb, args, cd_bases, cp)
            elif verb == "git":
                for d in _git_c_dirs(args):
                    cp.bases = _extend_bases(cp.bases, d)
            before_dd = args[: args.index("--")] if "--" in args else args
            cp.options = [a for a in before_dd if a.startswith("-") and a != "-"]
            cp.args = _positional(args) + _option_values(args)
            cp.writes += _verb_writes(verb, args)
            if _INTERPRETER_RE.match(verb):
                cp.inline_code = _inline_texts(args)
        results.append(cp)
    return results
