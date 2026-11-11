"""Single shared preprocessing of a raw shell command.

Both the normalizer (catalog classification) and write_targets (path/exec guards) must see
the SAME commands. Before either tokenizes, this module resolves, once and quote-aware:

  * backslash-newline: bash joins `\\<nl>` outside single quotes and inside double quotes
    (no space is inserted); inside single quotes it is literal data;
  * comments: `# ...` at the start of a word, to the end of the line (a backslash does not
    continue a comment), so an apostrophe in a comment cannot open a quote;
  * ANSI-C quotes `$'..'`: escapes are decoded and the result is re-emitted as an ordinary
    single-quoted word, so `\\'` cannot desync a tokenizer and `$'sys\\x74em'` reads `system`;
  * heredocs and here-strings: openers outside quotes become a sentinel and the bodies are
    removed, so body text (apostrophes, `<<`) never reaches a tokenizer;
  * process substitution `<(..)` / `>(..)` outside quotes: the inner command is extracted and
    classified like a `$(..)` body (it is literal inside double quotes and heredoc bodies);
  * newlines outside quotes become ` ; ` (a command separator), except after an operator
    that already continues the command (`&&`, `||`, `|`, `;`, `&`, `(`); a newline inside
    quotes stays data (multi-line sed/awk scripts).

Best effort, like the rest of the guards: `Prepared.ok` is False when a quote is left open,
and callers must then treat the command as unparseable (fail closed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SENTINEL_PREFIX = "__TKSENTINEL_"

_MAX_DEPTH = 8  # nesting of extracted substitutions; deeper is unparseable (fail closed)
_WORD_STOP = frozenset(" \t\n;&|()<>")
_COMMENT_PREV = frozenset(" \t\n;&|()")
_OPERATOR_END = frozenset("&|;(")
_BACKTICK_ESCAPE_RE = re.compile(r"\\([$`\\])")
_SIMPLE_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "?": "?",
}
_HEX_LENGTHS = {"x": 2, "u": 4, "U": 8}
_MAX_CODE_POINT = 0x10FFFF


@dataclass(frozen=True)
class Prepared:
    text: str
    sentinels: dict[str, str]  # heredoc sentinel -> "<heredoc>"
    warnings: tuple[str, ...]
    ok: bool  # False: a quote was left open


def decode_ansi_c(raw: str) -> str:
    """Decode the body of `$'...'` (bash ANSI-C quoting). NULs are dropped.

    Raises ValueError for a code point that is not a valid character (callers fail closed)."""
    out: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        e = raw[i + 1]
        if e in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[e])
            i += 2
        elif e in _HEX_LENGTHS:
            m = re.match(f"[0-9A-Fa-f]{{1,{_HEX_LENGTHS[e]}}}", raw[i + 2 :])
            if m:
                code = int(m.group(), 16)
                if code > _MAX_CODE_POINT or 0xD800 <= code <= 0xDFFF:
                    raise ValueError(f"invalid code point in ANSI-C quote: {m.group()}")
                out.append(chr(code))
                i += 2 + m.end()
            else:
                out.append(raw[i : i + 2])
                i += 2
        elif e in "01234567":
            m = re.match(r"[0-7]{1,3}", raw[i + 1 :])
            digits = m.group() if m else e
            out.append(chr(int(digits, 8) & 0xFF))
            i += 1 + len(digits)
        elif e == "c" and i + 2 < n:
            out.append(chr(ord(raw[i + 2]) & 0x1F))
            i += 3
        else:
            out.append(raw[i : i + 2])
            i += 2
    return "".join(out).replace("\x00", "")


def _scan_close(
    s: str,
    k: int,
    closer: str,
    *,
    quotes: bool,
    subst: bool,
    nest: bool = False,
    comments: bool = False,
) -> int:
    """Index just past the `closer` ending a construct whose body starts at `k`; -1 if none.

    Backslash pairs are skipped. `quotes`: `'..'`, `".."` and `$'..'` are nested constructs
    (not so inside double quotes). `subst`: `$(..)`, `${..}` and backticks are. `nest`
    counts parentheses; `comments` skips `# ...` at a word start (command substitution)."""
    depth, n = 1, len(s)
    while k < n:
        c = s[k]
        if c == "\\":
            k += 2
        elif c == closer:
            depth -= 1
            k += 1
            if depth == 0:
                return k
        elif nest and c == "(":
            depth += 1
            k += 1
        elif comments and c == "#" and s[k - 1] in " \t\n;&|(":
            eol = s.find("\n", k)
            k = n if eol < 0 else eol
        elif (quotes and c in "'\"") or (subst and c in "`$"):
            end = _skip_construct(s, k, quotes=quotes)
            if end is None:
                k += 1
            elif end < 0:
                return -1
            else:
                k = end
        else:
            k += 1
    return -1


def _skip_construct(s: str, k: int, *, quotes: bool) -> int | None:
    """End index of the quote/substitution construct starting at `s[k]` (-1: unterminated),
    or None when `s[k]` does not start one."""
    c = s[k]
    if c == "'":
        end = s.find("'", k + 1)
        return -1 if end < 0 else end + 1
    if c == '"':
        return _scan_close(s, k + 1, '"', quotes=False, subst=True)
    if c == "`":
        return _scan_close(s, k + 1, "`", quotes=False, subst=False)
    two = s[k : k + 2]
    if two == "$'" and quotes:
        return _scan_close(s, k + 2, "'", quotes=False, subst=False)
    if two == "$(":
        return _scan_close(s, k + 2, ")", quotes=True, subst=True, nest=True, comments=True)
    if two == "${":
        return _scan_close(s, k + 2, "}", quotes=True, subst=True)
    return None


def _find_substitutions(text: str, *, quotes: bool) -> tuple[list[str], bool]:
    """Command texts bash would run when it expands `text` (a double-quoted string, an
    unquoted heredoc body or a here-string word): the bodies of `$(..)` and backticks, also
    inside `${..}`, `$((..))` and nested double quotes. `quotes`: `'..'` is inert and `".."`
    nests (a word); False inside double quotes / heredoc bodies. Second item False: an
    unterminated construct (callers fail closed)."""
    found: list[str] = []
    k, n = 0, len(text)
    while k < n:
        c = text[k]
        if c == "\\":
            k += 2
            continue
        if not (c in "`$" or (quotes and c in "'\"")):
            k += 1
            continue
        end = _skip_construct(text, k, quotes=quotes)
        if end is None:
            k += 1
            continue
        if end < 0:
            return found, False
        sub: list[str] = []
        ok = True
        if text.startswith("$((", k):
            body = text[k + 3 : end - 2]
            if text[end - 2 : end] != "))" or ";" in body:
                found.append(text[k + 2 : end - 1])  # `$( (..) )`: a command, not arithmetic
            else:
                sub, ok = _find_substitutions(body, quotes=True)
        elif text.startswith("$(", k):
            found.append(text[k + 2 : end - 1])
        elif c == "`":
            found.append(_BACKTICK_ESCAPE_RE.sub(r"\1", text[k + 1 : end - 1]))
        elif text.startswith("${", k):
            sub, ok = _find_substitutions(text[k + 2 : end - 1], quotes=True)
        elif c == '"':
            sub, ok = _find_substitutions(text[k + 1 : end - 1], quotes=False)
        found += sub
        if not ok:
            return found, False
        k = end
    return found, True


def _defang(piece: str) -> str:
    """A substitution inside double quotes with its quotes turned into spaces and its
    backslash-newlines joined, so that a naive tokenizer cannot see it close the string."""
    out: list[str] = []
    i, n = 0, len(piece)
    while i < n:
        ch = piece[i]
        if ch == "\\":
            if piece[i + 1 : i + 2] != "\n":
                out.append(piece[i : i + 2])
            i += 2
            continue
        out.append(" " if ch in "\"'" else ch)
        i += 1
    return "".join(out)


def _dq_clean(raw: str) -> str:
    """A double-quoted string (opening quote first) as downstream tokenizers must see it:
    backslash-newline joined (bash joins it inside double quotes), and any nested
    substitution holding a double quote defanged so it cannot close the string early."""
    out = ['"']
    k, n = 1, len(raw)
    while k < n:
        c = raw[k]
        if c == "\\":
            if raw[k + 1 : k + 2] != "\n":
                out.append(raw[k : k + 2])
            k += 2
            continue
        end = _skip_construct(raw, k, quotes=False) if c in "`$" else None
        if end is None or end < 0:
            out.append(c)
            k += 1
            continue
        piece = raw[k:end]
        out.append(_defang(piece) if '"' in piece else piece)
        k = end
    return "".join(out)


class _Scanner:
    def __init__(self, text: str, *, sentinel_start: int, depth: int = 0) -> None:
        self.s = text
        self.n = len(text)
        self.i = 0
        self.depth = depth
        self.out: list[str] = []
        self.sep_pending = False
        self.pending: list[tuple[str, bool]] = []  # (delimiter, quoted) awaiting their bodies
        self.sentinels: dict[str, str] = {}
        self.sentinel_next = sentinel_start
        self.warnings: list[str] = []
        self.ok = True
        self.extras: list[str] = []  # command texts hidden in substitutions (classified too)
        self.arith: list[int] = []  # open $(( .. )): paren depth inside; `<<` is a shift there
        self.brace = 0  # open ${ .. }: `#` is never a comment there
        self.bracket = 0  # open $[ .. ] (old arithmetic): likewise
        self.parens: list[str] = []  # open `$(` ("$") and plain `(` ("(")
        self.last_sig = ""  # last significant char emitted, "a" when it was escaped
        self.at_ws = True  # the next char starts a word (a `#` there begins a comment)

    # -- output ------------------------------------------------------------

    def _emit(self, piece: str, *, literal: bool = False, ws: bool | None = None) -> None:
        stripped = piece.rstrip(" \t")
        if self.sep_pending and stripped:
            self.sep_pending = False
            if self.last_sig not in _OPERATOR_END and self.last_sig != "":
                self.out.append(" ; ")
        self.out.append(piece)
        if not piece:
            return
        if stripped:
            self.last_sig = "a" if literal else stripped[-1]
        if ws is None:
            ws = len(piece) == 1 and piece in _COMMENT_PREV and not literal
        self.at_ws = ws

    def _add_extras(self, texts: list[str], ok: bool) -> None:
        self.extras += texts
        if not ok:
            self.ok = False

    def flush_extras(self) -> None:
        """Append every hidden substitution command as a command of its own (at the end:
        classification is per command, so the position is irrelevant)."""
        for inner in self.extras:
            if self.depth >= _MAX_DEPTH:
                self.ok = False
                return
            sub = prepare(inner, sentinel_start=self.sentinel_next, _depth=self.depth + 1)
            self.sentinels.update(sub.sentinels)
            self.sentinel_next += len(sub.sentinels)
            self.warnings += sub.warnings
            self.ok = self.ok and sub.ok
            if sub.text:
                self.sep_pending = True
                self._emit(sub.text, ws=False)

    # -- main loop -----------------------------------------------------------

    def run(self) -> None:
        s = self.s
        while self.i < self.n:
            c = s[self.i]
            if c == "\\":
                self._backslash()
            elif c == "'":
                self._single_quote()
            elif c == '"':
                self._double_quote()
            elif c == "$" and s.startswith("$'", self.i):
                self._ansi_quote()
            elif c == "`":
                self._backtick()
            elif c == "$" and s.startswith("$((", self.i):
                self.arith.append(0)
                self._emit("$((", ws=False)
                self.i += 3
            elif c == "$" and s.startswith("${", self.i):
                self.brace += 1
                self._emit("${", ws=False)
                self.i += 2
            elif c == "$" and s.startswith("$(", self.i):
                if self.arith:
                    self.arith[-1] += 1
                else:
                    self.parens.append("$")
                self._emit("$(", ws=True)
                self.i += 2
            elif c == "$" and s.startswith("$[", self.i):
                self.bracket += 1
                self._emit("$[", ws=False)
                self.i += 2
            elif c == "]" and self.bracket:
                self.bracket -= 1
                self._emit(c, ws=False)
                self.i += 1
            elif c in "<>" and not self.arith and s.startswith("(", self.i + 1):
                self._process_subst()
            elif self.arith and c in "()":
                self._arith_paren(c)
            elif c == "(":
                # `((..))` is arithmetic: a `#` in it is never a comment (the 2nd paren marks it)
                self.parens.append("(")
                self._emit(c)
                self.i += 1
                if s.startswith("(", self.i):
                    self.parens.append("A")
                    self._emit("(")
                    self.i += 1
            elif c == ")":
                top = self.parens.pop() if self.parens else ""
                self._emit(c, ws=top != "$")
                self.i += 1
            elif c == "}" and self.brace:
                self.brace -= 1
                self._emit(c, ws=False)
                self.i += 1
            elif c == "#" and self._at_word_start():
                self._comment()
            elif c == "\n":
                self._newline()
            elif c == "<" and not self.arith and s.startswith("<<", self.i):
                self._heredoc()
            else:
                self._emit(c)
                self.i += 1

    def _arith_paren(self, c: str) -> None:
        """Inside `$(( .. ))`: `(`/`)` nest; the `))` that finds no open `(` ends it."""
        if c == "(":
            self.arith[-1] += 1
            self._emit(c, ws=False)
            self.i += 1
        elif self.arith[-1] == 0 and self.s.startswith("))", self.i):
            self.arith.pop()
            self._emit("))", ws=False)
            self.i += 2
        else:
            self.arith[-1] = max(0, self.arith[-1] - 1)
            self._emit(c, ws=False)
            self.i += 1

    def _at_word_start(self) -> bool:
        return (
            self.at_ws
            and not self.arith
            and not self.brace
            and not self.bracket
            and "A" not in self.parens
        )

    def _process_subst(self) -> None:
        """`<(cmd)` / `>(cmd)`: bash runs `cmd`, so it is classified as a command of its own.
        The text stays (the normalizer templates it), with quotes, escapes and newlines
        defanged so that nothing in it can hide the rest of the command line."""
        s = self.s
        end = _scan_close(s, self.i + 2, ")", quotes=True, subst=True, nest=True, comments=True)
        if end < 0:
            self.ok = False
            end = self.n
        else:
            self.extras.append(s[self.i + 2 : end - 1])
        piece = s[self.i : end]
        if any(ch in piece for ch in "'\"\\\n"):
            piece = _defang(piece).replace("\n", " ; ")
        self._emit(piece, ws=False)
        self.i = end

    def _comment(self) -> None:
        end = self.s.find("\n", self.i)
        self.i = self.n if end < 0 else end

    def _backslash(self) -> None:
        nxt = self.s[self.i + 1] if self.i + 1 < self.n else ""
        if nxt == "\n":
            self.i += 2  # line continuation: joined with no space, word state unchanged
        elif nxt:
            self._emit(self.s[self.i : self.i + 2], literal=True)
            self.i += 2
        else:
            self._emit("\\", literal=True)
            self.i += 1

    def _backtick(self) -> None:
        """`cmd`: opaque to the tokenizers (its `#`, quotes and newlines are not ours to
        interpret); the command inside is classified as a command of its own."""
        end = _skip_construct(self.s, self.i, quotes=True)
        if end is None or end < 0:
            self.ok = False
            end = self.n
        else:
            self.extras.append(_BACKTICK_ESCAPE_RE.sub(r"\1", self.s[self.i + 1 : end - 1]))
        self._emit(self.s[self.i : end], ws=False)
        self.i = end

    def _single_quote(self) -> None:
        end = self.s.find("'", self.i + 1)
        if end < 0:
            self.ok = False
            end = self.n - 1
        self._emit(self.s[self.i : end + 1])
        self.i = end + 1

    def _double_quote(self) -> None:
        end = _skip_construct(self.s, self.i, quotes=False)
        if end is None or end < 0:  # unterminated (or a substitution inside never closes)
            self.ok = False
            end = self.n
            body = self.s[self.i + 1 :]
        else:
            body = self.s[self.i + 1 : end - 1]
        found, ok = _find_substitutions(body, quotes=False)
        self._add_extras(found, ok)
        self._emit(_dq_clean(self.s[self.i : end]))
        self.i = end

    def _ansi_quote(self) -> None:
        s = self.s
        j = self.i + 2
        while j < self.n and s[j] != "'":
            j += 2 if s[j] == "\\" else 1
        if j >= self.n:
            self.ok = False
            j = self.n
        try:
            decoded = decode_ansi_c(s[self.i + 2 : j])
        except ValueError:
            self.ok = False
            decoded = ""
        self._emit("'" + decoded.replace("'", "'\\''") + "'")
        self.i = j + 1

    def _newline(self) -> None:
        self.i += 1
        if self.pending:
            self._consume_bodies()
        self.sep_pending = True
        self.at_ws = True

    # -- heredocs --------------------------------------------------------------

    def _sentinel(self) -> str:
        name = f"{SENTINEL_PREFIX}{self.sentinel_next}"
        self.sentinel_next += 1
        self.sentinels[name] = "<heredoc>"
        return name

    def _read_word(self, j: int) -> tuple[int, str, bool]:
        """The shell word at `j`: (end, quote-removed text, any part quoted). It stops at an
        unquoted blank or operator; quotes and substitutions are consumed whole."""
        s, n = self.s, self.n
        text: list[str] = []
        quoted = False
        while j < n and s[j] not in _WORD_STOP:
            c = s[j]
            end = _skip_construct(s, j, quotes=True) if c in "'\"`$" else None
            if end is not None and end < 0:
                self.ok = False
                return n, "".join(text), quoted
            if c == "\\" and j + 1 < n:
                if s[j + 1] != "\n":  # backslash-newline is joined, not part of the word
                    text.append(s[j + 1])
                    quoted = True
                j += 2
            elif end is None:
                text.append(c)
                j += 1
            else:
                inner = s[j:end]
                if c == "'":
                    inner, quoted = inner[1:-1], True
                elif c == '"':
                    inner, quoted = re.sub(r'\\([$`"\\])', r"\1", inner[1:-1]), True
                elif inner.startswith("$'"):
                    try:
                        inner = decode_ansi_c(inner[2:-1])
                    except ValueError:
                        self.ok = False
                        inner = ""
                    quoted = True
                text.append(inner)
                j = end
        return j, "".join(text), quoted

    def _skip_blanks(self, j: int) -> int:
        while j < self.n and self.s[j] in " \t":
            j += 1
        return j

    def _heredoc(self) -> None:
        # Only the body is removed; the rest of the opener line stays normal shell text.
        s = self.s
        if s.startswith("<<<", self.i):
            start = self._skip_blanks(self.i + 3)
            end, _, _ = self._read_word(start)
            self._emit(self._sentinel())
            found, ok = _find_substitutions(s[start:end], quotes=True)
            self._add_extras(found, ok)
            self.i = end if end > start else self.i + 3
            return
        start = self.i + 2
        if s.startswith("-", start):
            start += 1
        start = self._skip_blanks(start)
        end, delim, quoted = self._read_word(start)
        if end == start:
            self._emit("<<")
            self.i += 2
            return
        self.pending.append((delim, quoted))
        self._emit(self._sentinel())
        self.i = end

    def _read_line(self, *, join: bool) -> str:
        """The next heredoc body line; with `join` (unquoted delimiter) a line ending in an
        odd number of backslashes is joined to the next, as bash does before it compares
        a line with the delimiter."""
        end = self.s.find("\n", self.i)
        line = self.s[self.i : self.n if end < 0 else end]
        self.i = self.n if end < 0 else end + 1
        while join and self.i < self.n and (len(line) - len(line.rstrip("\\"))) % 2:
            end = self.s.find("\n", self.i)
            line = line[:-1] + self.s[self.i : self.n if end < 0 else end]
            self.i = self.n if end < 0 else end + 1
        return line

    def _consume_bodies(self) -> None:
        for delim, quoted in self.pending:
            body: list[str] = []
            closed = False
            while self.i < self.n:
                line = self._read_line(join=not quoted)
                if line.strip() == delim:
                    closed = True
                    break
                body.append(line)
            if not closed:
                self.warnings.append("unterminated_heredoc")
                self.ok = False  # the body cannot be delimited
            elif not quoted:  # an unquoted body runs its $(..) and `..`
                found, ok = _find_substitutions("\n".join(body), quotes=False)
                self._add_extras(found, ok)
        self.pending = []


def prepare(command: str, *, sentinel_start: int = 0, _depth: int = 0) -> Prepared:
    """Resolve continuations, comments, ANSI-C quotes, heredocs and newlines once.

    Heredoc handling removes only the BODY (the lines up to the delimiter); the rest of the
    opener line (`&& cmd`, `; cmd`, `| cmd`, redirects) stays in the text, so every
    segment of it is classified. A heredoc whose terminator is missing makes `ok` False.
    Commands hidden in substitutions (`"$(..)"`, backticks, unquoted heredoc bodies,
    here-strings) are appended as commands of their own, so they are classified too.
    """
    scanner = _Scanner(command, sentinel_start=sentinel_start, depth=_depth)
    try:
        scanner.run()
        scanner.flush_extras()
    except RecursionError:
        scanner.ok = False
    if scanner.pending:  # opener on the last line: no body, no terminator
        scanner.warnings.append("unterminated_heredoc")
        scanner.ok = False
    warnings = tuple(dict.fromkeys(scanner.warnings))
    return Prepared("".join(scanner.out).strip(), scanner.sentinels, warnings, scanner.ok)
