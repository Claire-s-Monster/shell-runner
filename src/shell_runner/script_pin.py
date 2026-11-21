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

import ast
import hashlib
import json
import os
import posixpath
import re
import stat
import warnings
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
MAX_FILES = 32  # existing files hashed per command; over budget the command is unpinnable
MAX_MISSING_FILES = 256  # absent candidates pinned as MISSING per command (bounds stat work)
_UNRESOLVABLE_CHARS = "$`*?[{\n\0"
_OVER_BUDGET = "over_budget"
_MAKEFILES = ("GNUmakefile", "makefile", "Makefile")
_SUSPECT_RE = re.compile(
    r"\b(?:bash|sh|zsh|dash|ksh|mksh|ash|fish|python[\d.]*|pypy[\d.]*|node|perl|ruby|php|awk"
    r"|gawk|mawk|nawk|sed|make|pixi|uv|npm|yarn|pnpm|find|eval|source|xargs|csh|tcsh|lua[\d.]*"
    r"|luajit|Rscript|tclsh[\d.]*|wish|deno|bun|osascript|go)\b|^\s*\.\s"
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
    preload_flags: str = ""  # short flags whose value is a file loaded before the script
    long_preload_flags: frozenset[str] = frozenset()
    include_flags: str = ""  # short flags whose value is a module search directory
    # inline code (-c/-e) is scanned for local loads: python | node | ruby | lua | r | php | deno
    lang: str = ""
    chdir_flags: str = ""  # short flags whose value is the directory the script runs in
    pragma_flags: str = ""  # short flags whose attached value is `use` code (perl -M/-m)
    # options are parsed after the -e code (node, perl, ruby); python's -c ends option parsing
    options_after_code: bool = False
    # short flags whose value is code, repeatable, with the operand still a script (lua -e)
    code_flags: str = ""
    code_then_script: bool = False  # an operand after code_flags is still a script (lua)
    config_flags: str = ""  # short flags whose value is a config file (php -c)
    define_flags: str = ""  # short flags whose value is NAME=VALUE (php -d)
    opaque_inline: bool = False  # inline code is another language: unpinnable (csh -c)
    unpin_options: bool = False  # option grammar not modelled: any leading option is unpinnable
    cli: str = ""  # has subcommands handled by _Analyzer: bun | deno
    implicit_files: tuple[str, ...] = ()  # cwd files the interpreter loads on its own
    # (implicit file, long options that make the interpreter skip it)
    implicit_off: tuple[tuple[str, frozenset[str]], ...] = ()
    unsafe_flags: str = ""  # short flags whose value makes the command unpinnable (php -z)

    def collects(self, ch: str) -> bool:
        """Whether short flag `ch` carries a value that _option_values records."""
        return any(
            ch in group
            for group in (
                self.preload_flags,
                self.include_flags,
                self.chdir_flags,
                self.code_flags,
                self.config_flags,
                self.define_flags,
                self.unsafe_flags,
            )
        )


_SHELL = _Interp(
    value="oO",
    inline="c",
    noexec="n",
    stdin="s",
    long_value=frozenset({"--rcfile", "--init-file"}),
    shell=True,
)
_PYTHON = _Interp(
    value="WX",
    inline="c",
    module="m",
    long_value=frozenset({"--check-hash-based-pycs"}),
    lang="python",
)
_NODE = _Interp(
    value="r",
    inline="ep",
    long_value=frozenset(
        {"--require", "--import", "--loader", "--experimental-loader", "--input-type"}
    ),
    long_inline=frozenset({"--eval", "--print"}),
    preload_flags="r",
    long_preload_flags=frozenset({"--require", "--import", "--loader", "--experimental-loader"}),
    lang="node",
    options_after_code=True,
)
_PERL = _Interp(
    value="I",
    attached="MmixCFdDV",
    inline="eE",
    include_flags="I",
    pragma_flags="Mm",
    options_after_code=True,
)
_RUBY = _Interp(
    value="rICEX",
    attached="ixKTWF",
    inline="e",
    preload_flags="r",
    include_flags="I",
    chdir_flags="CX",
    lang="ruby",
    options_after_code=True,
)
_PHP = _Interp(
    value="dczSt",
    inline="rBRE",
    noexec="lvimh",
    file_flags="fF",
    config_flags="c",
    define_flags="d",
    unsafe_flags="z",
    lang="php",
)
_FISH = _Interp(value="dDoC", inline="c", noexec="n")
_CSH = _Interp(inline="c", noexec="n", stdin="st", opaque_inline=True)
_LUA = _Interp(value="l", code_flags="e", code_then_script=True, preload_flags="l", lang="lua")
_RSCRIPT = _Interp(
    code_flags="e",
    lang="r",
    implicit_files=(".Rprofile", ".Renviron"),
    implicit_off=(
        (".Rprofile", frozenset({"--vanilla", "--no-init-file"})),
        (".Renviron", frozenset({"--vanilla", "--no-environ"})),
    ),
)
_BUN_LONG_VALUE = frozenset(
    {"--preload", "--require", "--import", "--define", "--loader", "--port", "--shell"}
    | {"--cwd", "--config", "--env-file", "--tsconfig-override", "--install", "--conditions"}
)
_BUN = _Interp(
    value="rdlc",
    inline="ep",
    long_value=_BUN_LONG_VALUE,
    long_inline=frozenset({"--eval", "--print"}),
    preload_flags="r",
    long_preload_flags=frozenset({"--preload", "--require", "--import"}),
    lang="node",
    options_after_code=True,
    cli="bun",
    implicit_files=("bunfig.toml",),
)
_DENO = _Interp(cli="deno", lang="deno")
_OPAQUE = _Interp(unpin_options=True)  # tclsh, wish, osascript: file operand only
_FAMILIES = {
    "node": _NODE,
    "nodejs": _NODE,
    "perl": _PERL,
    "ruby": _RUBY,
    "php": _PHP,
    "fish": _FISH,
    "csh": _CSH,
    "tcsh": _CSH,
    "lua": _LUA,
    "luajit": _LUA,
    "Rscript": _RSCRIPT,
    "deno": _DENO,
    "bun": _BUN,
    "osascript": _OPAQUE,
}
_SHELL_RE = re.compile(r"^(?:bash|sh|zsh|dash|ksh|ksh93|mksh|ash|rbash)$")
_PYTHON_RE = re.compile(r"^(?:python|pypy)[\d.]*$")
_VERSIONED_RE = re.compile(r"^(?:perl5|ruby\d|php\d|lua\d)(?:\.\d+)*$")
_TCL_RE = re.compile(r"^(?:tclsh|wish)[\d.]*$")


def _family(name: str) -> _Interp | None:
    if _SHELL_RE.match(name):
        return _SHELL
    if _PYTHON_RE.match(name):
        return _PYTHON
    if _VERSIONED_RE.match(name):
        return _FAMILIES[name.rstrip("0123456789.")]
    if _TCL_RE.match(name):
        return _OPAQUE
    return _FAMILIES.get(name)


def _scan_options(spec: _Interp, args: list[str]) -> tuple[str, str | None]:
    """Classify an interpreter invocation.

    Returns (kind, operand): file | inline | module | stdin | stdin! (explicitly
    requested) | none (nothing runs); operand is the file / code / module name.
    """
    inline = module = noexec = stdin = code = False
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
                if ch in spec.code_flags:
                    code = True  # the code is collected by _option_values; skip its value
                    step = 1 if rest else 2
                    break
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
        if noexec or (code and not spec.code_then_script):
            return "none", None
        return ("stdin!", None) if stdin else ("file", operand)
    if inline or module or noexec or code:
        return "none", None
    return ("stdin!", None) if stdin else ("stdin", None)


@dataclass
class _Options:
    """What the interpreter options name: preload files, include dirs, chdir targets."""

    preload: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    chdir: list[str] = field(default_factory=list)
    code: list[str] = field(default_factory=list)  # lua/Rscript -e code words
    files: list[str] = field(default_factory=list)  # files named by ini settings (php -d)
    configs: list[str] = field(default_factory=list)  # config files (php -c)
    flags: list[str] = field(default_factory=list)  # long option names seen
    ini_search: list[str] = field(default_factory=list)  # php -d include_path=
    unsafe: list[str] = field(default_factory=list)  # reasons the options are unpinnable


_LIB_PRAGMA_RE = re.compile(r"lib(?:=|\s)")
_PHP_FILE_SETTINGS = frozenset({"auto_prepend_file", "auto_append_file"})
_PHP_EXTENSION_SETTINGS = frozenset({"extension", "zend_extension"})


def _php_define(value: str) -> tuple[str, str]:
    """(name, value) of a php `-d NAME=VALUE`: whitespace and one pair of quotes removed."""
    name, _, setting = value.partition("=")
    setting = setting.strip()
    if len(setting) >= 2 and setting[0] == setting[-1] and setting[0] in "'\"":
        setting = setting[1:-1].strip()
    return name.strip().lower(), setting


def _record_value(spec: _Interp, ch: str, value: str, out: _Options) -> None:
    """File `value` of short flag `ch` into the matching _Options list."""
    if ch in spec.code_flags:
        out.code.append(value)
    elif ch in spec.preload_flags:
        out.preload.append(value)
    elif ch in spec.chdir_flags:
        out.chdir.append(value)
    elif ch in spec.include_flags:
        out.include.append(value)
    elif ch in spec.config_flags:
        out.configs.append(value)
    elif ch in spec.unsafe_flags:
        out.unsafe.append(f"-{ch} {value} loads a code extension that cannot be pinned")
    else:  # define_flags: only the ini settings that load a file or code matter
        name, setting = _php_define(value)
        if name in _PHP_FILE_SETTINGS and setting:
            out.files.append(setting)
        elif name == "include_path":
            out.ini_search.append(setting)
        elif name in _PHP_EXTENSION_SETTINGS:
            out.unsafe.append(f"-d {name} loads a code extension that cannot be pinned")


def _short_option_values(spec: _Interp, args: list[str], i: int, out: _Options) -> int:
    """Collect the values of the flags of interest in the short cluster `args[i]`; words used."""
    a = args[i]
    for pos, ch in enumerate(a[1:]):
        rest = a[pos + 2 :]
        if spec.collects(ch):
            value = rest or (args[i + 1] if i + 1 < len(args) else "")
            if value:
                _record_value(spec, ch, value, out)
            return 1 if rest else 2
        if ch in spec.pragma_flags and _LIB_PRAGMA_RE.match(rest):
            out.include.append(rest)  # perl -Mlib=DIR adds an include directory
            return 1
        if ch in spec.inline and spec.options_after_code:
            # no attached text: the code is the next word; `-eCODE`: the rest of the cluster
            # is code, never more options
            return 1 if rest else 2
        if ch in spec.attached:
            return 1
        if ch in spec.value:
            return 1 if rest else 2
    return 1


def _option_values(spec: _Interp, args: list[str]) -> _Options:
    """Preload files, include dirs and chdir targets named by the options before the operand."""
    out = _Options()
    i = 0
    while i < len(args):
        a = args[i]
        if not a.startswith("-") or a in ("-", "--"):
            break
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            out.flags.append(name)
            if name in spec.long_preload_flags:
                value = val if eq else (args[i + 1] if i + 1 < len(args) else "")
                if value:
                    out.preload.append(value)
            takes_value = name in spec.long_value or (
                name in spec.long_inline and spec.options_after_code
            )
            i += 2 if takes_value and not eq else 1
        else:
            i += _short_option_values(spec, args, i, out)
    return out


_INCLUDE_REASON = "-I puts a caller-chosen directory on the module search path"
_INI_SEARCH_REASON = "-d include_path puts a caller-chosen directory on the include search path"
_UNKNOWN_BASE_IMPORT = "inline code imports a module, but the cd/pushd target is not known"


def _node_modules_reason(kind: str, value: str) -> str:
    return f"node {kind} {value!r} resolves through node_modules"


_PATH_PREFIXES = ("./", "../", "/")
# (suffixes tried after the path as given, index file tried inside a directory)
_NODE_RESOLVE = ((".js", ".json", ".mjs", ".cjs"), ("index.js", "index.json"))
_RESOLVE = {
    "node": _NODE_RESOLVE,
    "deno": _NODE_RESOLVE,
    "ruby": ((".rb",), ()),
    "lua": ((".lua",), ("init.lua",)),
}
_OPTION_PATH_PREFIXES = (*_PATH_PREFIXES, "~/")
_JS_BLOCK_OR_LINE_COMMENT_RE = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)
_RUBY_KEYWORD_RE = re.compile(r"(?<![\w.:@$])(?:require_relative|require|load)\b")
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")  # http:, https:, npm:, jsr:, data:, file:, ...
# string-literal argument of a lua / R / php load call: group 1 the name, group 2 an open paren
_LUA_CALL_RE = re.compile(r"(?<![\w.:])(require|dofile|loadfile)\b\s*(\()?\s*")
_R_CALL_RE = re.compile(r"(?<![\w.])((?:sys\.)?source)\s*(\()\s*(?:file\s*=\s*)?")
_PHP_CALL_RE = re.compile(r"\b(require|include)(?:_once)?\b\s*(\()?\s*", re.I)
_STR_LITERAL_RE = re.compile(r"""(['"])([^'"\\\n]*)\1""")
_NODE_CALL_RE = re.compile(r"\b(?:require|import)\s*\(\s*")  # require( / import(
_NODE_LITERAL_RE = re.compile(r"""(['"])([^'"\\\n]*)\1\s*[,)]""")  # the whole call argument
_NODE_STATIC_RE = re.compile(r"""\b(?:from|import)\s*(['"])([^'"\\\n]*)\1""")
_RUBY_REQ_RE = re.compile(
    r"""(?<![\w.:@$])(require_relative|require|load)\b\s*(?:\(\s*)?(['"])([^'"\n]+)\2"""
)
_PY_IMPORT_RE = re.compile(r"\bimport\b")
_EXPORT_RE = re.compile(r"^export\s+")
_ENV_FILE_MAX = 8 << 20
_NODE_BUILTINS = frozenset(
    {"assert", "async_hooks", "buffer", "child_process", "cluster", "console", "constants"}
    | {"crypto", "dgram", "diagnostics_channel", "dns", "domain", "events", "fs", "http"}
    | {"http2", "https", "inspector", "module", "net", "os", "path", "perf_hooks", "process"}
    | {"punycode", "querystring", "readline", "repl", "stream", "string_decoder", "sys"}
    | {"timers", "tls", "trace_events", "tty", "url", "util", "v8", "vm", "wasi"}
    | {"worker_threads", "zlib", "assert/strict", "dns/promises", "fs/promises"}
    | {"inspector/promises", "path/posix", "path/win32", "readline/promises", "stream/consumers"}
    | {"stream/promises", "stream/web", "timers/promises", "util/types"}
)


def _python_imports(code: str) -> set[str] | None:
    """Dotted names a level-0 `import` in `code` could load (ast, no exec); None if unparseable."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError):
        return None
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names if a.name != "*")
    return names


def _python_import_files(names: set[str]) -> list[str]:
    """Relative files the imports `names` could load from the cwd."""
    files: list[str] = []
    for name in sorted(names):
        parts = name.split(".")
        for k in range(1, len(parts) + 1):
            stem = "/".join(parts[:k])
            files += [f"{stem}.py", f"{stem}/__init__.py"]
    return list(dict.fromkeys(files))


def _node_specifiers(code: str, *, expanded: bool = True) -> tuple[list[str], bool]:
    """(string-literal module specifiers in `code`, whether one is not statically knowable).

    A `require(` / `import(` argument that is not a single plain string literal, or a literal
    containing `$` when the shell expands the code word (`expanded`), makes the code dynamic.
    """
    specs: list[str] = []
    dynamic = False
    for view in _js_views(code):
        for m in _NODE_CALL_RE.finditer(view):
            lit = _NODE_LITERAL_RE.match(view, m.end())
            if lit is None:
                dynamic = True
            else:
                specs.append(lit[2])
        specs += [m[2] for m in _NODE_STATIC_RE.finditer(view)]
    return specs, dynamic or (expanded and any("$" in s for s in specs))


def _strip_js_comments_quoted(code: str) -> str:
    """`code` with comments replaced by a space, honouring '...', "..." and `...` literals."""
    out: list[str] = []
    i, n = 0, len(code)
    quote = ""
    while i < n:
        ch = code[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(code[i + 1])
                i += 1
            elif ch == quote or (ch == "\n" and quote != "`"):
                quote = ""
        elif ch in "'\"`":
            quote = ch
            out.append(ch)
        elif code.startswith("//", i):
            end = code.find("\n", i)
            i = n if end < 0 else end - 1
            out.append(" ")
        elif code.startswith("/*", i):
            end = code.find("*/", i + 2)
            i = n if end < 0 else end + 1
            out.append(" ")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _js_views(code: str) -> list[str]:
    """Texts to scan for module specifiers: raw, and with comments removed.

    A `/*` or quote inside a regex literal can fool any comment stripper, so the code is
    scanned as written, comment-stripped string-aware, and comment-stripped string-blind;
    a specifier found in any view counts (fail closed).
    """
    return [
        code,
        _strip_js_comments_quoted(code),
        _JS_BLOCK_OR_LINE_COMMENT_RE.sub(" ", code),
    ]


def _is_node_builtin(spec: str) -> bool:
    return spec.startswith("node:") or spec in _NODE_BUILTINS


def _is_remote(spec: str) -> bool:
    """A URL-style specifier (http:, npm:, jsr:, data:, ...) other than `node:` builtins."""
    return _URL_RE.match(spec) is not None and not spec.startswith("node:")


def _ruby_requires(code: str) -> list[str]:
    """Operands of `require_relative` / `load` / `require './x'` (could load a local file)."""
    return [
        m[3]
        for m in _RUBY_REQ_RE.finditer(code)
        if m[1] != "require" or m[3].startswith(_PATH_PREFIXES)
    ]


def _ruby_dynamic(code: str, *, expanded: bool) -> bool:
    """Whether a require/load/require_relative in `code` has an operand not known statically.

    Anything but a plain string literal (variable, interpolation, shell-expanded `$`) counts.
    """
    literal_at = {m.start(): m for m in _RUBY_REQ_RE.finditer(code)}
    for kw in _RUBY_KEYWORD_RE.finditer(code):
        m = literal_at.get(kw.start())
        if m is None or (m[2] == '"' and "#{" in m[3]) or (expanded and "$" in m[3]):
            return True
    return False


def _call_closes(tail: str, paren: bool) -> bool:
    """Whether the text after a literal call argument ends the call (no concatenation)."""
    if paren:
        return tail[:1] in (",", ")")
    return not tail or tail[0] in ";)_" or tail[0].isalpha() or tail.startswith("?>")


def _call_literals(code: str, call_re: re.Pattern[str]) -> tuple[list[tuple[str, str]], bool]:
    """((call name, literal argument) per load call, whether one is not a plain literal)."""
    found: list[tuple[str, str]] = []
    dynamic = False
    for m in call_re.finditer(code):
        lit = _STR_LITERAL_RE.match(code, m.end())
        if lit is None or "$" in lit[2] or not _call_closes(code[lit.end() :].lstrip(), bool(m[2])):
            dynamic = True
        else:
            found.append((m[1].lower(), lit[2]))
    return found, dynamic


def _lua_module_path(name: str) -> str:
    """File stem `require name` / `lua -l name` searches ('a.b' is a/b; `g=mod` is mod)."""
    name = name.rpartition("=")[2]
    return name if "/" in name else name.replace(".", "/")


# inline code that changes where modules are searched, or loads a file indirectly: unpinnable
_LUA_SEARCH_RE = re.compile(r"\bpackage\s*(?:\.\s*(?:c?path|searchers|loaders)\b|\[)")
_R_INDIRECT_RE = re.compile(
    r"\.libPaths\s*\(|(?<![\w.])parse\s*\((?![^)]*\btext\s*=)"
    r"|\bdo\.call\s*\(\s*[\"']?(?:sys\.)?source\b"
)
_PHP_SEARCH_RE = re.compile(r"\bset_include_path\s*\(|\bini_set\s*\([^)]*include_path", re.I)


def _lua_loads(code: str) -> tuple[list[str], bool]:
    calls, dynamic = _call_literals(code, _LUA_CALL_RE)
    paths = [_lua_module_path(v) if n == "require" else v for n, v in calls]
    return paths, dynamic or _LUA_SEARCH_RE.search(code) is not None


def _r_loads(code: str) -> tuple[list[str], bool]:
    calls, dynamic = _call_literals(code, _R_CALL_RE)
    return [v for _, v in calls], dynamic or _R_INDIRECT_RE.search(code) is not None


def _php_loads(code: str) -> tuple[list[str], bool]:
    calls, dynamic = _call_literals(code, _PHP_CALL_RE)
    return [v for _, v in calls], dynamic or _PHP_SEARCH_RE.search(code) is not None


_LOAD_SCANNERS = {"lua": _lua_loads, "r": _r_loads, "php": _php_loads}


def _heredoc_bodies(text: str) -> list[str]:
    """The bodies of the `<<WORD` heredocs in the command text (here-strings excluded)."""
    lines = text.split("\n")
    bodies: list[str] = []
    i = 0
    while i < len(lines):
        openers = _HEREDOC_OPEN_RE.findall(lines[i])
        i += 1
        for quoted_s, quoted_d, bare in openers:
            delim = quoted_s or quoted_d or bare
            body: list[str] = []
            while i < len(lines) and lines[i].strip() != delim:
                body.append(lines[i])
                i += 1
            i += 1
            bodies.append("\n".join(body))
    return bodies


_HEREDOC_OPEN_RE = re.compile(r"""<<(?!<)-?[ \t]*(?:'([^'\n]+)'|"([^"\n]+)"|\\?([\w.-]+))""")


def _dotenv_assignment(line: str) -> str | None:
    """`NAME=value` (quotes and a leading `export` removed) for a dotenv line, else None."""
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    key, eq, value = _EXPORT_RE.sub("", s, count=1).partition("=")
    if not eq:
        return key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return f"{key.strip()}={value}"


def _read_capped(path: str) -> bytes | None:
    """The whole file `path` (b"" when it is not a readable regular file), None when it is
    over the scan cap: a file is never scanned in part."""
    if not os.path.isfile(path):
        return b""
    try:
        with open(path, "rb") as fh:
            data = fh.read(_ENV_FILE_MAX + 1)
    except OSError:
        return b""
    return None if len(data) > _ENV_FILE_MAX else data


_AWK_INCLUDE_DEPTH = 16
_AWK_LONG_VALUE = frozenset(
    {"--file", "--include", "--source", "--field-separator", "--assign", "--exec"}
)
_AWK_DIRECTIVE_RE = re.compile(r"^[ \t]*@(include|load)\b[ \t]*(.*)$", re.M)
_AWK_NAME_RE = re.compile(r"""(["'])([^"'\n]+)\1[ \t]*(?:#.*)?$""")


def _awk_includes(text: str) -> tuple[list[str], bool]:
    """(literal `@include` names in awk source, False if an @load or a non-literal @include)."""
    names: list[str] = []
    for m in _AWK_DIRECTIVE_RE.finditer(text):
        lit = _AWK_NAME_RE.match(m[2])
        if m[1] == "load" or lit is None:
            return names, False
        names.append(lit[2])
    return names, True


def _awk_scan(args: list[str]) -> tuple[bool, str | None]:
    """(whether gawk -l / --load is given, the first operand) of an awk command's args."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return False, args[i + 1] if i + 1 < len(args) else None
        if a.startswith("--"):
            name, eq, _ = a.partition("=")
            if name == "--load":
                return True, None
            i += 2 if name in _AWK_LONG_VALUE and not eq else 1
        elif a.startswith("-") and a != "-":
            step = 1
            for pos, ch in enumerate(a[1:]):
                if ch == "l":
                    return True, None
                if ch in "Fvfei":
                    step = 1 if a[pos + 2 :] else 2
                    break
            i += step
        else:
            return False, a
    return False, None


def _env_file_reasons(path: str, cwd: str) -> list[str] | None:
    """Why each loader variable in the dotenv file `path` makes a command non-durable.

    None when the file is too large to scan completely (it is never scanned in part).
    """
    data = _read_capped(path)
    if data is None:
        return None
    reasons: list[str] = []
    for line in data.decode("utf-8", "replace").splitlines():
        assignment = _dotenv_assignment(line)
        reason = loader_env_reason(assignment, cwd) if assignment else None
        if reason:
            reasons.append(reason)
    return reasons


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


def _skip_wrapper(
    w: _Wrapper, args: list[str], assigns: list[str] | None = None
) -> tuple[str, list[str]]:
    """(status, rest): cmd (rest = wrapped command), none, unpin, or body (rest = [code]).

    VAR=val operands the wrapper skips are appended to `assigns` when given.
    """
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
            if assigns is not None and _ASSIGN_RE.match(a):
                assigns.append(a)
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


# Issue #98: environment variables through which a process loads attacker-chosen code.
# A command that sets one is non-durable: approve_once only, never a reusable rule.
_LOADER_ENV_VARS = frozenset(
    {"BASH_ENV", "ENV", "PYTHONSTARTUP", "PYTHONPATH", "PYTHONHOME", "NODE_OPTIONS", "RUBYOPT"}
    | {"PERL5OPT", "PERL5LIB", "LD_PRELOAD", "LD_LIBRARY_PATH"}
    | {"NODE_PATH", "RUBYLIB", "PYTHONUSERBASE", "GEM_PATH", "LD_AUDIT"}
    | {"R_PROFILE_USER", "R_PROFILE", "R_ENVIRON_USER", "R_ENVIRON", "PHPRC", "PHP_INI_SCAN_DIR"}
    | {"AWKPATH", "AWKLIBPATH", "BUN_OPTIONS", "R_LIBS", "R_LIBS_USER", "R_LIBS_SITE"}
    | {"PERL5DB", "TCLLIBPATH", "TCL_LIBRARY"}
)
_LOADER_ENV_RE = re.compile(r"LUA_(?:INIT|PATH|CPATH)(?:_\d_\d)?")  # LUA_INIT_5_4, LUA_PATH_5_3
_SAFE_PATH_REFS = frozenset({"$PATH", "${PATH}"})
_WORLD_WRITABLE_DIRS = ("/tmp", "/var/tmp", "/dev/shm")
_PATH_REASON = "PATH contains a relative, empty, expanding, world-writable or cwd component"
_ALLEXPORT_REASON = "allexport exports every assigned variable (set -a / -o allexport)"
# Marks a `$` that is a literal character (single-quoted or backslash-escaped), not an expansion.
_LITERAL_DOLLAR = "\x00"


def _plain_abs(path: str) -> str:
    """normpath without resolving symlinks, with a single leading slash."""
    return "/" + posixpath.normpath(path).lstrip("/")


def _is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _unsafe_abs_dir(part: str, cwd: str) -> bool:
    """Whether absolute PATH component `part` is (under) a world-writable dir or the cwd."""
    roots = list(_WORLD_WRITABLE_DIRS)
    if cwd.startswith("/") and _plain_abs(cwd) != "/":
        roots.append(_plain_abs(cwd))
    norm = _plain_abs(part)
    return any(_is_under(norm, root) for root in roots)


def _unsafe_path_value(value: str, cwd: str = "") -> bool:
    """Whether a PATH value has an unsafe component.

    Unsafe: '.', empty, relative, a `$` expansion other than a live `$PATH`/`${PATH}`, or a
    directory that is (under) /tmp, /var/tmp, /dev/shm or the command's cwd.
    """
    for part in value.split(":"):
        if part in _SAFE_PATH_REFS:
            continue
        if not part or "$" in part or not part.startswith("/") or _unsafe_abs_dir(part, cwd):
            return True
    return False


_RAW_PIECE_RE = re.compile(
    r"""'([^']*)'|"((?:\\.|[^"\\])*)"|\\(.)|([^\s;&|()<>'"\\]+)|([\s;&|()<>]+)""", re.S
)
_DQ_ESCAPE_RE = re.compile(r'\\([$`"\\\n])')


def _cooked_words(text: str) -> list[str]:
    """Words of the raw command text, unquoted like shlex but with literal `$` marked.

    A `$` that is single-quoted or backslash-escaped becomes _LITERAL_DOLLAR, so only a
    `$` that the shell really expands (unquoted or in double quotes) stays a `$`.
    """
    words: list[str] = []
    cur: list[str] | None = None
    for m in _RAW_PIECE_RE.finditer(text):
        group = m.lastindex or 4
        if group == 5:  # whitespace / operator: ends the word
            if cur is not None:
                words.append("".join(cur))
            cur = None
            continue
        piece = m.group(group)
        if group == 2:
            piece = _DQ_ESCAPE_RE.sub(
                lambda e: _LITERAL_DOLLAR if e[1] == "$" else ("" if e[1] == "\n" else e[1]), piece
            )
        elif group in (1, 3):
            piece = piece.replace("$", _LITERAL_DOLLAR)
        if cur is None:
            cur = []
        cur.append(piece)
    if cur is not None:
        words.append("".join(cur))
    return words


@dataclass
class _RawWord:
    """One shell word of the raw command text, with what its quoting means."""

    shlex: str = ""  # unquoted like shlex (the form the tokenizer hands to the analysis)
    bash: str = ""  # unquoted like bash (`\$` in double quotes is `$`)
    expands: bool = False  # an unquoted / double-quoted, unescaped `$` or backtick
    subst: bool = False  # ... that is a command substitution (`$(` or backtick)
    ansi: bool = False  # contains `$'...'` (escapes decoded by the shell)
    quoted: bool = False  # any quoting or backslash (a quoted heredoc delimiter)


@dataclass
class _Heredoc:
    delimiter: str
    strip_tabs: bool
    quoted: bool
    body: str = ""


@dataclass
class _ShellScan:
    words: list[_RawWord] = field(default_factory=list)
    heredocs: list[_Heredoc] = field(default_factory=list)
    ok: bool = True  # False: quoting could not be followed (callers scan naively)


_WORD_END = frozenset(" \t\r\f\v;&|()<>")


class _ShellScanner:
    """Quote-aware scan of command text: words (single / double quotes, backslashes,
    `$'..'`, `$".."`) and heredocs (a quoted delimiter makes the body literal)."""

    def __init__(self, text: str) -> None:
        self.t = text
        self.i = 0
        self.scan = _ShellScan()
        self.cur: _RawWord | None = None
        self.expect: bool | None = None  # after `<<` / `<<-`: the next word is the delimiter
        self.strip = False
        self.pending: list[_Heredoc] = []

    def run(self) -> _ShellScan:
        n = len(self.t)
        while self.i < n and self.scan.ok:
            c = self.t[self.i]
            if c == "\n":
                self.end_word()
                self.i += 1
                self.read_bodies()
            elif c == "<":
                self.redirect()
            elif c in _WORD_END:
                self.end_word()
                self.i += 1
            elif c == "#" and self.cur is None:
                end = self.t.find("\n", self.i)
                self.i = n if end < 0 else end
            else:
                self.word_char(c)
        self.end_word()
        return self.scan

    def redirect(self) -> None:
        self.end_word()
        if self.t.startswith("<<<", self.i):
            self.i += 3
        elif self.t.startswith("<<", self.i):
            if "((" in self.t[: self.i] or "$[" in self.t[: self.i]:
                self.scan.ok = False  # `<<` may be an arithmetic shift: stay strict
                return
            self.strip = self.t.startswith("<<-", self.i)
            self.i += 3 if self.strip else 2
            self.expect = True
        else:
            self.i += 1

    def end_word(self) -> None:
        word, self.cur = self.cur, None
        if word is None:
            return
        self.scan.words.append(word)
        if self.expect:
            self.expect = None
            doc = _Heredoc(word.bash, self.strip, word.quoted)
            self.scan.heredocs.append(doc)
            self.pending.append(doc)

    def read_bodies(self) -> None:
        lines = self.t[self.i :].split("\n")
        used = 0
        for doc in self.pending:
            body: list[str] = []
            while used < len(lines):
                line = lines[used]
                used += 1
                if (line.lstrip("\t") if doc.strip_tabs else line) == doc.delimiter:
                    break
                body.append(line)
            doc.body = "\n".join(body)
        self.pending = []
        self.i = min(len(self.t), self.i + sum(len(x) + 1 for x in lines[:used]))

    def word(self) -> _RawWord:
        if self.cur is None:
            self.cur = _RawWord()
        return self.cur

    def add(self, bash: str, shlex: str | None = None) -> None:
        w = self.word()
        w.bash += bash
        w.shlex += bash if shlex is None else shlex

    def mark(self, *, subst: bool) -> None:
        w = self.word()
        w.expands = True
        w.subst = w.subst or subst

    def word_char(self, c: str) -> None:
        if c == "\\":
            self.backslash()
        elif c == "'":
            self.single()
        elif c == '"':
            self.double()
        elif c == "$":
            self.dollar()
        else:
            if c == "`":
                self.mark(subst=True)
            self.add(c)
            self.i += 1

    def backslash(self) -> None:
        nxt = self.t[self.i + 1 : self.i + 2]
        self.i += 2
        if nxt == "\n":  # line continuation
            return
        self.word().quoted = True
        self.add(nxt or "\\")

    def single(self) -> None:
        end = self.t.find("'", self.i + 1)
        if end < 0:
            self.scan.ok = False
            return
        self.word().quoted = True
        self.add(self.t[self.i + 1 : end])
        self.i = end + 1

    def double(self) -> None:
        self.word().quoted = True
        self.i += 1
        n = len(self.t)
        while self.i < n:
            c = self.t[self.i]
            if c == '"':
                self.i += 1
                return
            if c == "\\" and self.i + 1 < n:
                self.dq_escape(self.t[self.i + 1])
                self.i += 2
                continue
            after = self.after_dollar() if c == "$" else ""
            if c == "`" or after in ("(", "{"):
                self.scan.ok = False  # nested quoting inside the expansion: stay strict
                return
            if after and after in _DQ_EXPANDING_AFTER_DOLLAR:
                self.mark(subst=False)  # any other `$` is literal inside double quotes
            self.add(c)
            self.i += 1
        self.scan.ok = False

    def after_dollar(self) -> str:
        """First char after the `$` at `self.i`, past any `\\<newline>` continuations ("" at end)."""
        j = self.i + 1
        while self.t[j : j + 2] == "\\\n":
            j += 2
        return self.t[j : j + 1]

    def dq_escape(self, nxt: str) -> None:
        if nxt in '$`"\\':
            self.add(nxt, nxt if nxt in '"\\' else "\\" + nxt)
        elif nxt == "\n":  # line continuation: joined, as in bash and shellprep
            self.add("")
        else:
            self.add("\\" + nxt)

    def dollar(self) -> None:
        nxt = self.t[self.i + 1 : self.i + 2]
        if nxt == "'":  # ANSI-C quoting: escapes decoded by the shell
            end = self.i + 2
            while end < len(self.t) and self.t[end] != "'":
                end += 2 if self.t[end] == "\\" else 1
            w = self.word()
            w.ansi = w.quoted = True
            if end >= len(self.t):
                self.scan.ok = False
            self.add(self.t[self.i + 2 : end])
            self.i = end + 1
        elif nxt == '"':  # locale translation: a double-quoted string
            self.i += 1
            self.double()
        else:
            self.mark(subst=nxt == "(")
            self.add("$")
            self.i += 1


# Characters after which a `$` inside double quotes expands: name start, digits, special
# parameters (`@ * # ? - $ ! 0`) and legacy arithmetic `$[`. `(` and `{` bail out earlier.
# End of input is excluded separately: a bare trailing `$` is literal.
_DQ_EXPANDING_AFTER_DOLLAR = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789@*#?-$!["
)


def _scan_shell(text: str) -> _ShellScan:
    return _ShellScanner(text).run()


# `NAME=value` or the append form `NAME+=value` (write_targets._ASSIGN_RE only has the first)
_ASSIGN_OR_APPEND_RE = re.compile(r"^[A-Za-z_]\w*\+?=")
_DECLARATIONS = frozenset({"export", "declare", "typeset", "local", "readonly"})
_DECLARES_EXPORTED = frozenset({"declare", "typeset", "local"})  # exported only with -x
# make command-line variable operands: = := ::= ?= += !=  (`!=` runs the value in a shell)
_MAKE_ASSIGN_RE = re.compile(r"^([A-Za-z_]\w*)(\+|::?|\?|!)?=(.*)$", re.S)


def _is_loader_var(name: str) -> bool:
    return name in _LOADER_ENV_VARS or _LOADER_ENV_RE.fullmatch(name) is not None


def loader_env_reason(assignment: str, cwd: str = "") -> str | None:
    """Why `NAME=value` / `NAME+=value` / bare `NAME` makes a command non-durable, else None.

    A bare name (no `=`) is only checked against the loader list: its value is inherited
    or set earlier, so it is unknown. For `PATH+=`, the existing value counts as $PATH.
    `cwd` is the command's directory: a PATH component under it is unsafe.
    """
    name, eq, value = assignment.partition("=")
    append = name.endswith("+") and bool(eq)
    name = name.removesuffix("+") if append else name
    if _is_loader_var(name):
        return f"{name} loads code through the environment"
    if name == "PATH" and eq and _unsafe_path_value(f"$PATH{value}" if append else value, cwd):
        return _PATH_REASON
    return None


def _make_assignments(args: list[str]) -> list[str]:
    """make operands `NAME<op>value` as `NAME=value` / `NAME+=value`; `!=` becomes `$(...)`."""
    out: list[str] = []
    for arg in args:
        m = _MAKE_ASSIGN_RE.match(arg)
        if m:
            name, op, value = m.groups()
            if op == "!":
                value = f"$({value})"  # shell output: not a known value
            out.append(f"{name}{'+' if op == '+' else ''}={value}")
    return out


def _scan_builtin(args: list[str], value_flags: str) -> tuple[dict[str, str], list[str]]:
    """(flag -> value, operands) of a builtin; `value_flags` take an attached or next-word value."""
    values: dict[str, str] = {}
    operands: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            operands += args[i:]
            break
        if not arg.startswith("-") or len(arg) == 1:
            operands.append(arg)
            continue
        for pos, ch in enumerate(arg[1:]):
            if ch in value_flags:
                rest = arg[pos + 2 :]
                if not rest and i < len(args):
                    rest, i = args[i], i + 1
                values[ch] = rest
                break
    return values, operands


def _read_targets(args: list[str]) -> list[str]:
    values, operands = _scan_builtin(args, "dinNptua")
    return [*operands, *([values["a"]] if "a" in values else [])]


def _mapfile_targets(args: list[str]) -> list[str]:
    return _scan_builtin(args, "dnOsuCc")[1][:1]  # only the array name


def _printf_targets(args: list[str]) -> list[str]:
    value = _scan_builtin(args[:2], "v")[0].get("v")
    return [value] if value else []


_TARGET_BUILTINS = {
    "read": _read_targets,
    "mapfile": _mapfile_targets,
    "readarray": _mapfile_targets,
    "printf": _printf_targets,
}


def _sets_allexport(args: list[str]) -> bool:
    """Whether `set ARGS` turns on allexport: an `a` in a `-` cluster, or `-o allexport`."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--" or not arg.startswith(("-", "+")) or len(arg) == 1:
            break  # positional parameters start here
        if arg.startswith("-") and "a" in arg[1:]:
            return True
        if arg.endswith("o"):
            # `-o NAME` / `+o NAME` (also clustered, `-eo NAME`): the next word is an option name
            if arg.startswith("-") and args[i + 1 : i + 2] == ["allexport"]:
                return True
            i += 1
        i += 1
    return False


def _builtin_reasons(words: list[str]) -> list[str]:
    """Why `set` / `read` / `printf -v` / `mapfile` make a command non-durable (may be empty).

    `set -a` / `set -o allexport` exports everything assigned later; the others write input
    into a variable, which is dangerous when it is a loader variable or PATH.
    """
    words = _unwrap_builtins(words)
    if not words:
        return []
    head, args = words[0], words[1:]
    if head == "set":
        return [_ALLEXPORT_REASON] if _sets_allexport(args) else []
    scan = _TARGET_BUILTINS.get(head)
    reasons: list[str] = []
    for target in scan(args) if scan else []:
        name = target.partition("[")[0]
        reason = loader_env_reason(name)
        if reason is None and name == "PATH":
            reason = f"PATH is overwritten by {head}"
        if reason:
            reasons.append(reason)
    return reasons


def _unwrap_builtins(words: list[str]) -> list[str]:
    """Drop leading `builtin` / `command [-p]` so the wrapped builtin can be inspected."""
    i = 0
    while i < len(words) and words[i] in ("builtin", "command"):
        i += 1
        while i < len(words) and words[i] == "-p":
            i += 1
    return words[i:]


def _exported_assignments(words: list[str]) -> list[str]:
    """Operands of export/declare/typeset/local/readonly that can set a loader variable.

    NAME=val / NAME+=val operands count when the variable ends up exported (export, or
    -x on declare/typeset/local), and always for a loader variable or PATH, which are
    normally exported already. A bare NAME operand of a loader variable always counts:
    its value was set earlier or is inherited, so it is unknown.
    """
    words = _unwrap_builtins(words)
    if not words or words[0] not in _DECLARATIONS:
        return []
    flags = [a for a in words[1:] if a.startswith("-") and not _ASSIGN_OR_APPEND_RE.match(a)]
    exported = words[0] == "export" or (
        words[0] in _DECLARES_EXPORTED and any("x" in f for f in flags if not f.startswith("--"))
    )
    found: list[str] = []
    for operand in (a for a in words[1:] if a not in flags):
        name = operand.partition("=")[0].removesuffix("+")
        if operand == name:  # bare NAME
            keep = _is_loader_var(name)
        else:
            keep = bool(_ASSIGN_OR_APPEND_RE.match(operand)) and (
                exported or _is_loader_var(name) or name == "PATH"
            )
        if keep:
            found.append(operand)
    return found


def _strip_reserved(words: list[str], assigns: list[str] | None = None) -> list[str]:
    """Drop reserved words / grouping and leading VAR=val; [] when nothing runs.

    The dropped VAR=val words are appended to `assigns` when given.
    """
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
        elif _ASSIGN_OR_APPEND_RE.match(head):
            if assigns is not None:
                assigns.append(head)
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
    # issue #98: code-loading environment variables; still pinned, but never reusable
    non_durable: list[str] = field(default_factory=list)

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
    | {"--max-lines", "--delimiter", "--arg-file", "--eof", "--max-chars", "--process-slot-var"}
)
# xargs tails whose work is chosen by stdin (target/task names): never pinnable
_XARGS_RUNNERS = frozenset({"make", "pixi", "uv", "npm", "yarn", "pnpm", "npx", "bunx", "pnpx"})
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


def _unknown_leading_flag(args: list[str], flags: frozenset[str], values: frozenset[str]) -> bool:
    """Whether an option before the subcommand/script is outside the known boolean/value flags."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--" or not a.startswith("-") or a == "-":
            return False
        name, eq, _ = a.partition("=")
        if name in values:
            i += 1 if eq else 2
        elif name in flags:
            i += 1
        else:
            return True
    return False


def _leading_has(args: list[str], targets: frozenset[str], values: frozenset[str]) -> bool:
    """Whether one of the `targets` options appears before the first operand (or `--`)."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--" or not a.startswith("-") or a == "-":
            return False
        name, eq, val = a.partition("=")
        if name in targets and not (eq and val == "false"):
            return True
        i += 1 if eq or name not in values else 2
    return False


def _scan_flag_values(
    args: list[str], collect: frozenset[str], values: frozenset[str], *, whole: bool
) -> list[tuple[str, str]]:
    """(option, value) of the `collect` options (`--x=v` or `--x v`) up to `--`.

    Without `whole` the scan also stops at the first operand (options before the script).
    """
    got: list[tuple[str, str]] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            break
        if not a.startswith("-") or a == "-":
            if not whole:
                break
            i += 1
            continue
        name, eq, val = a.partition("=")
        if name in collect:
            got.append((name, val if eq else (args[i + 1] if i + 1 < len(args) else "")))
        i += 1 if eq or (name not in collect and name not in values) else 2
    return got


_UV_VALUE = frozenset(
    {"--directory", "--project", "--with", "--with-editable", "--with-requirements", "--python"}
    | {"-p", "--package", "--env-file", "--index", "--default-index", "--index-url", "-i"}
    | {"--extra-index-url", "--group", "--extra", "--no-group", "--exclude-newer", "--script"}
    | {"--find-links", "-f", "--cache-dir", "--config-file", "--color", "--python-preference"}
    | {"--refresh-package", "--reinstall-package", "--upgrade-package", "-P"}
    | {"--no-extra", "--only-group", "--index-strategy", "--keyring-provider", "--resolution"}
    | {"--prerelease", "--link-mode", "--config-setting", "-C", "--no-build-package"}
    | {"--no-binary-package", "--allow-insecure-host", "--fork-strategy", "--python-platform"}
    | {"--constraints", "-c", "--overrides", "--build-constraints", "-b", "--torch-backend"}
    | {"--no-install-package"}
)
# uv options accepted before `run` / before the script; any other one fails closed
_UV_GLOBAL_VALUE = frozenset(
    {"--color", "--directory", "--project", "--config-file", "--cache-dir"}
    | {"--allow-insecure-host", "--python-preference"}
)
_UV_GLOBAL_FLAGS = frozenset(
    {"-q", "--quiet", "-qq", "-v", "--verbose", "-vv", "-vvv", "--native-tls", "--offline"}
    | {"--no-progress", "--no-config", "-n", "--no-cache", "--preview", "--no-preview"}
    | {"--no-python-downloads", "--managed-python", "--no-managed-python", "-h", "--help"}
    | {"-V", "--version"}
)
_UV_RUN_FLAGS = _UV_GLOBAL_FLAGS | (
    {"--frozen", "--locked", "--no-sync", "--isolated", "--all-extras", "--no-dev", "--dev"}
    | {"--only-dev", "--all-groups", "--no-default-groups", "--all-packages", "--no-project"}
    | {"--no-env-file", "-m", "--module", "-s", "--gui-script", "--exact", "--no-editable"}
    | {"--active", "--no-active", "--no-index", "-U", "--upgrade", "--reinstall", "--refresh"}
    | {"--no-build-isolation", "--no-build", "--no-binary", "--only-binary", "--compile-bytecode"}
    | {"--no-install-project", "--no-install-workspace", "--no-sources", "--inexact"}
    | {"--verify-hashes"}
)
# Not allowlisted, so refused: --script-shell, --shell-mode, --node-options, --userconfig,
# --use-yarnrc, --config.* (they change the shell, node binary or config file that runs it)
_NODE_PM_FLAGS = {
    "npm": frozenset(
        {"-s", "--silent", "--workspaces", "-ws", "--if-present", "--ignore-scripts"}
        | {"--include-workspace-root", "-d", "-dd", "-ddd", "--verbose", "-q", "--quiet"}
        | {"--color", "--no-color", "--foreground-scripts", "-h", "--help", "-v", "--version"}
        | {"--json", "--offline", "--prefer-offline", "--prefer-online", "--no-audit"}
        | {"--no-fund", "--production", "--no-progress", "--no-update-notifier", "--audit"}
        | {"--fund", "--update-notifier", "--progress"}
    ),
    "yarn": frozenset(
        {"-s", "--silent", "--verbose", "--offline", "--frozen-lockfile", "--immutable"}
        | {"--json", "--non-interactive", "--no-progress", "--prefer-offline", "--production"}
        | {"--ignore-engines", "--ignore-scripts", "--check-files", "-h", "--help", "-v"}
        | {"--version", "--no-default-rc"}
    ),
    "pnpm": frozenset(
        {"-r", "--recursive", "-w", "--workspace-root", "-s", "--silent", "--if-present"}
        | {"--parallel", "--stream", "--aggregate-output", "--no-bail", "--sort", "--no-sort"}
        | {"--reverse", "--report-summary", "--color", "--no-color", "-h", "--help", "-v"}
        | {"--version", "--offline", "--prefer-offline", "--frozen-lockfile", "--fail-if-no-match"}
        | {"--ignore-scripts", "--ignore-workspace", "--include-workspace-root"}
    ),
}
_NODE_PM_VALUES = {
    "npm": frozenset({"--prefix", "-w", "--workspace", "--registry", "--cache", "--loglevel"}),
    "yarn": frozenset({"--cwd", "--registry", "--network-timeout", "--network-concurrency"}),
    "pnpm": frozenset(
        {"-C", "--dir", "-F", "--filter", "--filter-prod", "--reporter", "--loglevel"}
        | {"--workspace-concurrency", "--resume-from", "--registry"}
    ),
}
_NPM_VALUE = frozenset(_NODE_PM_VALUES["npm"])
# flags npm parses AFTER the script name: the leading allowlist minus whatever selects
# workspaces; --prefix/-w/--workspace are collected and pinned separately
_NPM_ALL_WORKSPACES = frozenset({"--workspaces", "-ws", "--include-workspace-root"})
_NPM_TRAILING_FLAGS = _NODE_PM_FLAGS["npm"] - _NPM_ALL_WORKSPACES
_NPM_TRAILING_VALUE = frozenset({"--loglevel"})
_NPM_SELECT = frozenset({"--prefix", "-w", "--workspace"})
_PNPM_SELECT = frozenset({"-F", "--filter", "--filter-prod"})
_PNPM_RECURSIVE = frozenset({"-r", "--recursive"})
_UV_FILE_FLAGS = frozenset(
    {"--with-requirements", "-c", "--constraints", "--overrides", "--config-file"}
    | {"-b", "--build-constraints"}
)
_UV_ALWAYS_PATH = frozenset({"--find-links", "-f", "--with-editable"})
_ARCHIVE_SUFFIXES = (".whl", ".tar.gz", ".zip")
_UV_PATHISH_FLAGS = frozenset({"--python", "-p", "--with", "--with-editable", "--find-links", "-f"})
_UV_MODULE = frozenset({"-m", "--module"})
_NPM_RUN = frozenset(
    {"run", "run-script", "rum", "urn", "test", "t", "tst", "start", "stop", "restart"}
)
_PM_VALUE = _NODE_PM_VALUES["yarn"] | _NODE_PM_VALUES["pnpm"] | {"--prefix"}
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
_BUN_VALUE = _BUN_LONG_VALUE | {"-r", "-d", "-l", "-c", "-e", "-p", "--eval", "--print"}
_BUN_UNPIN = frozenset({"--cwd", "--config", "-c", "--env-file", "--tsconfig-override"})
_BUN_PASSIVE = _PM_NON_RUN | {"build", "pm", "repl", "upgrade", "completions", "help"}
# bun/deno options accepted before the subcommand or script; any other one fails closed
_BUN_FLAGS = frozenset(
    {"--watch", "--hot", "--smol", "--bun", "-b", "--silent", "--no-install", "--prefer-offline"}
    | {"--prefer-latest", "--no-addons", "--no-clear-screen", "--no-macros", "--if-present"}
    | {"--no-summary", "--verbose", "--install", "-h", "--help", "-v", "--version", "--revision"}
)
_DENO_GLOBAL_FLAGS = frozenset({"-h", "--help", "-V", "--version", "-q", "--quiet", "--no-color"})
_DENO_GLOBAL_VALUE = frozenset({"-L", "--log-level"})
_BUN_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
# deno subcommands that do not run a script given on the command line
_DENO_PASSIVE = frozenset(
    {"fmt", "lint", "check", "info", "install", "uninstall", "upgrade"}
    | {"compile", "cache", "doc", "init", "add", "remove", "outdated", "completions", "help"}
    | {"types", "publish", "lsp", "clean", "coverage", "vendor", "create"}
)
# deno run/eval options; an unknown one could swallow the script operand, so it fails closed
_DENO_FLAGS = frozenset(
    {"-q", "--quiet", "--no-check", "--check", "--no-prompt", "-A", "--allow-all", "--no-remote"}
    | {"--cached-only", "--no-npm", "--no-lock", "--reload", "-r", "--watch", "--unstable"}
    | {"--no-clear-screen", "--no-config", "-p", "--print"}
)
_DENO_PREFIXES = ("--allow-", "--deny-", "--unstable-")
_DENO_VALUE = frozenset(
    {"--config", "-c", "--import-map", "--cert", "--location", "--seed", "--ext"}
)
_DENO_FILE_FLAGS = frozenset({"--config", "-c", "--import-map"})


def _deno_options(args: list[str]) -> tuple[list[str], list[str], bool]:
    """(files of --config/--import-map, operands, unknown option) of the args after run/eval."""
    files: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return files, args[i + 1 :], False
        if not a.startswith("-") or a == "-":
            break
        name, eq, val = a.partition("=")
        if name in _DENO_VALUE:
            value = val if eq else (args[i + 1] if i + 1 < len(args) else "")
            if name in _DENO_FILE_FLAGS:
                files.append(value)
            i += 1 if eq else 2
        elif name in _DENO_FLAGS or name.startswith(_DENO_PREFIXES):
            i += 1
        else:
            return files, [], True
    return files, args[i:], False


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


def _prog_files(
    args: list[str],
    value_flags: str,
    attached_flags: str = "",
    file_flag: str = "f",
    long_file: str = "--file",
) -> list[str]:
    """Files named by `-f FILE` / `--file=FILE` of awk / sed (or gawk `-i` / `--include`)."""
    files: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            break
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if name == long_file:
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
            if ch == file_flag:
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
        self.raw_words: list[str] = []  # its words, unquoted with literal `$` marked
        self.scan: _ShellScan | None = None  # its quote-aware scan (words, heredocs)
        # (bases, base_unknown) to restore after a systemd-run unwrap
        self.wd_saved: tuple[list[str], bool] | None = None

    # -- helpers --
    def unpin(self, reason: str) -> None:
        self.plan.unpinnable = True
        self.plan.reasons.append(reason)

    def non_durable(self, reason: str) -> None:
        if reason not in self.plan.non_durable:
            self.plan.non_durable.append(reason)

    def check_assigns(self, assigns: list[str]) -> None:
        """Flag loader-variable / unsafe PATH assignments.

        The shlex token has lost its quoting, so each assignment is checked against the
        matching raw word(s) of the command text: a quoted or escaped `$PATH` is a literal.
        """
        for assignment in assigns:
            raw = [w for w in self.raw_words if w.replace(_LITERAL_DOLLAR, "$") == assignment]
            name = assignment.partition("=")[0].removesuffix("+")
            if not raw and name == "PATH" and "$" in assignment:
                self.non_durable("PATH: unresolvable quoting")  # fail closed
                continue
            for form in raw or [assignment]:
                reason = loader_env_reason(form, self.cwd)
                if reason:
                    self.non_durable(reason)

    def check_builtin(self, words: list[str]) -> None:
        for reason in _builtin_reasons(words):
            self.non_durable(reason)

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
        saved_raw, self.raw_words = self.raw_words, _cooked_words(text)
        saved_scan, self.scan = self.scan, _scan_shell(text)
        try:
            for cmd in _split_commands(tokens):
                assigns: list[str] = []
                words = _strip_reserved(cmd.words, assigns)
                self.check_assigns(assigns)
                self.check_assigns(_exported_assignments(words))
                self.check_builtin(words)
                if words:
                    self.command(words, cmd, depth)
        finally:
            self.text, self.raw_words, self.scan = saved, saved_raw, saved_scan

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

    def _strip(self, words: list[str]) -> list[str]:
        """_strip_reserved, checking the dropped VAR=val words for code loaders."""
        assigns: list[str] = []
        stripped = _strip_reserved(words, assigns)
        self.check_assigns(assigns)
        return stripped

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
                words = self._strip(inner) if inner else []
                if not words:
                    return None
                continue
            spec = _WRAPPERS.get(name) if name else None
            if spec is None:
                return words
            assigns: list[str] = []
            status, rest = _skip_wrapper(spec, words[1:], assigns)
            self.check_assigns(assigns)
            if status == "unpin":
                self.unpin(f"cannot parse the options of {name}")
                return None
            if status == "body":
                self.run(rest[0], depth + 1)
                return None
            if status == "none":
                return None
            words = self._strip(rest)
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
            self.awk(args)
        elif base == "go":
            self.go(args)
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

    def awk(self, args: list[str]) -> None:
        loads, operand = _awk_scan(args)
        if loads:
            self.unpin("gawk -l/--load loads a compiled extension that cannot be pinned")
        files = _prog_files(args, "Fvel")
        for f in files:
            self.add_file(f)
        # gawk -i FILE / --include=FILE: found as given, then with `.awk` appended
        includes = _prog_files(args, "Fvefl", file_flag="i", long_file="--include")
        for f in includes:
            if _literal(f):
                self.pin_inline([f, f + ".awk"])
            else:
                self.add_file(f)  # unpinnable
        texts = _prog_files(args, "Fvfil", file_flag="e", long_file="--source")
        if not files and not texts and operand is not None:
            texts = [operand]
        seen: set[str] = set()
        for f in [*files, *includes]:
            if _literal(f):
                self.awk_follow_file(f, seen, 0)
        for text in texts:
            self.awk_follow_text(text, seen, 0)

    def awk_path(self, name: str) -> str | None:
        """Realpath of the first existing file among `name`, `name.awk` (gawk's @include rule)."""
        for cand in (name, name + ".awk"):
            for path in _ref_paths(ScriptRef(cand, tuple(self.bases)), self.cwd):
                if os.path.isfile(path):
                    return path
        return None

    def awk_follow_file(self, name: str, seen: set[str], depth: int) -> None:
        path = self.awk_path(name)
        if path is None or path in seen:
            return
        seen.add(path)
        data = _read_capped(path)
        if data is None:
            self.unpin(f"awk source {name} is too large to scan for @include")
            return
        self.awk_follow_text(data.decode("utf-8", "replace"), seen, depth)

    def awk_follow_text(self, text: str, seen: set[str], depth: int) -> None:
        """Pin the files `@include` pulls into awk source `text`, recursively (cycle-safe)."""
        if depth >= _AWK_INCLUDE_DEPTH:
            self.unpin("awk @include nesting is too deep to pin")
            return
        names, ok = _awk_includes(text)
        if not ok:
            self.unpin("awk source loads an extension or has a non-literal @include")
            return
        for name in names:
            if not _literal(name):
                self.add_file(name)  # unpinnable
                continue
            self.pin_inline([name, name + ".awk"])
            if not self.base_unknown:
                self.awk_follow_file(name, seen, depth + 1)

    def go(self, args: list[str]) -> None:
        sub, rest, _ = _split_runner(args, frozenset({"-C"}), frozenset())
        if sub == "run" and rest:
            self.unpin("go run compiles and runs code that cannot be pinned")
        elif sub == "generate":
            self.unpin("go generate runs commands named in source files")

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
        if spec.cli == "deno":
            self.deno(spec, args, (heredoc, cmd), depth)
            return
        if spec.cli == "bun":
            bun_args = self.bun_args(args, depth)
            if bun_args is None:
                return
            args = bun_args
        if (
            spec.unpin_options
            and args[:1]
            and args[0].startswith("-")
            and args[0] not in ("-", "--")
        ):
            self.unpin("interpreter options are not modelled; run a literal script file")
            return
        opts = _option_values(spec, args)
        if not opts.chdir:
            self.run_interpreter(spec, args, opts, (heredoc, cmd), depth)
            return
        saved = (list(self.bases), self.base_unknown)
        for directory in opts.chdir:  # ruby -C DIR: everything below resolves inside DIR
            if _literal(directory):
                self.bases = _extend_bases(self.bases, directory)
            else:
                self.base_unknown = True
        try:
            self.run_interpreter(spec, args, opts, (heredoc, cmd), depth)
        finally:
            self.bases, self.base_unknown = saved

    def run_interpreter(
        self,
        spec: _Interp,
        args: list[str],
        opts: _Options,
        source: tuple[bool, _Simple],
        depth: int,
    ) -> None:
        heredoc, cmd = source
        self.option_refs(spec, opts)
        kind, operand = _scan_options(spec, args)
        for code in opts.code:
            self.inline_code(spec, code, depth)
        if kind != "none" or opts.code:
            self.implicit_refs(spec, opts)
        if heredoc and kind in ("stdin", "stdin!"):
            self.heredoc_stdin(spec.lang)
            return
        if kind == "file" and operand is not None:
            self.add_file(operand)
        elif kind == "inline" and operand is not None:
            self.inline_code(spec, operand, depth)
        elif kind == "module" and operand is not None:
            self.module(operand)
        elif kind in ("stdin", "stdin!"):
            self.stdin(cmd, explicit=kind == "stdin!", depth=depth)

    def text_expands(self) -> bool:
        """Whether the shell expands `$` / backticks anywhere the interpreter's input is built.

        Single-quoted or escaped characters and the body of a quoted-delimiter heredoc are
        literal. When the quoting cannot be followed, any `$` or backtick counts.
        """
        scan = self.scan
        if scan is None or not scan.ok:
            return "$" in self.text or "`" in self.text
        return any(w.expands for w in scan.words) or any(
            not h.quoted and ("$" in h.body or "`" in h.body) for h in scan.heredocs
        )

    def heredoc_bodies(self) -> list[str]:
        scan = self.scan
        if scan is None or not scan.ok:
            return _heredoc_bodies(self.text)
        return [h.body for h in scan.heredocs]

    def inline_word(self, operand: str) -> tuple[bool, bool, str]:
        """(shell expands in it, ... by command substitution, code) for inline code `operand`.

        The words of the raw command text containing the operand say how it was quoted; if
        none does (the tokenizer rewrote it), any `$` / backtick in the operand counts. The
        code is the bash-unquoted form (`\\$1` in double quotes is `$1`) of an exact match.
        """
        cands = self.exact_words(operand)
        if not cands:
            return "$" in operand or "`" in operand, "$(" in operand or "`" in operand, operand
        bashes = {w.bash for w in cands}
        code = bashes.pop() if len(bashes) == 1 and not any(w.ansi for w in cands) else operand
        return any(w.expands for w in cands), any(w.subst for w in cands), code

    def exact_words(self, operand: str) -> list[_RawWord]:
        """Raw words whose shlex-unquoted form is exactly `operand` (none: quoting unknown)."""
        scan = self.scan
        if scan is None or not scan.ok:
            return []
        return [w for w in scan.words if w.shlex == operand]

    def has_ansi_c(self) -> bool:
        scan = self.scan
        if scan is None or not scan.ok:
            return "$'" in self.text
        return any(w.ansi for w in scan.words)

    def shell_body(self, operand: str, depth: int) -> None:
        """Recurse into a shell `-c` body as the inner shell receives it (bash-unquoted)."""
        if "$(" in operand or "`" in operand:
            self.unpin("inline code is assembled by a command substitution")
            return
        cands = self.exact_words(operand)
        bashes = {w.bash for w in cands}
        if len(bashes) == 1 and not any(w.ansi for w in cands):
            body = bashes.pop()
        elif "\\$" in operand or "\\`" in operand:
            self.unpin("shell -c body has an escaped $ or backtick whose quoting is unknown")
            return
        else:
            body = operand
        self.run(body, depth + 1)

    def inline_code(self, spec: _Interp, operand: str, depth: int) -> None:
        if spec.shell:  # the nested command string is analyzed on its own
            self.shell_body(operand, depth)
            return
        expands, subst, code = self.inline_word(operand)
        if subst:
            self.unpin("inline code is assembled by a command substitution")
        elif spec.opaque_inline:
            self.unpin("inline code of this interpreter cannot be pinned")
        elif spec.lang and self.has_ansi_c():
            self.unpin("inline code uses $'...' quoting, whose escapes are decoded by the shell")
        elif spec.lang:
            self.inline_imports(spec.lang, code, expanded=expands)

    def local_file_exists(self, operand: str) -> bool:
        ref = ScriptRef(operand, tuple(self.bases))
        return any(os.path.isfile(p) for p in _ref_paths(ref, self.cwd))

    def resolve_candidates(self, operand: str, lang: str) -> list[str]:
        """Files node/ruby would try for `operand` (as given, suffixed, index), in order."""
        suffixes, indexes = _RESOLVE.get(lang, ((), ()))
        return [operand, *(operand + s for s in suffixes)] + [
            posixpath.join(operand, i) for i in indexes
        ]

    def resolve_local(self, operand: str, lang: str) -> str | None:
        """First existing file node/ruby would load for `operand` (as given, suffixed, index)."""
        cands = self.resolve_candidates(operand, lang)
        return next((c for c in cands if self.local_file_exists(c)), None)

    def option_refs(self, spec: _Interp, opts: _Options) -> None:
        """Preload files (node -r/--require/--import/--loader, ruby -r) and -I directories (#98)."""
        for value in opts.preload:
            if not _literal(value):
                self.add_file(value)  # unpinnable: not a literal path
            elif spec.lang == "lua":  # lua -l NAME: a module found through ./?.lua
                self.pin_inline_paths([_lua_module_path(value)], "lua")
            elif value.startswith(_OPTION_PATH_PREFIXES):
                self.add_file(self.resolve_local(value, spec.lang) or value)
            elif spec.lang == "node" and not _is_node_builtin(value):
                self.non_durable(_node_modules_reason("preload", value))
        for value in opts.files:  # php -d auto_prepend_file= / auto_append_file=
            self.add_file(value)
        for value in opts.configs:  # php -c
            self.config_ref(value)
        if opts.include:
            self.non_durable(_INCLUDE_REASON)
        if opts.ini_search:
            self.non_durable(_INI_SEARCH_REASON)
        for reason in opts.unsafe:
            self.unpin(reason)

    def config_ref(self, value: str) -> None:
        """A config file (or a directory holding php.ini): pinned, but not a script run."""
        if not _literal(value):
            self.unpin(f"config path {value!r} is not a literal path")
        elif value.endswith(".ini"):
            self.add_file(value, script=False)
        else:
            self._ref(value, "search", ("php.ini",))

    def implicit_refs(self, spec: _Interp, opts: _Options) -> None:
        """Pin cwd files the interpreter loads on its own (Rscript .Rprofile, bun bunfig.toml)."""
        off = dict(spec.implicit_off)
        for name in spec.implicit_files:
            if any(flag in opts.flags for flag in off.get(name, ())):
                continue
            exists = self.local_file_exists(name)
            self.emit(
                ScriptRef(".", tuple(self.bases), "search", (name,), required=False, script=exists)
            )

    def inline_imports(self, lang: str, code: str, *, expanded: bool = False) -> None:
        """Pin the local files inline code (-c / -e / heredoc) imports; fail closed if unsure.

        `expanded`: the shell expands `$` / backticks in the code word (heredocs are checked
        before this is reached).
        """
        if lang == "python":
            self.inline_python(code, expanded=expanded)
        elif lang in ("node", "deno"):
            self.inline_node(code, expanded=expanded, remote_unpinnable=lang == "deno")
        elif lang == "ruby":
            if _ruby_dynamic(code, expanded=expanded):
                self.unpin("inline ruby code requires a file that cannot be resolved statically")
            else:
                self.pin_inline_paths(_ruby_requires(code), "ruby")
        else:
            operands, dynamic = _LOAD_SCANNERS[lang](code)
            if dynamic:
                self.unpin(f"inline {lang} code loads a file that cannot be resolved statically")
            else:
                self.pin_inline_paths(operands, lang)

    def pin_inline(self, files: list[str]) -> None:
        """Pin every candidate file; an absent one is pinned as MISSING (created later: mismatch).

        Only a candidate that exists counts as running a script (non-promotable).
        """
        for f in dict.fromkeys(files):
            self.emit(
                ScriptRef(f, tuple(self.bases), required=False, script=self.local_file_exists(f))
            )

    def pin_inline_paths(self, operands: list[str], lang: str) -> None:
        """Pin what node/ruby would load for local `operands`; unknown cd target: unpinnable."""
        if self.base_unknown and any(not o.startswith("/") for o in operands):
            self.unpin(_UNKNOWN_BASE_IMPORT)
            return
        files: list[str] = []
        for o in dict.fromkeys(operands):
            for cand in self.resolve_candidates(o, lang):
                files.append(cand)
                if self.local_file_exists(cand):
                    break  # later candidates only matter if this one goes away
        self.pin_inline(files)

    def inline_python(self, code: str, *, expanded: bool) -> None:
        names = _python_imports(code)
        if (names is None or expanded) and _PY_IMPORT_RE.search(code):
            self.unpin("inline python code has an import that cannot be resolved statically")
        elif names and self.base_unknown:
            self.unpin(_UNKNOWN_BASE_IMPORT)
        elif names:
            self.pin_inline(_python_import_files(names))

    def inline_node(self, code: str, *, expanded: bool, remote_unpinnable: bool = False) -> None:
        specs, dynamic = _node_specifiers(code, expanded=expanded)
        if dynamic:
            self.unpin("inline node code requires a module that cannot be resolved statically")
            return
        if remote_unpinnable and any(_is_remote(s) for s in specs):
            self.unpin("inline deno code imports a remote specifier")
            return
        local = [s for s in specs if s.startswith(_PATH_PREFIXES)]
        for spec in dict.fromkeys(specs):
            if not spec.startswith(_PATH_PREFIXES) and not _is_node_builtin(spec):
                self.non_durable(_node_modules_reason("require", spec))
        self.pin_inline_paths(local, "node")

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

    def heredoc_stdin(self, lang: str) -> None:
        """A heredoc/here-string script: its text is in the command unless $ or ` assembles it."""
        if self.text_expands():
            self.unpin("heredoc/here-string feeding an interpreter expands $ or backticks")
        elif lang:
            for body in self.heredoc_bodies():
                self.inline_imports(lang, body)

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
                if self.text_expands():
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
        # make exports command-line variables (NAME=value operands) to its recipes
        self.check_assigns(_make_assignments(args))
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
            head = self.xargs_tail(inner, depth)
            tail = self.plan
        finally:
            self.plan = saved
        for reason in tail.non_durable:  # `xargs env VAR=... cmd` loader variables
            self.non_durable(reason)
        name = posixpath.basename(head)
        is_direct = "/" in head and posixpath.dirname(head) not in trusted_bin_dirs()
        if (
            tail.unpinnable
            or any(r.script for r in tail.refs)
            or is_direct
            or _family(name) is not None
            or name in _XARGS_RUNNERS
        ):
            self.unpin("xargs feeds arguments to a script, interpreter or runner")

    def xargs_tail(self, inner: list[str], depth: int) -> str:
        """Analyze the command xargs runs; its head once wrappers (nice, env, ...) are stripped."""
        unwrapped = self.unwrap(inner, depth)
        if unwrapped is None:
            return ""
        restore = self.wd_saved
        try:
            self._command(unwrapped, _Simple(list(unwrapped)), depth)
        finally:
            if restore is not None:
                self.bases, self.base_unknown = restore
        return unwrapped[0]

    def manifest(
        self, values: list[str], names: tuple[str, ...], upward: tuple[str, ...] | None = None
    ) -> None:
        if not values:
            self._ref(".", "upward", upward or names)
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
        collect = frozenset({"--directory", "--project", "--script", "--env-file"})
        collect |= _UV_FILE_FLAGS | _UV_PATHISH_FLAGS
        if _unknown_leading_flag(args, _UV_GLOBAL_FLAGS, _UV_GLOBAL_VALUE):
            self.unpin("unrecognised uv option before the subcommand")
            return
        sub, rest, got = _split_runner(args, _UV_VALUE, collect)
        if sub != "run":
            return
        if _unknown_leading_flag(rest, _UV_RUN_FLAGS, _UV_VALUE):
            self.unpin("unrecognised uv run option before the script")
            return
        task, rest2, got2 = _split_runner(rest, _UV_VALUE, collect)
        got += got2
        dirs = [v for k, v in got if k in ("--directory", "--project")]
        saved = list(self.bases)
        directory = next((v for k, v in reversed(got) if k == "--directory"), None)
        if directory is not None and _literal(directory):
            self.bases = _extend_bases(self.bases, directory)
        try:
            # uv also reads a uv.toml next to the pyproject.toml it finds
            self.manifest(dirs, ("pyproject.toml",), ("pyproject.toml", "uv.toml"))
            extended, self.bases = self.bases, saved
            for d in dirs:
                if _literal(d) and not d.endswith((".toml", ".json")):
                    self._ref(d, "search", ("uv.toml",))
            self.bases = extended
            for k, v in got:
                if k == "--script":
                    self.add_file(v)
                elif k == "--env-file":
                    self.env_file(v)
                elif k in _UV_FILE_FLAGS or k in _UV_PATHISH_FLAGS:
                    self.uv_path(k, v)
            if task is not None and _leading_has(rest, _UV_MODULE, _UV_VALUE):
                inner = ["python", "-m", task, *rest2]
                self.command(inner, _Simple(list(inner)), depth)
            elif task is not None and task.endswith(".py"):
                self.add_file(task)
            elif task is not None:
                self.command([task, *rest2], _Simple([task, *rest2]), depth)
        finally:
            self.bases = saved

    def uv_path(self, flag: str, value: str) -> None:
        """A uv option whose value may be a local file: pin a file, refuse a directory/missing."""
        for part in value.split(",") if flag == "--with" else [value]:
            self.uv_path_value(flag, part)

    def uv_path_value(self, flag: str, value: str) -> None:
        if (
            flag not in _UV_FILE_FLAGS
            and flag not in _UV_ALWAYS_PATH
            and not ("/" in value or value.startswith((".", "~")))
            and not (flag == "--with" and self.names_local_entry(value))
        ):
            return  # a version or package name
        if not _literal(value):
            self.unpin(f"uv {flag} value {value!r} is not a literal path")
            return
        paths = _ref_paths(ScriptRef(value, tuple(self.bases)), self.cwd)
        if any(os.path.isfile(p) for p in paths):
            self.add_file(value, script=False)
        else:
            self.unpin(f"uv {flag} {value} is not a regular file")

    def names_local_entry(self, value: str) -> bool:
        """Whether a bare `uv run --with` value is an archive name or exists on disk."""
        if value.endswith(_ARCHIVE_SUFFIXES):
            return True
        refs = _ref_paths(ScriptRef(value, tuple(self.bases)), self.cwd)
        return any(os.path.lexists(p) for p in refs)

    def env_file(self, path: str) -> None:
        """`uv run --env-file F`: pin F, and a loader variable inside it is non-durable (#98)."""
        self.add_file(path, script=False)
        if not _literal(path):
            return
        for real in _ref_paths(ScriptRef(path, tuple(self.bases)), self.cwd):
            reasons = _env_file_reasons(real, self.cwd)
            if reasons is None:
                self.unpin(f"--env-file {path} is too large to scan for loader variables")
                return
            for reason in reasons:
                self.non_durable(f"{reason} (--env-file {path})")

    def node_pm(self, tool: str, args: list[str], depth: int) -> None:
        collect = frozenset({"--prefix", "--cwd", "--dir", "-C"})
        value = _NPM_VALUE if tool == "npm" else _PM_VALUE
        checked = tool in _NODE_PM_FLAGS
        if checked and _unknown_leading_flag(args, _NODE_PM_FLAGS[tool], _NODE_PM_VALUES[tool]):
            self.unpin(f"unrecognised {tool} option before the subcommand or script")
            return
        sub, rest, got = _split_runner(args, value, collect)
        if sub is None:
            return
        if (
            checked
            and sub in _PM_RUN_BIN
            and _unknown_leading_flag(rest, _NODE_PM_FLAGS[tool], _NODE_PM_VALUES[tool])
        ):
            self.unpin(f"unrecognised {tool} run option before the script")
            return
        members: list[str] = []
        if tool == "yarn" and sub == "workspaces":
            self.unpin("yarn workspaces runs commands in every workspace")
            return
        if tool == "yarn" and sub == "workspace":
            if len(rest) < 2:
                self.unpin("yarn workspace without a name and command")
                return
            members.append(rest[0])
            sub, rest = rest[1], rest[2:]
        runs = sub in _NPM_RUN if tool == "npm" else sub not in _PM_NON_RUN
        if runs and tool == "npm":
            selected = self.npm_trailing(args, sub, rest)
            if selected is None:
                return
            got = [(k, v) for k, v in selected if k == "--prefix"]
            members = [v for _, v in selected]
        elif runs and tool == "pnpm":
            if not self.pnpm_select(args, rest, sub, members):
                return
        if runs:
            self.manifest([v for _, v in got], ("package.json",))
            for member in members:
                self.workspace_member(member)
        if sub in _PM_EXEC or (tool != "npm" and runs) or sub in _PM_RUN_BIN:
            self.pm_inner(sub, rest, depth)

    def npm_trailing(
        self, args: list[str], sub: str, rest: list[str]
    ) -> list[tuple[str, str]] | None:
        """npm parses options after the script name too: (option, value) of --prefix/-w/
        --workspace anywhere before `--`; None (unpinnable) for any other non-allowlisted one."""
        for scan in (args, rest) if sub in _PM_RUN_BIN else (args,):
            if _leading_has(scan, _NPM_ALL_WORKSPACES, _NODE_PM_VALUES["npm"]):
                self.unpin("npm runs the script in every workspace")
                return None
        trailing = rest
        if sub in _PM_RUN_BIN:
            _, trailing, _ = _split_runner(rest, _NPM_VALUE, frozenset())
        i = 0
        while i < len(trailing):
            a = trailing[i]
            if a == "--":
                break
            name, eq, val = a.partition("=")
            off = eq and val == "false" and name in _NODE_PM_FLAGS["npm"]
            if not a.startswith("-") or a == "-" or name in _NPM_TRAILING_FLAGS or off:
                i += 1
            elif name in _NPM_SELECT or name in _NPM_TRAILING_VALUE:
                i += 1 if eq else 2
            else:
                self.unpin(f"unrecognised npm option {name} after the script name")
                return None
        return _scan_flag_values(args, _NPM_SELECT, _NPM_VALUE, whole=True)

    def pnpm_select(self, args: list[str], rest: list[str], sub: str, members: list[str]) -> bool:
        """pnpm --filter/-F values go to `members`; --recursive is unpinnable (False)."""
        value = _NODE_PM_VALUES["pnpm"]
        scans = [args] + ([rest] if sub in _PM_RUN_BIN else [])
        for scan in scans:
            if _leading_has(scan, _PNPM_RECURSIVE, value):
                self.unpin("pnpm --recursive runs the script in every workspace package")
                return False
            members.extend(v for _, v in _scan_flag_values(scan, _PNPM_SELECT, value, whole=False))
        return True

    def workspace_member(self, member: str) -> None:
        """Pin <member>/package.json (npm -w/--prefix, pnpm --filter); refuse a missing one."""
        path = posixpath.join(member, "package.json")
        if not _literal(member):
            self.unpin(f"workspace selector {member!r} is not a literal path")
        elif any(
            os.path.isfile(p) for p in _ref_paths(ScriptRef(path, tuple(self.bases)), self.cwd)
        ):
            self.add_file(path, script=False)
        else:
            self.unpin(f"workspace selector {member!r} does not name a package directory")

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

    def deno(
        self, spec: _Interp, args: list[str], source: tuple[bool, _Simple], depth: int
    ) -> None:
        """`deno run FILE` / `deno eval CODE`; tasks, unknown options and URLs fail closed."""
        heredoc, cmd = source
        if _unknown_leading_flag(args, _DENO_GLOBAL_FLAGS, _DENO_GLOBAL_VALUE):
            self.unpin("unrecognised deno option before the subcommand")
            return
        sub, rest, _ = _split_runner(args, _DENO_GLOBAL_VALUE, frozenset())
        if sub is None or sub in _DENO_PASSIVE:
            return
        if sub not in ("run", "eval"):
            self.unpin(f"deno {sub} runs code that cannot be pinned")
            return
        files, operands, unknown = _deno_options(rest)
        if unknown:
            self.unpin("unrecognised deno option before the script")
            return
        for f in files:
            if _is_remote(f):
                self.unpin("deno config or import map is a remote specifier")
            else:
                self.add_file(f, script=False)
        operand = operands[0] if operands else None
        if operand is None:
            return
        if sub == "run" and _URL_RE.match(operand):
            self.unpin("deno runs a remote or URL specifier")
        elif operand == "-":
            if heredoc:
                self.heredoc_stdin(spec.lang)
            else:
                self.stdin(cmd, explicit=True, depth=depth)
        elif sub == "eval":
            self.inline_code(spec, operand, depth)
        else:
            self.add_file(operand)

    def bun_is_file(self, operand: str) -> bool:
        return (
            operand.startswith(_PATH_PREFIXES)
            or operand.endswith(_BUN_EXTS)
            or self.local_file_exists(operand)
        )

    def bun_args(self, args: list[str], depth: int) -> list[str] | None:
        """bun args to interpret as a script run (`run` dropped); None when handled here.

        `bun run NAME` / `bun NAME` with no such file is an npm-style script: the package.json
        manifest is pinned like `npm run`. `bun x PKG` is `bunx`. Options that change the config
        file or directory are unpinnable.
        """
        sub, rest, got = _split_runner(args, _BUN_VALUE, _BUN_UNPIN)
        if got:
            self.unpin("bun option changes the config file or working directory")
            return None
        if _unknown_leading_flag(args, _BUN_FLAGS, _BUN_VALUE):
            self.unpin("unrecognised bun option before the subcommand or script")
            return None
        if sub is None:
            return args
        if sub == "test":
            self.unpin("bun test runs discovered code that cannot be pinned")
            return None
        if sub in _BUN_PASSIVE:
            return None
        if sub == "x":
            self.implicit_refs(_BUN, _Options())
            self.pm_inner("exec", rest, depth)
            return None
        pos = len(args) - len(rest) - 1
        target = _split_runner(rest, _BUN_VALUE, frozenset())[0] if sub == "run" else sub
        if target is not None and not self.bun_is_file(target):
            self.implicit_refs(_BUN, _Options())
            self.node_pm("bun", args if sub == "run" else [*args[:pos], "run", *args[pos:]], depth)
            return None
        return [*args[:pos], *rest] if sub == "run" else args


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


def _hash_refs(refs: list[ScriptRef], cwd: str) -> tuple[dict[str, str], bool] | None:
    """Hash every candidate path of `refs`: (paths, ok), or None when over the file/byte caps."""
    paths: dict[str, str] = {}
    budget = [MAX_TOTAL_BYTES]
    ok = True
    existing = missing = 0
    for ref in refs:
        candidates = _ref_paths(ref, cwd)
        for path in candidates:
            if path not in paths:
                digest = paths[path] = _file_sha256(path, budget)
                if digest == MISSING:
                    missing += 1
                else:
                    existing += 1
                if existing > MAX_FILES or missing > MAX_MISSING_FILES:
                    return None
        if ref.required and all(paths[p] in _UNHASHED for p in candidates):
            ok = False
    if _OVER_BUDGET in paths.values():
        return None
    return paths, ok


def manifest_pins(command: str, cwd: str) -> dict[str, str] | None | str:
    """Current content of the runner manifest(s) `command` depends on (issue #98).

    {realpath: sha256|missing|unreadable} over the manifest refs only (make, pixi run,
    ... ; ScriptRef.script is False). None when the command has no runner manifest;
    UNPINNABLE when it cannot be pinned (an unreadable manifest counts as that).
    """
    plan = analyze(command, cwd)
    if plan.unpinnable:
        return UNPINNABLE
    refs = [r for r in plan.refs if not r.script]
    if not refs:
        return None
    hashed = _hash_refs(refs, cwd)
    if hashed is None or UNREADABLE in hashed[0].values():
        return UNPINNABLE
    return hashed[0] or None


def compute_pin(command: str, cwd: str) -> ScriptPin | None:
    """Pin the script(s) `command` runs; None if it runs none.

    An unpinnable command gets the UNPINNABLE sentinel (it never matches). One script
    at one path: sha256 is that file's hex digest. Otherwise it is a digest over the
    sorted (realpath, sha256) pairs, so a missing path that is created later changes it.
    """
    plan = analyze(command, cwd)
    if plan.unpinnable or not plan.refs:
        return ScriptPin(UNPINNABLE, {}, ok=False) if plan.unpinnable else None
    hashed = _hash_refs(plan.refs, cwd)
    if hashed is None:
        return ScriptPin(UNPINNABLE, {}, ok=False)
    paths, ok = hashed
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
