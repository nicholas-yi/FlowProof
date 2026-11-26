"""Tokens for Flowproof source text."""

from __future__ import annotations

import textwrap
from dataclasses import dataclass

from .tree import Pos, Ref, Template

KEYWORDS = frozenset(
    {
        "workflow", "end", "needs", "role", "can", "budget", "at", "most", "let", "var", "set",
        "ask", "edit", "exec", "test", "with", "frozen", "choose", "by", "then", "else", "fork",
        "isolated", "into", "keep", "for", "each", "in", "repeat", "until", "if", "require",
        "note", "give", "as", "and", "or", "not", "true", "false", "len",
    }
)

OPERATORS = ("==", "!=", "<=", ">=", "<", ">", ":", ",", ".", "=", "(", ")")
ESCAPES = {'"': '"', "\\": "\\", "n": "\n", "t": "\t", "r": "\r"}


class SyntaxProblem(Exception):
    """A located syntax error with a diagnostic code."""

    def __init__(self, message: str, pos: Pos, code: str = "E100"):
        super().__init__(message)
        self.message = message
        self.pos = pos
        self.code = code


@dataclass(frozen=True)
class Comment:
    line: int
    text: str  # including the leading '#'
    own_line: bool  # nothing but whitespace before it on its line


@dataclass(frozen=True)
class Token:
    kind: str  # "name", "keyword", "number", "string", "op", "nl", "eof"
    text: str
    pos: Pos
    template: Template | None = None

    def is_(self, kind: str, text: str | None = None) -> bool:
        return self.kind == kind and (text is None or self.text == text)


def _is_name_start(ch: str) -> bool:
    return ch == "_" or ("a" <= ch <= "z") or ("A" <= ch <= "Z")


def _is_name_char(ch: str) -> bool:
    return _is_name_start(ch) or (ch.isdigit() and ch.isascii())


def parse_template(raw: str, pos: Pos) -> Template:
    """Process escapes and `{name.field}` interpolation in string contents."""
    parts: list[str | Ref] = []
    buf: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\":
            if i + 1 >= len(raw) or raw[i + 1] not in ESCAPES:
                bad = raw[i : i + 2]
                raise SyntaxProblem(f"unknown escape {bad!r} in string", pos)
            buf.append(ESCAPES[raw[i + 1]])
            i += 2
        elif ch == "{" and raw.startswith("{{", i):
            buf.append("{")
            i += 2
        elif ch == "}" and raw.startswith("}}", i):
            buf.append("}")
            i += 2
        elif ch == "{":
            close = raw.find("}", i)
            if close < 0:
                raise SyntaxProblem("unclosed '{' in string (write '{{' for a brace)", pos)
            inner = raw[i + 1 : close].strip()
            path = tuple(inner.split("."))
            if not inner or not all(
                p and _is_name_start(p[0]) and all(_is_name_char(c) for c in p) for p in path
            ):
                raise SyntaxProblem(f"cannot interpolate {{{inner}}}: use {{name}} or {{name.field}}",
                                    pos)
            if buf:
                parts.append("".join(buf))
                buf = []
            parts.append(Ref(path, pos))
            i = close + 1
        elif ch == "}":
            raise SyntaxProblem("unmatched '}' in string (write '}}' for a brace)", pos)
        else:
            buf.append(ch)
            i += 1
    if buf:
        parts.append("".join(buf))
    return Template(tuple(parts), pos)


def _dedent_triple(raw: str) -> str:
    if raw.startswith("\r\n"):
        raw = raw[2:]
    elif raw.startswith("\n"):
        raw = raw[1:]
    lines = raw.split("\n")
    if len(lines) > 1 and not lines[-1].strip():
        lines.pop()
    return textwrap.dedent("\n".join(line.rstrip("\r") for line in lines))


class _Lexer:
    def __init__(self, text: str):
        self.text = text
        self.i, self.line, self.col = 0, 1, 1
        self.depth = 0  # open parentheses: newlines inside them do not end a statement
        self.tokens: list[Token] = []
        self.comments: list[Comment] = []
        self.code_line = 0  # the line on which the last non-newline token ended

    @property
    def here(self) -> Pos:
        return Pos(self.line, self.col)

    def advance(self, n: int) -> None:
        for _ in range(n):
            if self.text[self.i] == "\n":
                self.line, self.col = self.line + 1, 1
            else:
                self.col += 1
            self.i += 1

    def emit(self, kind: str, end: int, template: Template | None = None) -> None:
        self.tokens.append(Token(kind, self.text[self.i : end], self.here, template))
        self.advance(end - self.i)
        self.code_line = self.line

    def run(self) -> list[Token]:
        text = self.text
        while self.i < len(text):
            ch = text[self.i]
            if ch == "\n":
                if self.depth == 0 and self.tokens and not self.tokens[-1].is_("nl"):
                    self.tokens.append(Token("nl", "\n", self.here))
                self.advance(1)
            elif ch in " \t\r":
                self.advance(1)
            elif ch == "#":
                end = text.find("\n", self.i)
                end = len(text) if end < 0 else end
                own = self.code_line < self.line
                self.comments.append(Comment(self.line, text[self.i : end].rstrip(), own))
                self.advance(end - self.i)
            elif text.startswith('"""', self.i):
                self.triple()
            elif ch == '"':
                self.string()
            elif ch.isdigit() and ch.isascii():
                self.number()
            elif _is_name_start(ch):
                end = self.i
                while end < len(text) and _is_name_char(text[end]):
                    end += 1
                word = text[self.i : end]
                self.emit("keyword" if word in KEYWORDS else "name", end)
            else:
                self.operator(ch)
        if self.tokens and not self.tokens[-1].is_("nl"):
            self.tokens.append(Token("nl", "\n", self.here))
        self.tokens.append(Token("eof", "", self.here))
        return self.tokens

    def triple(self) -> None:
        text, here = self.text, self.here
        end = self.i + 3
        while True:
            end = text.find('"""', end)
            if end < 0:
                raise SyntaxProblem('unterminated """ string', here)
            backslashes = 0
            while text[end - 1 - backslashes] == "\\":
                backslashes += 1
            if backslashes % 2 == 0:
                break
            end += 1
        raw = _dedent_triple(text[self.i + 3 : end])
        self.emit("string", end + 3, parse_template(raw, here))

    def string(self) -> None:
        text, here = self.text, self.here
        end = self.i + 1
        while end < len(text) and text[end] != '"':
            if text[end] == "\n":
                raise SyntaxProblem('unterminated string (use """ for several lines)', here)
            end += 2 if text[end] == "\\" else 1
        if end >= len(text):
            raise SyntaxProblem("unterminated string", here)
        self.emit("string", end + 1, parse_template(text[self.i + 1 : end], here))

    def number(self) -> None:
        text = self.text

        def digits(j: int) -> int:
            while j < len(text) and text[j].isdigit() and text[j].isascii():
                j += 1
            return j

        end = digits(self.i)
        if end + 1 < len(text) and text[end] == "." and text[end + 1].isdigit():
            end = digits(end + 1)
        if end < len(text) and _is_name_start(text[end]):
            raise SyntaxProblem(f"malformed number {text[self.i : end + 1]!r}", self.here)
        self.emit("number", end)

    def operator(self, ch: str) -> None:
        op = next((o for o in OPERATORS if self.text.startswith(o, self.i)), None)
        if op is None:
            raise SyntaxProblem(f"unexpected character {ch!r}", self.here)
        if op == "(":
            self.depth += 1
        elif op == ")":
            self.depth = max(0, self.depth - 1)
        self.emit("op", self.i + len(op))


def tokenize(text: str) -> list[Token]:
    return _Lexer(text).run()


def tokenize_with_comments(text: str) -> tuple[list[Token], list[Comment]]:
    lexer = _Lexer(text)
    return lexer.run(), lexer.comments
