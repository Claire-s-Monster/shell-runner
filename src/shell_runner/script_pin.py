"""Script-content binding for commands that run a script file (issue #95).

Approvals match on raw_cmd/cwd/agent_id, so a script could be rewritten between
approval and execution. This module (1) analyzes a command and finds every script
file it runs, so the classifier can mark it non-promotable (and fail closed when the
script cannot be named statically), and (2) pins the script contents (sha256) when
a prompt is created and re-checks them before the approved command runs.

Detected: interpreters (`bash|sh|zsh|dash|ksh|mksh|ash|fish|python*|node|perl|ruby|
php [options] FILE`, `-c` bodies parsed recursively), `. FILE`, direct execution of
a path outside the trusted bin dirs, stdin scripts (`< FILE`, `cat FILE | bash`),
`awk|sed -f FILE`, `make` (the makefile it reads), `pixi|uv|npm|yarn|pnpm run` (the
task manifest), `python -m LOCALMOD`, `find -exec CMD`, all through compound forms
(`{ }`, `( )`, if/while/for, `!`, `time`) and wrappers (env, nohup, timeout, sudo,
...). Inline code (`-c`, `-e`), installed `-m` modules and the `bash -n` syntax check
run no local script file and are not pinned.

Anything that runs a script whose content cannot be bound statically (variables,
globs, `~user`, unknown producers on stdin, `eval`, `xargs`, unparseable wrapper
chains, bare `. name`) is *unpinnable*: it is refused every approval (fail closed).

Known residual TOCTOU: the hash is taken before dispatch, and the shell opens the
file afterwards; a swap in that window is not detected.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import stat
from dataclasses import dataclass, field

from .executor import trusted_bin_dirs
from .shellprep import SENTINEL_PREFIX
from .write_targets import _ASSIGN_RE, _cd_directory, _extend_bases, _is_punct, _tokenize

UNPINNABLE = "UNPINNABLE"  # script_sha256 sentinel: never matches, never executes
MISSING = "missing"
UNREADABLE = "unreadable"
_UNHASHED = frozenset({MISSING, UNREADABLE})
MAX_SCRIPT_BYTES = 64 << 20  # larger files are treated as unreadable (bounded hashing)
_MAX_DEPTH = 3  # nested `-c` bodies
_MAX_WRAP = 12
MAX_TOTAL_BYTES = 64 << 20  # total bytes hashed per command
MAX_FILES = 32  # files hashed per command; over budget the command is unpinnable
_UNRESOLVABLE_CHARS = "$`*?[{\n\0"
_OVER_BUDGET = "over_budget"
_MAKEFILES = ("GNUmakefile", "makefile", "Makefile")
_SUSPECT_RE = re.compile(
    r"\b(?:bash|sh|zsh|dash|ksh|mksh|ash|fish|python[\d.]*|node|perl|ruby|php|awk|gawk|mawk"
    r"|sed|make|pixi|uv|npm|yarn|pnpm|find|eval|source|xargs)\b|^\s*\.\s"
)


# --- interpreter option grammar --------------------------------------------------


@dataclass(frozen=True)
class _Interp:
    value: str = ""  # short flags taking a value (attached or next word)
    attached: str = ""  # short flags whose argument is the rest of the word
    inline: str = ""  # short flags: the next operand is code
    module: str = ""  # short flags: the next operand is a module name
    noexec: str = ""  # short flags: nothing is executed (syntax check)
    stdin: str = ""  # short flags: the script is read from stdin
    file_flags: str = ""  # short flags whose value is the script file
    long_value: frozenset[str] = frozenset()
    long_inline: frozenset[str] = frozenset()
    shell: bool = False


_SHELL = _Interp(
    value="oO",
    inline="c",
    noexec="n",
    stdin="s",
    long_value=frozenset({"--rcfile", "--init-file"}),
    shell=True,
)
_PYTHON = _Interp(
    value="WX", inline="c", module="m", long_value=frozenset({"--check-hash-based-pycs"})
)
_NODE = _Interp(
    value="r",
    inline="ep",
    long_value=frozenset(
        {"--require", "--import", "--loader", "--experimental-loader", "--input-type"}
    ),
    long_inline=frozenset({"--eval", "--print"}),
)
_PERL = _Interp(value="I", attached="MmixCFdDV", inline="eE")
_RUBY = _Interp(value="rICE", attached="ixKTWF", inline="e")
_PHP = _Interp(value="dczSt", inline="rBRE", noexec="lvimh", file_flags="fF")
_FISH = _Interp(value="dDoC", inline="c", noexec="n")
_FAMILIES = {
    "node": _NODE,
    "nodejs": _NODE,
    "perl": _PERL,
    "ruby": _RUBY,
    "php": _PHP,
    "fish": _FISH,
}
_SHELL_RE = re.compile(r"^(?:bash|sh|zsh|dash|ksh|ksh93|mksh|ash|rbash)$")
_PYTHON_RE = re.compile(r"^python[\d.]*$")


def _family(name: str) -> _Interp | None:
    if _SHELL_RE.match(name):
        return _SHELL
    if _PYTHON_RE.match(name):
        return _PYTHON
    return _FAMILIES.get(name)


def _scan_options(spec: _Interp, args: list[str]) -> tuple[str, str | None]:
    """Classify an interpreter invocation.

    Returns (kind, operand): file | inline | module | stdin | stdin! (explicitly
    requested) | none (nothing runs); operand is the file / code / module name.
    """
    inline = module = noexec = stdin = False
    i = 0
    operand: str | None = None
    while i < len(args):
        a = args[i]
        if a == "--":
            operand = args[i + 1] if i + 1 < len(args) else None
            break
        if a == "-":
            return "stdin!", None
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if name in spec.long_inline and eq:
                return "inline", val  # --eval=CODE
            inline = inline or name in spec.long_inline
            i += 2 if name in spec.long_value and not eq else 1
        elif a.startswith("+"):
            i += 2 if a in ("+o", "+O") else 1
        elif a.startswith("-"):
            flags = a[1:]
            step = 1
            for pos, ch in enumerate(flags):
                rest = flags[pos + 1 :]
                if ch in spec.file_flags:
                    val = rest or (args[i + 1] if i + 1 < len(args) else "")
                    return ("file", val) if val else ("none", None)
                if rest and not spec.shell and (ch in spec.inline or ch in spec.module):
                    # `-cCODE` / `-mmod`: the rest of the word is the operand
                    return ("inline" if ch in spec.inline else "module"), rest
                inline = inline or ch in spec.inline
                module = module or ch in spec.module
                noexec = noexec or ch in spec.noexec
                stdin = stdin or ch in spec.stdin
                if ch in spec.attached:
                    break
                if ch in spec.value:
                    step = 1 if rest else 2
                    break
            i += step
        else:
            operand = a
            break
    if operand is not None:
        if inline:
            return "inline", operand
        if module:
            return "module", operand
        if noexec:
            return "none", None
        return ("stdin!", None) if stdin else ("file", operand)
    if inline or module or noexec:
        return "none", None
    return ("stdin!", None) if stdin else ("stdin", None)


# --- wrapper commands ------------------------------------------------------------


@dataclass(frozen=True)
class _Wrapper:
    noarg: str = ""
    val: str = ""
    long_noarg: frozenset[str] = frozenset()
    long_val: frozenset[str] = frozenset()
    positional: int = 0  # operands before the wrapped command (timeout DURATION, ...)
    no_positional_with: str = ""  # this value flag replaces the positional operand
    assign: bool = False  # accepts VAR=val
    unpin: str = ""  # short flags that make the wrapped command unknowable
    long_unpin: frozenset[str] = frozenset()
    none_flags: str = ""  # short flags after which no command follows (query mode)
    body_flag: str = ""  # short flag whose value is shell code (flock -c)


_WRAPPERS: dict[str, _Wrapper] = {
    "env": _Wrapper(
        noarg="i0v",
        val="uC",
        long_noarg=frozenset({"--ignore-environment", "--null", "--debug", "--verbose"}),
        long_val=frozenset({"--unset", "--chdir"}),
        unpin="SC",  # -S string, -C DIR (directory change)
        long_unpin=frozenset({"--split-string", "--chdir"}),
        assign=True,
    ),
    "command": _Wrapper(noarg="p", none_flags="vV"),
    "builtin": _Wrapper(),
    "exec": _Wrapper(noarg="cl", val="a"),
    "nice": _Wrapper(val="n", long_val=frozenset({"--adjustment"})),
    "nohup": _Wrapper(),
    "timeout": _Wrapper(
        noarg="v",
        val="sk",
        long_noarg=frozenset({"--foreground", "--preserve-status", "--verbose"}),
        long_val=frozenset({"--signal", "--kill-after"}),
        positional=1,
    ),
    "setsid": _Wrapper(noarg="cfw", long_noarg=frozenset({"--ctty", "--fork", "--wait"})),
    "stdbuf": _Wrapper(val="ioe", long_val=frozenset({"--input", "--output", "--error"})),
    "sudo": _Wrapper(
        noarg="EHnSbiskKAPVe",
        val="ugCDhprtUT",
        long_noarg=frozenset(
            {
                "--preserve-env",
                "--login",
                "--shell",
                "--set-home",
                "--non-interactive",
                "--stdin",
                "--background",
                "--reset-timestamp",
                "--remove-timestamp",
                "--askpass",
                "--preserve-groups",
                "--edit",
            }
        ),
        long_val=frozenset(
            {
                "--user",
                "--group",
                "--chdir",
                "--host",
                "--prompt",
                "--role",
                "--type",
                "--other-user",
                "--command-timeout",
                "--close-from",
            }
        ),
        assign=True,
        none_flags="lv",
        unpin="D",  # -D DIR / --chdir change the directory the script resolves in
        long_unpin=frozenset({"--chdir"}),
    ),
    "doas": _Wrapper(noarg="nLs", val="uC"),
    "ionice": _Wrapper(
        noarg="t",
        val="cn",
        long_noarg=frozenset({"--ignore"}),
        long_val=frozenset({"--class", "--classdata"}),
        none_flags="p",
    ),
    "chrt": _Wrapper(noarg="firbomRva", val="TPD", positional=1, none_flags="p"),
    "taskset": _Wrapper(noarg="a", val="c", positional=1, no_positional_with="c", none_flags="p"),
    "flock": _Wrapper(
        noarg="xsuonF",
        val="wE",
        long_noarg=frozenset(
            {"--shared", "--exclusive", "--unlock", "--nonblock", "--nb", "--close", "--no-fork"}
        ),
        long_val=frozenset({"--timeout", "--wait", "--conflict-exit-code"}),
        positional=1,
        body_flag="c",
    ),
    "time": _Wrapper(noarg="pavq", val="fo"),
}

# systemd-run is unwrapped by _systemd_run (its options are too rich for _Wrapper).
_SDRUN_NOARG = frozenset(
    {"--user", "--system", "--scope", "--collect", "--wait", "--pipe", "--pty", "--quiet"}
    | {"--no-block", "--remain-after-exit", "--same-dir", "--send-sighup", "--no-ask-password"}
)
_SDRUN_VAL = frozenset(
    {"--unit", "--property", "--description", "--slice", "--setenv", "--uid", "--gid", "--nice"}
    | {"--working-directory", "--service-type"}
)
_SDRUN_SHORT_VAL = {"u": "--unit", "p": "--property", "E": "--setenv"}
_SDRUN_SHORT_NOARG = frozenset("GPtqrd")
# Resource-limit properties only; anything else (ExecStartPre=, EnvironmentFile=,
# WorkingDirectory=, BindPaths=, ...) can change what runs or where.
_SDRUN_PROPERTIES = frozenset(
    {"MemoryMax", "MemoryHigh", "CPUQuota", "CPUWeight", "TasksMax", "Nice", "IOWeight"}
    | {"TimeoutSec", "RuntimeMaxSec"}
)


def _sdrun_value(name: str, value: str) -> str | None:
    """Reason a systemd-run option value is unacceptable, else None."""
    if name == "--property" and value.partition("=")[0] not in _SDRUN_PROPERTIES:
        return f"systemd-run property {value.partition('=')[0]!r} can change what runs"
    return None


def _systemd_run(args: list[str]) -> tuple[str, list[str], str | None, bool]:
    """(status, rest, workdir, in_cwd): `cmd` (rest = wrapped command) or `unpin` (rest = [reason]).

    `in_cwd` is True when the command runs in the caller's cwd (--scope, --same-dir/-d);
    with neither that nor a workdir the command's directory is unknown (the manager's
    default: the user's home, or `/`).
    """
    n = len(args)
    i = 0
    workdir: str | None = None
    same_dir = scope = False
    fail = ("unpin", ["unsupported systemd-run option or no command to pin"], None, False)
    while i < n:
        a = args[i]
        if a == "--":
            i += 1
            break
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if name in _SDRUN_NOARG and not eq:
                same_dir = same_dir or name == "--same-dir"
                scope = scope or name == "--scope"
                i += 1
                continue
            if name not in _SDRUN_VAL or (not eq and i + 1 >= n):
                return fail
            val = val if eq else args[i + 1]
            i += 1 if eq else 2
        elif a.startswith("-") and a != "-":
            flags = a[1:]
            name, val, step = "", "", 1
            for pos, ch in enumerate(flags):
                if ch in _SDRUN_SHORT_NOARG:
                    same_dir = same_dir or ch == "d"
                    continue
                rest = flags[pos + 1 :]
                if ch not in _SDRUN_SHORT_VAL or not (rest or i + 1 < n):
                    return fail
                name, val, step = _SDRUN_SHORT_VAL[ch], rest or args[i + 1], 1 if rest else 2
                break
            i += step
            if not name:
                continue
        else:
            break
        reason = _sdrun_value(name, val)
        if reason:
            return "unpin", [reason], None, False
        if name == "--working-directory":
            workdir = val
    if i >= n or (workdir is not None and same_dir):
        return fail
    return "cmd", args[i:], workdir, same_dir or scope


def _skip_wrapper(w: _Wrapper, args: list[str]) -> tuple[str, list[str]]:
    """(status, rest): cmd (rest = wrapped command), none, unpin, or body (rest = [code])."""
    positional = w.positional
    seen: set[str] = set()
    i = 0
    n = len(args)
    while i < n:
        a = args[i]
        if a == "--":
            i += 1
            break
        if (w.assign and _ASSIGN_RE.match(a)) or a == "-" or re.fullmatch(r"-\d+", a):
            i += 1
        elif a.startswith("--"):
            name, eq, _ = a.partition("=")
            if name in w.long_unpin or not (name in w.long_val or name in w.long_noarg):
                return "unpin", []
            i += 2 if name in w.long_val and not eq else 1
        elif a.startswith("-"):
            flags = a[1:]
            step = 1
            for pos, ch in enumerate(flags):
                rest = flags[pos + 1 :]
                if ch in w.unpin:
                    return "unpin", []
                if ch in w.none_flags:
                    return "none", []
                if w.body_flag and ch == w.body_flag:
                    return "body", [rest or (args[i + 1] if i + 1 < n else "")]
                if ch in w.val:
                    seen.add(ch)
                    step = 1 if rest else 2
                    break
                if ch not in w.noarg:
                    return "unpin", []
            i += step
        else:
            break
    if w.no_positional_with and w.no_positional_with in seen:
        positional = 0
    rest_words = args[i:]
    if len(rest_words) < positional:
        return "none", []
    rest_words = rest_words[positional:]
    return ("cmd", rest_words) if rest_words else ("none", [])


# --- command splitting -----------------------------------------------------------

_PREFIX = frozenset(
    {"{", "!", "if", "then", "elif", "else", "while", "until", "do", "time", "coproc"}
)
_SKIP = frozenset({"for", "case", "select", "fi", "done", "esac", "}"})
_CLOSERS = frozenset({"}", "done", "fi", "esac"})


@dataclass
class _Simple:
    words: list[str] = field(default_factory=list)
    redirs: list[tuple[str, str | None]] = field(default_factory=list)
    piped_from: _Simple | None = None
    pipe_unknown: bool = False  # piped from a group / unidentifiable producer


def _split_commands(tokens: list[str]) -> list[_Simple]:
    """Simple commands with their redirects and pipeline predecessor."""
    out: list[_Simple] = []
    cur = _Simple()
    n = len(tokens)
    i = 0
    parens = 0
    while i < n:
        tok = tokens[i]
        i += 1
        if tok in ("<(", ">("):
            # process substitution: the outer command keeps the marker word, the
            # inner command is analyzed as a command of its own
            cur.words.append(tok)
            out.append(cur)
            cur = _Simple()
            parens += 1
        elif not _is_punct(tok):
            cur.words.append(tok)
        elif "<" in tok or ">" in tok:
            nxt = tokens[i] if i < n and not _is_punct(tokens[i]) else None
            if cur.words and cur.words[-1].isdigit():
                cur.words.pop()  # fd number of `2>file`
            i += 1 if nxt is not None else 0
            is_dup = tok.endswith("&") and nxt is not None and (nxt.isdigit() or nxt == "-")
            cur.redirs.append((tok, None if is_dup else nxt))
        else:
            prev = cur
            nonempty = bool(prev.words or prev.redirs)
            parens += tok.count("(")
            unmatched_close = tok.count(")") > parens
            parens = max(parens - tok.count(")"), 0)
            if nonempty and not unmatched_close:  # an unmatched `)` ends a case pattern
                out.append(prev)
            cur = _Simple()
            if tok in ("|", "|&"):
                if nonempty and not (prev.words and prev.words[0] in _CLOSERS):
                    cur.piped_from = prev
                else:
                    cur.pipe_unknown = True
    if cur.words or cur.redirs:
        out.append(cur)
    return out


def _strip_reserved(words: list[str]) -> list[str]:
    """Drop reserved words / grouping and leading VAR=val; [] when nothing runs."""
    w = list(words)
    while w:
        head = w[0]
        if head in _PREFIX:
            w.pop(0)
            if head == "time":
                while w and w[0] == "-p":
                    w.pop(0)
        elif head in _SKIP:
            return []
        elif head == "function":
            w = w[w.index("{") + 1 :] if "{" in w else []
        elif _ASSIGN_RE.match(head):
            w.pop(0)
        else:
            break
    return w


# --- analysis --------------------------------------------------------------------


@dataclass(frozen=True)
class ScriptRef:
    """A file (or search) the command depends on.

    kind: file (operand is the path), module (dotted python module), search (first
    existing of `names` in directory `operand`), upward (names searched from
    directory `operand` up to the root).
    """

    operand: str
    bases: tuple[str, ...]
    kind: str = "file"
    names: tuple[str, ...] = ()
    required: bool = True  # a missing file makes the approval unusable
    # False for a runner's manifest/makefile (make, pixi run, ...): pinned for one-shot
    # approvals, but template/verb approvals of the runner stay honoured
    script: bool = True


@dataclass
class Plan:
    refs: list[ScriptRef] = field(default_factory=list)
    unpinnable: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def blocks_reuse(self) -> bool:
        """Reusable (template/verb) approvals must not cover this command."""
        return self.unpinnable or any(r.script for r in self.refs)


def _literal(text: str) -> bool:
    """Whether `text` is a statically known path (no expansion applies to it)."""
    if not text or any(c in text for c in _UNRESOLVABLE_CHARS):
        return False
    if text.startswith(("<(", ">(")):
        return False
    return not (text.startswith("~") and text != "~" and not text.startswith("~/"))


def _trusted_name(head: str) -> str | None:
    """Bare command name for `head` (no slash, or inside a trusted bin dir), else None."""
    if "/" not in head:
        return head
    if posixpath.dirname(head) in trusted_bin_dirs():
        return posixpath.basename(head)
    return None


_XARGS_VALUE = frozenset(
    {"-n", "-I", "-P", "-L", "-s", "-d", "-E", "-a", "--max-args", "--replace", "--max-procs"}
    | {"--max-lines", "--delimiter", "--arg-file", "--eof", "--max-chars"}
)
_PIXI_VALUE = frozenset(
    {"--manifest-path", "-e", "--environment", "--color", "--pypi-keyring-provider", "--auth-file"}
    | {"--concurrent-solves", "--concurrent-downloads"}
)
_PIXI_GLOBAL_FLAGS = frozenset(
    {"--verbose", "--quiet", "--no-progress", "--help", "-h", "--version", "-V"}
)


_PIXI_RUN_FLAGS = _PIXI_GLOBAL_FLAGS | {
    "--frozen",
    "--locked",
    "--no-lockfile-update",
    "--no-install",
    "--clean-env",
}


def _pixi_unknown_global(args: list[str], flags: frozenset[str] = _PIXI_GLOBAL_FLAGS) -> bool:
    """Whether an option before the pixi subcommand/task is unknown (so it could be misparsed)."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--" or not a.startswith("-") or a == "-":
            return False
        name, eq, _ = a.partition("=")
        if name in _PIXI_VALUE:
            i += 1 if eq else 2
        elif name in flags or re.fullmatch(r"-(v+|q+)", a):
            i += 1
        else:
            return True
    return False


_UV_VALUE = frozenset(
    {"--directory", "--project", "--with", "--with-editable", "--with-requirements", "--python"}
    | {"-p", "--package", "--env-file", "--index", "--default-index", "--index-url", "-i"}
    | {"--extra-index-url", "--group", "--extra", "--no-group", "--exclude-newer", "--script"}
    | {"--find-links", "-f", "--cache-dir", "--config-file", "--color", "--python-preference"}
    | {"--refresh-package", "--reinstall-package", "--upgrade-package", "-P"}
)
_NPM_VALUE = frozenset({"--prefix", "-w", "--workspace", "--userconfig", "--registry", "--cache"})
_NPM_RUN = frozenset(
    {"run", "run-script", "rum", "urn", "test", "t", "tst", "start", "stop", "restart"}
)
_PM_VALUE = frozenset({"--cwd", "--dir", "-C", "--prefix", "--filter", "-F", "--registry"})
_PM_NON_RUN = frozenset(
    {"install", "i", "add", "remove", "rm", "init", "config", "cache", "info", "why", "list"}
    | {"ls", "upgrade", "up", "link", "unlink", "pack", "publish", "version", "versions"}
    | {"outdated", "audit", "global", "workspaces", "login", "logout", "owner", "team", "tag"}
    | {"bin", "autoclean", "check", "import", "licenses", "node", "set", "plugin", "dlx", "exec"}
    | {"create", "dedupe", "prune", "fetch", "rebuild", "store", "update", "env", "patch", "root"}
)


_PM_EXEC = frozenset({"exec", "dlx", "x", "node"})
_PM_RUN_BIN = frozenset({"run", "run-script", "rum", "urn"})
_EXEC_VALUE = frozenset(
    {"-p", "--package", "-w", "--workspace", "--prefix", "--registry", "--cache", "--userconfig"}
)
# Heads that only read/transform data or manage packages: an interpreter name among their
# arguments is data, not a command to run (rule 7: unknown heads fail closed otherwise).
_DATA_HEADS = frozenset(
    {"echo", "printf", "cat", "grep", "egrep", "fgrep", "rg", "ls", "which", "type", "whereis"}
    | {"man", "file", "stat", "wc", "head", "tail", "diff", "cmp", "sort", "uniq", "cut", "tr"}
    | {"jq", "less", "more", "tee", "test", "[", "[[", "read", "true", "false", "pwd", "date"}
    | {"git", "pip", "pip3", "conda", "mamba", "apt", "apt-get", "dpkg", "brew", "cargo", "gh"}
    | {"rm", "cp", "mv", "mkdir", "rmdir", "touch", "chmod", "chown", "ln", "kill", "pkill"}
    | {"pgrep", "ps", "mkfifo", "tar", "curl", "wget", "export", "unset", "set", "declare"}
    | {"local", "readonly", "typeset", "return", "exit", "sleep", "id", "whoami", "hostname"}
    | {"uname", "df", "du", "basename", "dirname", "realpath", "readlink", "printenv", "tput"}
)
_RUNNER_NAMES = frozenset(
    {"awk", "gawk", "mawk", "nawk", "sed", "make", "pixi", "uv", "npm", "npx", "bunx", "pnpx"}
    | {"yarn", "pnpm", "find", "xargs", "eval", "source", "."}
)


# Unlisted heads that run another command (or a command string) given as an argument.
_LAUNCHER_HEADS = frozenset(
    {"tmux", "screen", "script", "watch", "parallel", "ssh", "su", "runuser", "chroot", "unshare"}
    | {"nsenter", "firejail", "bwrap", "dbus-run-session", "xvfb-run", "strace", "ltrace"}
    | {"fakeroot", "busybox", "trap", "alias", "at", "batch"}
)


def _command_flag_values(args: list[str]) -> list[str]:
    """Values of `-c X` / `--command X` / `--command=X` arguments."""
    out: list[str] = []
    for i, a in enumerate(args):
        if a in ("-c", "--command") and i + 1 < len(args):
            out.append(args[i + 1])
        elif a.startswith("--command="):
            out.append(a.split("=", 1)[1])
    return out


def _names_launcher(word: str) -> bool:
    """Whether `word` (or the value of `NAME=value`) is an interpreter/runner/wrapper name."""
    candidates = [word, word.split("=", 1)[1]] if "=" in word else [word]
    for cand in candidates:
        if cand == ".":
            continue  # only the source builtin as a command head; as an argument it is a path
        name = posixpath.basename(cand)
        if _family(name) is not None or name in _RUNNER_NAMES or name in _WRAPPERS:
            return True
    return False


def _split_runner(
    args: list[str], value_flags: frozenset[str], collect: frozenset[str]
) -> tuple[str | None, list[str], list[tuple[str, str]]]:
    """(subcommand, args after it, collected (flag, value) pairs) for a runner CLI."""
    got: list[tuple[str, str]] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if not a.startswith("-") or a == "-":
            break
        name, eq, val = a.partition("=")
        if name in collect or name in value_flags:
            value = val if eq else (args[i + 1] if i + 1 < len(args) else "")
            if name in collect:
                got.append((name, value))
            i += 1 if eq else 2
        else:
            i += 1
    if i >= len(args):
        return None, [], got
    return args[i], args[i + 1 :], got


def _prog_files(args: list[str], value_flags: str, attached_flags: str = "") -> list[str]:
    """Program files named by `-f FILE` / `--file=FILE` of awk / sed."""
    files: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            break
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if name == "--file":
                files += [val] if eq else args[i + 1 : i + 2]
                i += 1 if eq else 2
            else:
                i += 1
            continue
        if not a.startswith("-") or a == "-":
            i += 1
            continue
        step = 1
        for pos, ch in enumerate(a[1:]):
            rest = a[pos + 2 :]
            if ch == "f":
                files += [rest] if rest else args[i + 1 : i + 2]
                step = 1 if rest else 2
                break
            if ch in value_flags:
                step = 1 if rest else 2
                break
            if ch in attached_flags:
                break
        i += step
    return files


def _make_options(args: list[str]) -> tuple[list[str], list[str]]:
    """(makefiles from -f/--file/--makefile, directories from -C/--directory) of a make call."""
    files: list[str] = []
    dirs: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            break
        step, key, val = 1, "", ""
        if a in ("-f", "--file", "--makefile", "-C", "--directory"):
            key = "C" if a in ("-C", "--directory") else "f"
            val, step = (args[i + 1] if i + 1 < len(args) else ""), 2
        elif a.startswith(("--file=", "--makefile=", "--directory=")):
            key = "C" if a.startswith("--directory=") else "f"
            val = a.split("=", 1)[1]
        elif a.startswith("-") and not a.startswith("--") and len(a) > 1:
            for pos, ch in enumerate(a[1:]):
                rest = a[pos + 2 :]
                if ch in "fC":
                    key, val = ch, rest or (args[i + 1] if i + 1 < len(args) else "")
                    step = 1 if rest else 2
                    break
                if ch in "Ilow":
                    step = 1 if rest else 2
                    break
        if key == "C":
            dirs.append(val)
        elif key == "f":
            files.append(val)
        i += step
    return files, dirs


class _Analyzer:
    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.plan = Plan()
        self.bases: list[str] = []
        self.base_unknown = False
        self.text = ""  # raw text of the command string being analyzed
        # (bases, base_unknown) to restore after a systemd-run unwrap
        self.wd_saved: tuple[list[str], bool] | None = None

    # -- helpers --
    def unpin(self, reason: str) -> None:
        self.plan.unpinnable = True
        self.plan.reasons.append(reason)

    def emit(self, ref: ScriptRef) -> None:
        if self.base_unknown and not ref.operand.startswith(("/", "~")):
            self.unpin("script path depends on a cd/pushd target that is not statically known")
        else:
            self.plan.refs.append(ref)

    def add_file(self, operand: str, *, script: bool = True) -> None:
        if not _literal(operand):
            self.unpin(f"script path {operand!r} is not a literal path")
            return
        self.emit(ScriptRef(operand, tuple(self.bases), script=script))

    def _ref(self, operand: str, kind: str, names: tuple[str, ...]) -> None:
        """A runner's manifest/makefile search (pinned, but not a script run)."""
        self.emit(ScriptRef(operand, tuple(self.bases), kind, names, required=False, script=False))

    # -- entry points --
    def run(self, text: str, depth: int) -> None:
        if depth > _MAX_DEPTH:
            self.unpin("nested -c bodies too deep")
            return
        tokens = _tokenize(text)
        if tokens is None:
            if _SUSPECT_RE.search(text):
                self.unpin("command could not be parsed")
            return
        saved, self.text = self.text, text
        try:
            for cmd in _split_commands(tokens):
                words = _strip_reserved(cmd.words)
                if words:
                    self.command(words, cmd, depth)
        finally:
            self.text = saved

    def probe(self, text: str, depth: int) -> bool:
        """Whether `text`, parsed as a command string, runs a script (or is unpinnable)."""
        if depth + 1 > _MAX_DEPTH:
            return False
        saved, self.plan = self.plan, Plan()
        try:
            self.run(text, depth + 1)
            return bool(self.plan.refs or self.plan.unpinnable)
        finally:
            self.plan = saved

    def unwrap(self, words: list[str], depth: int) -> list[str] | None:
        """Strip wrapper commands; None when no command remains (or it was flagged).

        `systemd-run` changes the directory the wrapped command runs in: the bases (and
        base-unknown flag) to restore afterwards are left in `self.wd_saved` (None: unchanged).
        """
        undo: list[tuple[list[str], bool]] = []
        self.wd_saved = None
        result = self._unwrap(words, depth, undo)
        if undo and result is None:
            self.bases, self.base_unknown = undo[0]
        elif undo:
            self.wd_saved = undo[0]
        return result

    def _systemd_run(
        self, words: list[str], undo: list[tuple[list[str], bool]]
    ) -> list[str] | None:
        status, rest, workdir, in_cwd = _systemd_run(words[1:])
        if status == "unpin":
            self.unpin(rest[0])
            return None
        if workdir is not None and not _literal(workdir):
            self.unpin("systemd-run --working-directory is not a literal path")
            return None
        if workdir is None and in_cwd:
            return rest
        if not undo:
            undo.append((list(self.bases), self.base_unknown))
        if workdir is not None:
            self.bases = _extend_bases(self.bases, workdir)
        else:  # the service's default directory (home or /) is not guessed
            self.base_unknown = True
        return rest

    def _unwrap(
        self, words: list[str], depth: int, undo: list[tuple[list[str], bool]]
    ) -> list[str] | None:
        for _ in range(_MAX_WRAP):
            name = _trusted_name(words[0])
            if name == "systemd-run":
                inner = self._systemd_run(words, undo)
                words = _strip_reserved(inner) if inner else []
                if not words:
                    return None
                continue
            spec = _WRAPPERS.get(name) if name else None
            if spec is None:
                return words
            status, rest = _skip_wrapper(spec, words[1:])
            if status == "unpin":
                self.unpin(f"cannot parse the options of {name}")
                return None
            if status == "body":
                self.run(rest[0], depth + 1)
                return None
            if status == "none":
                return None
            words = _strip_reserved(rest)
            if not words:
                return None
        self.unpin("wrapper chain too long")
        return None

    def command(self, words: list[str], cmd: _Simple, depth: int) -> None:
        unwrapped = self.unwrap(words, depth)
        if unwrapped is None:
            return
        saved = self.wd_saved
        try:
            self._command(unwrapped, cmd, depth)
        finally:
            if saved is not None:
                self.bases, self.base_unknown = saved

    def _command(self, unwrapped: list[str], cmd: _Simple, depth: int) -> None:
        head, args = unwrapped[0], unwrapped[1:]
        name = _trusted_name(head)
        if head in ("<(", ">("):
            return  # leftover marker of a process substitution; its inner command is separate
        if not _literal(head) and head not in ("[", "[["):
            self.unpin(f"command word {head!r} cannot be resolved statically")
        elif name in ("cd", "pushd"):
            self.cd(args)
        elif name == "popd":
            self.base_unknown = True
        elif name == "eval":
            self.unpin("eval runs code that cannot be pinned")
        elif name in (".", "source"):
            self.source(args)
        else:
            if "/" in head and posixpath.dirname(head) not in trusted_bin_dirs():
                self.add_file(head)
            self.dispatch(posixpath.basename(head), args, cmd, depth)

    def dispatch(self, base: str, args: list[str], cmd: _Simple, depth: int) -> None:
        spec = _family(base)
        if spec is not None:
            self.interpreter(spec, args, cmd, depth)
        elif base in ("awk", "gawk", "mawk", "nawk"):
            for f in _prog_files(args, "Fve"):
                self.add_file(f)
        elif base == "sed":
            for f in _prog_files(args, "el", "i"):
                self.add_file(f)
        elif base == "make":
            self.make(args)
        elif base == "find":
            self.find(args, depth)
        elif base == "xargs":
            self.xargs(args, depth)
        elif base in ("pixi", "uv", "npm", "yarn", "pnpm"):
            self.runner(base, args, depth)
        elif base in ("npx", "bunx", "pnpx"):
            self.pm_inner("exec", args, depth)
        elif base not in _DATA_HEADS:
            self.unknown_head(base, args, depth)

    def unknown_head(self, base: str, args: list[str], depth: int) -> None:
        """Fail closed for an unlisted wrapper/launcher (strace, watch, ssh, trap, alias, ...).

        A known launcher head is flagged when one of its arguments names an
        interpreter/runner/wrapper, or is itself a command string that runs a script
        (`su -c 'bash x.sh'`). Any other unlisted head is only flagged when a `-c` /
        `--command` value is a command string that runs a script (`mytool python` is an
        accepted residual gap).
        """
        if base in _LAUNCHER_HEADS:
            if any(_names_launcher(a) for a in args):
                self.unpin("unlisted command launches an interpreter or runner")
                return
            strings = [s for a in args for s in ([a, a.split("=", 1)[1]] if "=" in a else [a])]
        else:
            strings = _command_flag_values(args)
        if any(any(c.isspace() for c in s) and self.probe(s, depth) for s in strings):
            self.unpin("unlisted command takes a command string that runs a script")

    def cd(self, args: list[str]) -> None:
        target = _cd_directory(args) if args else "~"
        if target is None or not _literal(target):
            self.base_unknown = True
            return
        self.bases = _extend_bases(self.bases, target)

    def source(self, args: list[str]) -> None:
        if not args:
            return
        if "/" not in args[0]:
            self.unpin("`. name` searches PATH first; use a path with a slash")
            return
        self.add_file(args[0])

    def interpreter(self, spec: _Interp, args: list[str], cmd: _Simple, depth: int) -> None:
        # prepare() turns heredoc / here-string openers into sentinel words
        heredoc = any(a.startswith(SENTINEL_PREFIX) for a in args)
        args = [a for a in args if not a.startswith(SENTINEL_PREFIX)]
        kind, operand = _scan_options(spec, args)
        if heredoc and kind in ("stdin", "stdin!"):
            if "$" in self.text or "`" in self.text:
                self.unpin("heredoc/here-string feeding an interpreter expands $ or backticks")
            return
        if kind == "file" and operand is not None:
            self.add_file(operand)
        elif kind == "inline" and operand is not None:
            if "$(" in operand or "`" in operand:
                self.unpin("inline code is assembled by a command substitution")
            elif spec.shell:
                self.run(operand, depth + 1)
        elif kind == "module" and operand is not None:
            self.module(operand)
        elif kind in ("stdin", "stdin!"):
            self.stdin(cmd, explicit=kind == "stdin!", depth=depth)

    def module(self, name: str) -> None:
        if not re.fullmatch(r"[A-Za-z_][\w.]*", name):
            self.unpin(f"python module {name!r} is not a literal name")
            return
        if self.base_unknown:
            self.unpin("module lookup depends on an unknown cd target")
            return
        ref = ScriptRef(name, tuple(self.bases), "module", required=False, script=False)
        # Candidates are always pinned (a module created later is a mismatch); only a
        # module that exists locally counts as running a script (non-promotable).
        if any(os.path.exists(p) for p in _ref_paths(ref, self.cwd)):
            ref = ScriptRef(name, tuple(self.bases), "module", required=False)
        self.plan.refs.append(ref)

    def stdin(self, cmd: _Simple, *, explicit: bool, depth: int) -> None:
        for op, target in cmd.redirs:
            if op in ("<", "<>"):
                if target is None:
                    self.unpin("stdin redirect has no literal target")
                else:
                    self.add_file(target)
                return
            if op.startswith("<<"):
                # heredoc / here-string: the content is in the command text, unless
                # a substitution or variable assembles it
                if "$" in self.text or "`" in self.text:
                    self.unpin("heredoc/here-string feeding an interpreter expands $ or backticks")
                return
            if op == "<&":
                self.unpin("stdin comes from a file descriptor")
                return
        if cmd.pipe_unknown:
            self.unpin("interpreter reads a script from an unidentifiable pipeline producer")
        elif cmd.piped_from is not None:
            self.producer(cmd.piped_from, depth)
        elif explicit:
            self.unpin("interpreter reads a script from stdin of unknown origin")

    def producer(self, src: _Simple, depth: int) -> None:
        words = _strip_reserved(src.words)
        unwrapped = self.unwrap(words, depth) if words else None
        if unwrapped is None:
            self.unpin("pipeline producer cannot be identified")
            return
        saved = self.wd_saved
        try:
            self._producer(src, unwrapped)
        finally:
            if saved is not None:
                self.bases, self.base_unknown = saved

    def _producer(self, src: _Simple, unwrapped: list[str]) -> None:
        name = posixpath.basename(unwrapped[0])
        args = unwrapped[1:]
        if name in ("echo", "printf"):
            if any("$" in a or "`" in a for a in args):
                self.unpin("script text piped into an interpreter is not literal")
            return
        if name != "cat":
            self.unpin(f"{name} output is piped into an interpreter")
            return
        files = [a for a in args if not a.startswith("-") or a == "-"]
        files += [t for op, t in src.redirs if op == "<" and t is not None]
        if not files or "-" in files:
            self.unpin("cat without literal files feeds an interpreter")
            return
        for f in files:
            self.add_file(f)

    def make(self, args: list[str]) -> None:
        files, dirs = _make_options(args)
        directory = ""
        for d in dirs:
            if not _literal(d):
                self.unpin("make -C directory is not a literal path")
                return
            directory = posixpath.join(directory, d) if directory else d
        if not files:
            self._ref(directory or ".", "search", _MAKEFILES)
        for f in files:
            self.add_file(
                posixpath.join(directory, f) if directory and _literal(f) else f, script=False
            )

    def find(self, args: list[str], depth: int) -> None:
        for i, a in enumerate(args):
            if a in ("-execdir", "-okdir"):
                self.unpin("find -execdir runs the command in each match's directory")
            elif a in ("-exec", "-ok"):
                inner: list[str] = []
                for w in args[i + 1 :]:
                    if w in (";", "+"):
                        break
                    inner.append(w)
                inner = _strip_reserved(inner)
                if inner:
                    self.command(inner, _Simple(list(inner)), depth)

    def xargs(self, args: list[str], depth: int) -> None:
        sub, rest, _ = _split_runner(args, _XARGS_VALUE, frozenset())
        if sub is None:
            return
        inner = [sub, *rest]
        saved, self.plan = self.plan, Plan()
        try:
            self.command(inner, _Simple(list(inner)), depth)
            fed = bool(self.plan.refs or self.plan.unpinnable)
        finally:
            self.plan = saved
        is_direct = "/" in sub and posixpath.dirname(sub) not in trusted_bin_dirs()
        if fed or is_direct or _family(posixpath.basename(sub)) is not None:
            self.unpin("xargs feeds arguments to a script or interpreter")

    def manifest(self, values: list[str], names: tuple[str, ...]) -> None:
        if not values:
            self._ref(".", "upward", names)
        for v in values:
            if not _literal(v):
                self.unpin("manifest/project directory is not a literal path")
            elif v.endswith((".toml", ".json")):
                self.add_file(v, script=False)
            else:
                self._ref(v, "search", names)

    def runner(self, tool: str, args: list[str], depth: int) -> None:
        if tool == "pixi":
            if _pixi_unknown_global(args):
                self.unpin("unrecognised pixi option before the subcommand")
                return
            sub, rest, _ = _split_runner(args, _PIXI_VALUE, frozenset())
            if sub not in ("run", "r"):
                return
            if _pixi_unknown_global(rest, _PIXI_RUN_FLAGS):
                self.unpin("unrecognised pixi run option before the task name")
                return
            task, rest2, got = _split_runner(rest, _PIXI_VALUE, frozenset({"--manifest-path"}))
            self.manifest([v for _, v in got], ("pixi.toml", "pyproject.toml"))
            if task is not None:
                self.command([task, *rest2], _Simple([task, *rest2]), depth)
        elif tool == "uv":
            self.uv(args, depth)
        else:
            self.node_pm(tool, args, depth)

    def uv(self, args: list[str], depth: int) -> None:
        collect = frozenset({"--directory", "--project", "--script"})
        sub, rest, got = _split_runner(args, _UV_VALUE, collect)
        if sub != "run":
            return
        task, rest2, got2 = _split_runner(rest, _UV_VALUE, collect)
        got += got2
        dirs = [v for k, v in got if k in ("--directory", "--project")]
        saved = list(self.bases)
        directory = next((v for k, v in reversed(got) if k == "--directory"), None)
        if directory is not None and _literal(directory):
            self.bases = _extend_bases(self.bases, directory)
        try:
            self.manifest(dirs, ("pyproject.toml",))
            for k, v in got:
                if k == "--script":
                    self.add_file(v)
            if task is not None and task.endswith(".py"):
                self.add_file(task)
            elif task is not None:
                self.command([task, *rest2], _Simple([task, *rest2]), depth)
        finally:
            self.bases = saved

    def node_pm(self, tool: str, args: list[str], depth: int) -> None:
        collect = frozenset({"--prefix", "--cwd", "--dir", "-C"})
        value = _NPM_VALUE if tool == "npm" else _PM_VALUE
        sub, rest, got = _split_runner(args, value, collect)
        if sub is None:
            return
        runs = sub in _NPM_RUN if tool == "npm" else sub not in _PM_NON_RUN
        if runs:
            self.manifest([v for _, v in got], ("package.json",))
        if sub in _PM_EXEC or (tool != "npm" and runs) or sub in _PM_RUN_BIN:
            self.pm_inner(sub, rest, depth)

    def pm_inner(self, sub: str, rest: list[str], depth: int) -> None:
        """Recurse into the trailing command of `npm exec`, `pnpm dlx`, `npx`, `yarn run`, ..."""
        if sub == "node":
            inner = ["node", *rest]
        elif sub in _PM_EXEC or sub in _PM_RUN_BIN:
            cmd, crest, got = _split_runner(rest, _EXEC_VALUE, frozenset({"-c", "--call"}))
            for _, body in got:
                self.run(body, depth + 1)
            inner = [cmd, *crest] if cmd else []
        else:  # yarn/pnpm <script-or-bin> args
            inner = [sub, *rest]
        if inner:
            self.command(inner, _Simple(list(inner)), depth)


def analyze(command: str, cwd: str) -> Plan:
    """Which script files `command` runs (and whether that can be pinned)."""
    analyzer = _Analyzer(cwd)
    analyzer.run(command, 0)
    return analyzer.plan


def runs_script(command: str, cwd: str) -> bool:
    """Whether `command` runs a script file, or something unpinnable (issue #95).

    A runner's manifest alone (make, pixi run, ...) does not count: those stay promotable.
    """
    return analyze(command, cwd).blocks_reuse


# --- hashing ---------------------------------------------------------------------


def _ref_paths(ref: ScriptRef, cwd: str) -> list[str]:
    """Realpaths a ref may refer to: under the cwd and under every cd base."""
    roots = [cwd, *(os.path.join(cwd, os.path.expanduser(b)) for b in ref.bases)]
    out: set[str] = set()
    for root in roots:
        target = os.path.join(root, os.path.expanduser(ref.operand))
        if ref.kind == "file":
            out.add(os.path.realpath(target))
        elif ref.kind == "module":
            base = os.path.join(root, *ref.operand.split("."))
            for cand in (f"{base}.py", f"{base}/__main__.py", f"{base}/__init__.py"):
                out.add(os.path.realpath(cand))
        elif ref.kind == "search":
            directory = os.path.realpath(target)
            present = [n for n in ref.names if os.path.isfile(os.path.join(directory, n))]
            out.update(os.path.join(directory, n) for n in (present[:1] or ref.names))
        else:  # upward
            cur = os.path.realpath(target)
            while True:
                found = [n for n in ref.names if os.path.isfile(os.path.join(cur, n))]
                if found or os.path.dirname(cur) == cur:
                    out.update(os.path.join(cur, n) for n in found)
                    break
                cur = os.path.dirname(cur)
    return sorted(out)


def _file_sha256(path: str, budget: list[int]) -> str:
    """sha256 of a regular file, MISSING, or UNREADABLE (FIFO, device, directory, huge).

    `budget` is the remaining total bytes for the command; going over it gives _OVER_BUDGET.
    """
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return MISSING
    except (OSError, ValueError):  # ValueError: embedded NUL
        return UNREADABLE
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_SCRIPT_BYTES:
        return UNREADABLE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except (FileNotFoundError, NotADirectoryError):
        return MISSING
    except OSError:
        return UNREADABLE
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_SCRIPT_BYTES:
            return UNREADABLE
        budget[0] -= opened.st_size
        if budget[0] < 0:
            return _OVER_BUDGET
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(fd, 1 << 16):
            total += len(chunk)
            if total > MAX_SCRIPT_BYTES:
                return UNREADABLE
            digest.update(chunk)
    except OSError:
        return UNREADABLE
    finally:
        os.close(fd)
    return digest.hexdigest()


@dataclass(frozen=True)
class ScriptPin:
    """Content pin: `paths` maps each candidate realpath to its sha256 / MISSING / UNREADABLE."""

    sha256: str
    paths: dict[str, str]
    ok: bool = True  # every required script exists and was read

    @property
    def paths_json(self) -> str:
        return json.dumps(self.paths, sort_keys=True)

    @property
    def unpinnable(self) -> bool:
        return self.sha256 == UNPINNABLE

    @property
    def storage_sha(self) -> str:
        """What to store on the prompt: a script that is not a readable regular file (a
        directory, FIFO, device, oversized) can never run, so it is stored as UNPINNABLE
        and the prompt is refused every approval."""
        return UNPINNABLE if UNREADABLE in self.paths.values() else self.sha256


def compute_pin(command: str, cwd: str) -> ScriptPin | None:
    """Pin the script(s) `command` runs; None if it runs none.

    An unpinnable command gets the UNPINNABLE sentinel (it never matches). One script
    at one path: sha256 is that file's hex digest. Otherwise it is a digest over the
    sorted (realpath, sha256) pairs, so a missing path that is created later changes it.
    """
    plan = analyze(command, cwd)
    if plan.unpinnable:
        return ScriptPin(UNPINNABLE, {}, ok=False)
    if not plan.refs:
        return None
    paths: dict[str, str] = {}
    budget = [MAX_TOTAL_BYTES]
    ok = True
    for ref in plan.refs:
        candidates = _ref_paths(ref, cwd)
        for path in candidates:
            if path not in paths:
                if len(paths) >= MAX_FILES:
                    return ScriptPin(UNPINNABLE, {}, ok=False)
                paths[path] = _file_sha256(path, budget)
        if ref.required and all(paths[p] in _UNHASHED for p in candidates):
            ok = False
    if _OVER_BUDGET in paths.values():
        return ScriptPin(UNPINNABLE, {}, ok=False)
    if not paths:
        return None
    only = next(iter(paths.values()))
    if len(paths) == 1 and only not in _UNHASHED:
        return ScriptPin(sha256=only, paths=paths, ok=ok)
    combined = hashlib.sha256(json.dumps(sorted(paths.items())).encode()).hexdigest()
    return ScriptPin(sha256=combined, paths=paths, ok=ok)


def pin_unchanged(command: str, cwd: str, stored_sha256: str | None) -> bool:
    """Whether the command still matches the pin stored on its approved prompt.

    A prompt stored without a pin (NULL) expects the command to still have nothing to
    pin; a manifest or module that has appeared since then counts as a change.
    """
    if not stored_sha256:
        return compute_pin(command, cwd) is None
    return pin_matches(command, cwd, stored_sha256)


def pin_matches(command: str, cwd: str, expected_sha256: str) -> bool:
    """Recompute the pin; False on any change, an unreadable script, or an unpinnable command."""
    if expected_sha256 == UNPINNABLE:
        return False
    pin = compute_pin(command, cwd)
    return pin is not None and not pin.unpinnable and pin.ok and pin.sha256 == expected_sha256
