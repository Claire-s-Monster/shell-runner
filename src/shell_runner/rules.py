"""User-configurable deny rules and protected-path rules.

Loaded from TOML files under the shell-runner config dir:

    <config_dir>/rules.toml            global rules (+ optional default_agent_cap)
    <config_dir>/projects/*.toml       per-project rules, applied when the
                                       command's cwd is under one of `roots`

This is an ACCIDENT GUARDRAIL and MEASUREMENT aid, not a security boundary:
shell parsing is best-effort and the files live under the same OS user as the
agents. See README "Rules files".

FAIL CLOSED: if any rules file fails to parse or validate, evaluate() reports a
config error and the classifier denies every command.

Public API:
    config_dir() -> Path
    evaluate(command, cwd) -> RulesVerdict
    configured_default_agent_cap() -> Tier | None
    glob_to_regex(glob) -> re.Pattern[str]
"""

from __future__ import annotations

import os
import re
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .catalog import Tier
from .write_targets import CommandPaths, analyze

Mode = Literal["deny", "flag"]
Kind = Literal["write", "touch"]

CONFIG_DIR_ENV = "SHELL_RUNNER_CONFIG_DIR"

_GLOBAL_KEYS = {"deny", "protected", "default_agent_cap"}
_PROJECT_KEYS = {"deny", "protected", "roots"}
_DENY_KEYS = {"pattern", "reason"}
_PROTECTED_KEYS = {"glob", "mode", "reason"}
_PATH_CANDIDATE_RE = re.compile(r"[^\s'\"();,=\[\]{}<>|&]+")


class RulesConfigError(Exception):
    """A rules file is unreadable or invalid."""


def config_dir() -> Path:
    """SHELL_RUNNER_CONFIG_DIR if set, else ${XDG_CONFIG_HOME:-~/.config}/shell-runner."""
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "shell-runner"


# ---------------------------------------------------------------------------
# Glob matching
# ---------------------------------------------------------------------------


def _translate_class(glob: str, i: int) -> tuple[str, int]:
    """Translate a [...] class starting at glob[i]; returns (regex, next_index)."""
    j = i + 1
    if j < len(glob) and glob[j] in "!^":
        j += 1
    if j < len(glob) and glob[j] == "]":
        j += 1
    while j < len(glob) and glob[j] != "]":
        j += 1
    if j >= len(glob):
        return re.escape("["), i + 1
    body = glob[i + 1 : j]
    if body[:1] in ("!", "^"):
        body = "^" + body[1:]
    return "[" + body.replace("\\", "\\\\") + "]", j + 1


def _translate(glob: str) -> str:
    out: list[str] = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            j = i
            while j < n and glob[j] == "*":
                j += 1
            at_seg_start = i == 0 or glob[i - 1] == "/"
            if j - i >= 2 and at_seg_start and glob[j : j + 1] == "/":
                out.append("(?:.*/)?")  # `**/` spans zero or more directories
                i = j + 1
            elif j - i >= 2:
                out.append(".*")
                i = j
            else:
                out.append("[^/]*")
                i = j
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            piece, i = _translate_class(glob, i)
            out.append(piece)
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def glob_to_regex(glob: str) -> re.Pattern[str]:
    """fnmatch-style glob -> regex (use fullmatch); `**` spans directories.

    `*` and `?` do not cross `/`. A trailing `/**` also matches the directory
    itself, so `rm -r tests/acceptance` is caught by `tests/acceptance/**`.
    """
    if glob.endswith("/**"):
        return re.compile(_translate(glob[:-3]) + "(?:/.*)?", re.DOTALL)
    return re.compile(_translate(glob), re.DOTALL)


# ---------------------------------------------------------------------------
# Parsed rule model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DenyRule:
    pattern: str
    regex: re.Pattern[str]
    reason: str
    source_file: str


@dataclass(frozen=True)
class ProtectedRule:
    glob: str  # as written in the file
    abs_glob: str  # resolved against the project root (or absolute/~ in global)
    regex: re.Pattern[str]
    mode: Mode
    reason: str
    source_file: str


@dataclass(frozen=True)
class PathMatch:
    path: str
    glob: str
    mode: str
    kind: str  # "write" | "touch"
    source_file: str
    reason: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "glob": self.glob,
            "mode": self.mode,
            "kind": self.kind,
            "source_file": self.source_file,
        }


@dataclass(frozen=True)
class _RulesFile:
    path: str
    roots: tuple[str, ...] | None  # None for the global file
    deny: tuple[DenyRule, ...]
    protected: tuple[ProtectedRule, ...]


@dataclass(frozen=True)
class _Loaded:
    global_file: _RulesFile | None
    projects: tuple[_RulesFile, ...]
    default_agent_cap: Tier | None
    error: str | None


@dataclass(frozen=True)
class RulesVerdict:
    """Outcome of evaluating the rules for one (command, cwd)."""

    config_error: str | None = None
    deny: DenyRule | None = None
    matches: tuple[PathMatch, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def write_matches(self) -> tuple[PathMatch, ...]:
        return tuple(m for m in self.matches if m.kind == "write")

    @property
    def denying_match(self) -> PathMatch | None:
        for m in self.write_matches:
            if m.mode == "deny":
                return m
        return None

    @property
    def flagged(self) -> bool:
        return bool(self.write_matches)


# ---------------------------------------------------------------------------
# Parsing / validation
# ---------------------------------------------------------------------------


def _expect_table_list(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw = data.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(e, dict) for e in raw):
        raise RulesConfigError(f"[[{key}]] must be an array of tables")
    return raw


def _check_keys(entry: dict[str, Any], allowed: set[str], what: str) -> None:
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise RulesConfigError(f"unknown key(s) {unknown} in {what}")


def _str_field(entry: dict[str, Any], key: str, what: str, *, required: bool = False) -> str:
    value = entry.get(key, "")
    if key not in entry and required:
        raise RulesConfigError(f"{what} is missing required key {key!r}")
    if not isinstance(value, str):
        raise RulesConfigError(f"{what}: {key!r} must be a string")
    return value


def _parse_deny(entries: list[dict[str, Any]], source: str) -> tuple[DenyRule, ...]:
    rules: list[DenyRule] = []
    for entry in entries:
        _check_keys(entry, _DENY_KEYS, "[[deny]]")
        pattern = _str_field(entry, "pattern", "[[deny]]", required=True)
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise RulesConfigError(
                f"[[deny]] pattern {pattern!r} is not a valid regex: {exc}"
            ) from exc
        rules.append(DenyRule(pattern, regex, _str_field(entry, "reason", "[[deny]]"), source))
    return tuple(rules)


def _real_prefix(abs_glob: str) -> str:
    """realpath() the wildcard-free leading directories so symlinked prefixes
    (matching how candidate paths are resolved) compare equal."""
    parts = abs_glob.split("/")
    fixed = 0
    while fixed < len(parts) and not any(c in parts[fixed] for c in "*?["):
        fixed += 1
    if fixed == len(parts):
        return os.path.realpath(abs_glob)
    head = os.path.realpath("/".join(parts[:fixed]) or "/")
    return "/".join([head.rstrip("/"), *parts[fixed:]])


def _resolve_glob(glob: str, root_for_relative: str | None) -> str:
    if glob.startswith("~"):
        return _real_prefix(os.path.normpath(os.path.expanduser(glob)))
    if os.path.isabs(glob):
        return _real_prefix(os.path.normpath(glob))
    if root_for_relative is None:
        raise RulesConfigError(
            f"relative glob {glob!r} is not allowed in the global rules file "
            "(use an absolute path or ~)"
        )
    return _real_prefix(os.path.normpath(os.path.join(root_for_relative, glob)))


def _parse_protected(
    entries: list[dict[str, Any]], source: str, roots: tuple[str, ...] | None
) -> tuple[ProtectedRule, ...]:
    rules: list[ProtectedRule] = []
    for entry in entries:
        _check_keys(entry, _PROTECTED_KEYS, "[[protected]]")
        glob = _str_field(entry, "glob", "[[protected]]", required=True)
        mode = entry.get("mode", "deny")
        if mode not in ("deny", "flag"):
            raise RulesConfigError(f"[[protected]] mode must be 'deny' or 'flag', got {mode!r}")
        reason = _str_field(entry, "reason", "[[protected]]")
        if roots is None:
            targets = [_resolve_glob(glob, None)]
        else:
            targets = [_resolve_glob(glob, r) for r in roots]
        for abs_glob in targets:
            rules.append(
                ProtectedRule(glob, abs_glob, glob_to_regex(abs_glob), mode, reason, source)
            )
    return tuple(rules)


def _parse_roots(data: dict[str, Any]) -> tuple[str, ...]:
    raw = data.get("roots")
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(r, str) and os.path.isabs(r) for r in raw)
    ):
        raise RulesConfigError("`roots` must be a non-empty list of absolute paths")
    return tuple(os.path.realpath(r) for r in raw)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RulesConfigError(str(exc)) from exc


def _parse_default_cap(data: dict[str, Any]) -> Tier | None:
    name = data.get("default_agent_cap")
    if name is None:
        return None
    if not isinstance(name, str) or name not in Tier.__members__:
        raise RulesConfigError(
            f"default_agent_cap {name!r} is not a tier name (one of {sorted(Tier.__members__)})"
        )
    return Tier[name]


def _parse_file(path: Path, *, is_global: bool) -> tuple[_RulesFile, Tier | None]:
    data = _read_toml(path)
    _check_keys(data, _GLOBAL_KEYS if is_global else _PROJECT_KEYS, "top level")
    roots = None if is_global else _parse_roots(data)
    source = str(path)
    deny = _parse_deny(_expect_table_list(data, "deny"), source)
    protected = _parse_protected(_expect_table_list(data, "protected"), source, roots)
    cap = _parse_default_cap(data) if is_global else None
    return _RulesFile(source, roots, deny, protected), cap


def _project_files(base: Path) -> list[Path]:
    try:
        return sorted(base.glob("*.toml"))
    except OSError:
        return []


def _load(base: Path) -> _Loaded:
    global_path = base / "rules.toml"
    global_file: _RulesFile | None = None
    cap: Tier | None = None
    projects: list[_RulesFile] = []
    current = str(global_path)
    try:
        if global_path.is_file():
            global_file, cap = _parse_file(global_path, is_global=True)
        for p in _project_files(base / "projects"):
            current = str(p)
            projects.append(_parse_file(p, is_global=False)[0])
    except RulesConfigError as exc:
        return _Loaded(None, (), None, f"{current}: {exc}")
    return _Loaded(global_file, tuple(projects), cap, None)


# ---------------------------------------------------------------------------
# Cache (stat on every call; reload when the file set or any mtime changes)
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: tuple[tuple[Any, ...], _Loaded] | None = None


def _signature(base: Path) -> tuple[Any, ...]:
    files = [base / "rules.toml", *_project_files(base / "projects")]
    sig: list[Any] = [str(base)]
    for f in files:
        try:
            st = f.stat()
            sig.append((str(f), st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((str(f), None, None))
    return tuple(sig)


def _loaded() -> _Loaded:
    global _cache
    base = config_dir()
    sig = _signature(base)
    with _cache_lock:
        if _cache is not None and _cache[0] == sig:
            return _cache[1]
        loaded = _load(base)
        _cache = (sig, loaded)
        return loaded


def configured_default_agent_cap() -> Tier | None:
    """default_agent_cap from the global rules file, or None (absent or config error)."""
    return _loaded().default_agent_cap


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _applicable(loaded: _Loaded, real_cwd: str) -> list[_RulesFile]:
    files: list[_RulesFile] = [loaded.global_file] if loaded.global_file else []
    for proj in loaded.projects:
        if proj.roots and any(
            real_cwd == r or real_cwd.startswith(r.rstrip("/") + "/") for r in proj.roots
        ):
            files.append(proj)
    return files


def _resolve(token: str, real_cwd: str) -> str:
    expanded = os.path.expanduser(token)
    return os.path.realpath(os.path.join(real_cwd, expanded))


def _match_path(
    token: str, kind: Kind, rules: list[ProtectedRule], real_cwds: list[str]
) -> list[PathMatch]:
    """Match against every candidate base; a hit on any base counts."""
    if not token:
        return []
    found: list[PathMatch] = []
    for path in dict.fromkeys(_resolve(token, base) for base in real_cwds):
        found += [
            PathMatch(path, r.glob, r.mode, kind, r.source_file, r.reason)
            for r in rules
            if r.regex.fullmatch(path)
        ]
    return found


def _inline_candidates(text: str) -> list[str]:
    return [c for c in _PATH_CANDIDATE_RE.findall(text) if "/" in c or "." in c]


def _command_matches(
    cp: CommandPaths, rules: list[ProtectedRule], real_cwd: str
) -> list[PathMatch]:
    found: list[PathMatch] = []
    bases = [real_cwd, *(_resolve(b, real_cwd) for b in cp.bases)]
    for token in cp.writes:
        found += _match_path(token, "write", rules, bases)
    for text in cp.inline_code:
        for cand in _inline_candidates(text):
            found += _match_path(cand, "write", rules, bases)
    for token in cp.args:
        found += _match_path(token, "touch", rules, bases)
    return found


def _dedupe(matches: list[PathMatch]) -> tuple[PathMatch, ...]:
    """Drop duplicates; a touch is dropped when the same path/glob was a write."""
    writes = {(m.path, m.glob, m.source_file) for m in matches if m.kind == "write"}
    seen: set[tuple[str, str, str, str]] = set()
    out: list[PathMatch] = []
    for m in matches:
        if m.kind == "touch" and (m.path, m.glob, m.source_file) in writes:
            continue
        key = (m.path, m.glob, m.source_file, m.kind)
        if key not in seen:
            seen.add(key)
            out.append(m)
    return tuple(out)


def evaluate(command: str, cwd: str) -> RulesVerdict:
    """Evaluate config deny patterns and protected-path rules for a command."""
    loaded = _loaded()
    if loaded.error is not None:
        return RulesVerdict(config_error=loaded.error)
    real_cwd = os.path.realpath(cwd)
    files = _applicable(loaded, real_cwd)
    if not files:
        return RulesVerdict()
    deny = next((d for f in files for d in f.deny if d.regex.search(command)), None)
    protected = [p for f in files for p in f.protected]
    matches: list[PathMatch] = []
    warnings: list[str] = []
    if protected:
        for cp in analyze(command):
            matches += _command_matches(cp, protected, real_cwd)
            warnings += [w for w in cp.warnings if w not in warnings]
    return RulesVerdict(deny=deny, matches=_dedupe(matches), warnings=tuple(warnings))
