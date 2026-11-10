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

_OPENER_RE = re.compile(
    r"<<-?[ \t]*(?:'(\w+)'|\"(\w+)\"|\\?([A-Za-z_]\w*))",
)
_HERESTRING_WORD_RE = re.compile(r"[ \t]*(?:'[^']*'|\"[^\"]*\"|[^\s;&|()<>]+)")
_COMMENT_PREV = frozenset(" \t\n;&|()")
_OPERATOR_END = frozenset("&|;(")
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


@dataclass(frozen=True)
class Prepared:
    text: str
    sentinels: dict[str, str]  # heredoc sentinel -> "<heredoc>"
    warnings: tuple[str, ...]
    ok: bool  # False: a quote was left open


def decode_ansi_c(raw: str) -> str:
    """Decode the body of `$'...'` (bash ANSI-C quoting). NULs are dropped."""
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
                out.append(chr(int(m.group(), 16)))
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


class _Scanner:
    def __init__(self, text: str, *, sentinel_start: int) -> None:
        self.s = text
        self.n = len(text)
        self.i = 0
        self.out: list[str] = []
        self.sep_pending = False
        self.pending: list[str] = []  # heredoc delimiters awaiting their bodies
        self.sentinels: dict[str, str] = {}
        self.sentinel_next = sentinel_start
        self.warnings: list[str] = []
        self.ok = True
        self.arith = 0  # depth of $(( ... )): `<<` there is a shift, not a heredoc

    # -- output ------------------------------------------------------------

    def _last(self) -> str:
        for piece in reversed(self.out):
            stripped = piece.rstrip(" \t")
            if stripped:
                return stripped[-1]
        return ""

    def _emit(self, piece: str) -> None:
        if self.sep_pending and piece.strip(" \t"):
            self.sep_pending = False
            if self._last() not in _OPERATOR_END and self._last() != "":
                self.out.append(" ; ")
        self.out.append(piece)

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
            elif c == "$" and s.startswith("$((", self.i):
                self.arith += 1
                self._emit("$((")
                self.i += 3
            elif c == ")" and self.arith and s.startswith("))", self.i):
                self.arith -= 1
                self._emit("))")
                self.i += 2
            elif c == "#" and self._at_word_start():
                self._comment()
            elif c == "\n":
                self._newline()
            elif c == "<" and not self.arith and s.startswith("<<", self.i):
                self._heredoc()
            else:
                self._emit(c)
                self.i += 1

    def _at_word_start(self) -> bool:
        return self.i == 0 or self.s[self.i - 1] in _COMMENT_PREV or not self.out

    def _comment(self) -> None:
        end = self.s.find("\n", self.i)
        self.i = self.n if end < 0 else end

    def _backslash(self) -> None:
        nxt = self.s[self.i + 1] if self.i + 1 < self.n else ""
        if nxt == "\n":
            self.i += 2  # line continuation: joined with no space
        elif nxt:
            self._emit(self.s[self.i : self.i + 2])
            self.i += 2
        else:
            self._emit("\\")
            self.i += 1

    def _single_quote(self) -> None:
        end = self.s.find("'", self.i + 1)
        if end < 0:
            self.ok = False
            end = self.n - 1
        self._emit(self.s[self.i : end + 1])
        self.i = end + 1

    def _double_quote(self) -> None:
        s = self.s
        j = self.i + 1
        piece = ['"']
        while j < self.n:
            c = s[j]
            if c == "\\" and j + 1 < self.n:
                if s[j + 1] != "\n":  # backslash-newline is joined inside double quotes
                    piece.append(s[j : j + 2])
                j += 2
                continue
            piece.append(c)
            j += 1
            if c == '"':
                self._emit("".join(piece))
                self.i = j
                return
        self.ok = False
        self._emit("".join(piece))
        self.i = self.n

    def _ansi_quote(self) -> None:
        s = self.s
        j = self.i + 2
        while j < self.n and s[j] != "'":
            j += 2 if s[j] == "\\" else 1
        if j >= self.n:
            self.ok = False
            j = self.n
        decoded = decode_ansi_c(s[self.i + 2 : j])
        self._emit("'" + decoded.replace("'", "'\\''") + "'")
        self.i = j + 1

    def _newline(self) -> None:
        self.i += 1
        if self.pending:
            self._consume_bodies()
        self.sep_pending = True

    # -- heredocs --------------------------------------------------------------

    def _sentinel(self) -> str:
        name = f"{SENTINEL_PREFIX}{self.sentinel_next}"
        self.sentinel_next += 1
        self.sentinels[name] = "<heredoc>"
        return name

    def _heredoc(self) -> None:
        # Only the body is removed; the rest of the opener line stays normal shell text.
        s = self.s
        if s.startswith("<<<", self.i):
            m = _HERESTRING_WORD_RE.match(s, self.i + 3)
            self._emit(self._sentinel())
            self.i = m.end() if m else self.i + 3
            return
        m = _OPENER_RE.match(s, self.i)
        if m is None:
            self._emit("<<")
            self.i += 2
            return
        self.pending.append(next(g for g in m.groups() if g))
        self._emit(self._sentinel())
        self.i = m.end()

    def _consume_bodies(self) -> None:
        for delim in self.pending:
            while True:
                if self.i >= self.n:
                    self.warnings.append("unterminated_heredoc")
                    self.ok = False  # the body cannot be delimited
                    break
                end = self.s.find("\n", self.i)
                line = self.s[self.i : self.n if end < 0 else end]
                self.i = self.n if end < 0 else end + 1
                if line.strip() == delim:
                    break
        self.pending = []


def prepare(command: str, *, sentinel_start: int = 0) -> Prepared:
    """Resolve continuations, comments, ANSI-C quotes, heredocs and newlines once.

    Heredoc handling removes only the BODY (the lines up to the delimiter); the rest of the
    opener line (`&& cmd`, `; cmd`, `| cmd`, redirects) stays in the text, so every
    segment of it is classified. A heredoc whose terminator is missing makes `ok` False.
    """
    scanner = _Scanner(command, sentinel_start=sentinel_start)
    scanner.run()
    if scanner.pending:  # opener on the last line: no body, no terminator
        scanner.warnings.append("unterminated_heredoc")
        scanner.ok = False
    warnings = tuple(dict.fromkeys(scanner.warnings))
    return Prepared("".join(scanner.out).strip(), scanner.sentinels, warnings, scanner.ok)
