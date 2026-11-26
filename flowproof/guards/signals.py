"""Line-level heuristics for test tampering: skip markers, trivial asserts, early exits."""

from __future__ import annotations

import re
from dataclasses import dataclass

SKIP_MARKER = "heuristic-skip-marker"
TRIVIAL_ASSERT = "heuristic-trivial-assert"
EARLY_EXIT = "heuristic-early-exit"

JS_SUFFIXES: tuple[str, ...] = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts")

_LITERAL = r"(?:True|False|None|-?\d+(?:\.\d+)?|'[^'\\]*'|\"[^\"\\]*\")"

_PY_SKIP = re.compile(
    r"\bpytest\.skip\s*\("
    r"|\bpytest\.mark\.skip\b"
    r"|\bskip[iI]f\s*\(\s*(?:True|1)\b"
    r"|\bskipUnless\s*\(\s*(?:False|0|None)\b"
    r"|\bpytest\.mark\.xfail\b"
    r"|\bpytest\.xfail\s*\("
    r"|\bunittest\.skip\b"
    r"|^@skip\b"
    r"|\bunittest\.expectedFailure\b"
    r"|^@expectedFailure\b"
    r"|\.skipTest\s*\("
    r"|\braise\s+(?:unittest\.)?SkipTest\b"
)
_PY_TRIVIAL = re.compile(
    r"^assert\s*\(?\s*(?:True|1|not\s+(?:False|None|0))\s*\)?\s*(?:,|#|$)"
    rf"|^assert\s+(?P<a>{_LITERAL})\s*==\s*(?P=a)\s*(?:,|#|$)"
    r"|\.assertTrue\(\s*(?:True|1)\s*[,)]"
    r"|\.assertFalse\(\s*(?:False|0|None)\s*[,)]"
    rf"|\.assertEquals?\(\s*(?P<b>{_LITERAL})\s*,\s*(?P=b)\s*[,)]"
)
_PY_EXIT = re.compile(
    r"(?:\bsys\.exit|\bos\._exit|(?<![\w.])exit|(?<![\w.])quit)\s*\(\s*(?:0|None|False)?\s*\)"
    r"|\braise\s+SystemExit\b(?:\s*\(\s*(?:0|None|False)?\s*\))?\s*(?:#|$)"
    r"|\bpytest\.exit\s*\("
)
_JS_SKIP = re.compile(
    r"\b(?:it|test|describe|context|suite)\.(?:skip|todo|only)\s*\("
    r"|(?<![\w.])x(?:it|test|describe)\s*\("
    r"|(?<![\w.])f(?:it|describe)\s*\("
)
_JS_TRIVIAL = re.compile(
    r"\bexpect\(\s*(true|1)\s*\)\.(?:toBe|toEqual|toStrictEqual)\(\s*\1\s*\)"
    r"|(?<![\w.])assert(?:\.ok)?\(\s*true\s*\)"
)
_JS_EXIT = re.compile(r"\bprocess\.exit\s*\(\s*0?\s*\)")


@dataclass(frozen=True)
class Signal:
    kind: str
    line: str  # the stripped source line
    lineno: int


def scannable(path: str) -> bool:
    return path.endswith(".py") or path.endswith(JS_SUFFIXES)


def signals(path: str, data: bytes, *, test_file: bool) -> list[Signal]:
    """Suspicious lines in a file, in line order.

    Skip markers are looked for in every Python file (a helper module can skip
    tests too); trivial asserts and early exits only in test files. JavaScript and
    TypeScript files are scanned only when they are test files.
    """
    if path.endswith(".py"):
        skip, trivial, early, comment = _PY_SKIP, _PY_TRIVIAL, _PY_EXIT, "#"
    elif path.endswith(JS_SUFFIXES) and test_file:
        skip, trivial, early, comment = _JS_SKIP, _JS_TRIVIAL, _JS_EXIT, "//"
    else:
        return []
    found: list[Signal] = []
    for lineno, raw in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(comment):
            continue
        if skip.search(line):
            found.append(Signal(SKIP_MARKER, line, lineno))
        if test_file and trivial.search(line):
            found.append(Signal(TRIVIAL_ASSERT, line, lineno))
        if test_file and early.search(line):
            found.append(Signal(EARLY_EXIT, line, lineno))
    return found
