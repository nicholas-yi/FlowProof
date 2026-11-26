"""Canonical formatting: `parse(format_workflow(w)) == w` and formatting is idempotent."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from .tree import (
    Ask,
    BinOp,
    Bool,
    Choose,
    Do,
    Each,
    Edit,
    Exec,
    Expr,
    Field,
    Fork,
    Give,
    If,
    Len,
    Let,
    Name,
    Not,
    Note,
    Num,
    Ref,
    Repeat,
    Require,
    Set,
    Stmt,
    Str,
    Template,
    Test,
    Value,
    Var,
    Workflow,
)

INDENT = "  "
_PRECEDENCE = {"or": 1, "and": 2, "not": 3, "cmp": 4, "atom": 5}


def _escape(text: str, *, triple: bool) -> str:
    out = text.replace("\\", "\\\\").replace("{", "{{").replace("}", "}}")
    if not triple:
        out = out.replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    elif '"""' in text or text.endswith('"'):
        out = out.replace('"', '\\"')
    return out.replace("\r", "\\r")


def _ref(ref: Ref) -> str:
    return "{" + ".".join(ref.path) + "}"


def _fits_triple(template: Template) -> bool:
    flat = "".join(p if isinstance(p, str) else "x" for p in template.parts)
    if "\n" not in flat or "\r" in flat:
        return False
    return all(not line or (not line[0].isspace() and line == line.rstrip()) for line in flat.split("\n"))


def template(t: Template, depth: int = 0) -> str:
    if _fits_triple(t):
        body = "".join(_escape(p, triple=True) if isinstance(p, str) else _ref(p) for p in t.parts)
        pad = INDENT * (depth + 1)
        lines = [pad + line if line else "" for line in body.split("\n")]
        return '"""\n' + "\n".join(lines) + "\n" + pad + '"""'
    body = "".join(_escape(p, triple=False) if isinstance(p, str) else _ref(p) for p in t.parts)
    return '"' + body + '"'


def _number(value: int | float) -> str:
    if isinstance(value, float):
        text = format(Decimal(repr(value)), "f")
        return text[:-2] if text.endswith(".0") else text
    return str(value)


def _level(expr: Expr) -> int:
    if isinstance(expr, BinOp):
        return _PRECEDENCE[expr.op] if expr.op in ("and", "or") else _PRECEDENCE["cmp"]
    if isinstance(expr, Not):
        return _PRECEDENCE["not"]
    return _PRECEDENCE["atom"]


def expr(e: Expr, depth: int = 0) -> str:
    if isinstance(e, Num):
        return _number(e.value)
    if isinstance(e, Str):
        return template(e.template, depth)
    if isinstance(e, Bool):
        return "true" if e.value else "false"
    if isinstance(e, Name):
        return e.name
    if isinstance(e, Field):
        base = expr(e.base, depth)
        return f"{base if _level(e.base) == _PRECEDENCE['atom'] else '(' + base + ')'}.{e.name}"
    if isinstance(e, Len):
        return f"len({expr(e.arg, depth)})"
    if isinstance(e, Not):
        inner = expr(e.arg, depth)
        return f"not {inner if _level(e.arg) >= _PRECEDENCE['not'] else '(' + inner + ')'}"
    mine = _level(e)
    if e.op in ("and", "or"):
        # Left-associative: the left side may be at the same level, the right must be tighter.
        left = _wrap(e.left, mine, depth, strict=False)
        right = _wrap(e.right, mine, depth, strict=True)
    else:
        left = _wrap(e.left, mine, depth, strict=True)
        right = _wrap(e.right, mine, depth, strict=True)
    return f"{left} {e.op} {right}"


def _wrap(e: Expr, level: int, depth: int, *, strict: bool) -> str:
    text = expr(e, depth)
    inner = _level(e)
    return f"({text})" if inner < level or (strict and inner == level) else text


def value(v: Value, depth: int = 0) -> str:
    if isinstance(v, Ask):
        kind = "" if v.kind == "text" else f" as {v.kind}"
        return f"ask {v.role}: {template(v.prompt, depth)}{kind}"
    if isinstance(v, Edit):
        return f"edit {v.role}: {template(v.prompt, depth)}"
    if isinstance(v, Exec):
        return f"exec {template(v.command, depth)}"
    if isinstance(v, Test):
        frozen = ""
        if v.frozen:
            frozen = " frozen " + ", ".join(template(f, depth) for f in v.frozen)
        return f"test {expr(v.subject, depth)} with {template(v.command, depth)}{frozen}"
    if isinstance(v, Choose):
        policies = ", then ".join(
            f"pairwise {p.role}" if p.name == "pairwise" else p.name for p in v.policies
        )
        return f"choose {expr(v.subject, depth)} by {policies} else {v.fallback}"
    return expr(v, depth)


Notes = Mapping[tuple[int, str], tuple[str, ...]]


def _notes(notes: Notes, node: object | None, slot: str) -> tuple[str, ...]:
    return notes.get((0 if node is None else id(node), slot), ())


def _with_trailing(lines: list[str], trailing: tuple[str, ...], pad: str, *,
                   header: bool) -> list[str]:
    """Attach end-of-line comments without ever placing one inside a string."""
    if not trailing:
        return lines
    index = 0 if header else len(lines) - 1
    if header and '"""' in lines[0]:
        return [pad + t for t in trailing] + lines
    out = list(lines)
    out[index] = f"{out[index]}  {trailing[0]}"
    return [pad + t for t in trailing[1:]] + out


def _block(body: tuple[Stmt, ...], depth: int, notes: Notes) -> list[str]:
    lines: list[str] = []
    for index, stmt in enumerate(body):
        is_block = isinstance(stmt, (Fork, Each, Repeat, If))
        if is_block and index > 0 and lines and lines[-1]:
            lines.append("")
        pad = INDENT * depth
        lines += [pad + c for c in _notes(notes, stmt, "lead")]
        lines += statement(stmt, depth, notes)
        if is_block and index < len(body) - 1:
            lines.append("")
    return lines


def _closing(node: object, word: str, depth: int, notes: Notes, line: str) -> list[str]:
    inner = INDENT * (depth + 1)
    lead = [inner + c for c in _notes(notes, node, word)]
    return lead + _with_trailing([line], _notes(notes, node, f"trail-{word}"), inner,
                                 header=False)


def statement(s: Stmt, depth: int, notes: Notes | None = None) -> list[str]:
    notes = notes or {}
    pad = INDENT * depth
    trailing = _notes(notes, s, "trail")
    if isinstance(s, (Fork, Each, Repeat, If)):
        return _block_statement(s, depth, notes, trailing)
    if isinstance(s, (Let, Var, Set)):
        word = {Let: "let", Var: "var", Set: "set"}[type(s)]
        text = f"{pad}{word} {s.name} = {value(s.value, depth)}"
    elif isinstance(s, Do):
        text = pad + value(s.action, depth)
    elif isinstance(s, Require):
        text = f"{pad}require {expr(s.cond, depth)}, {template(s.message, depth)}"
    elif isinstance(s, Note):
        text = f"{pad}note {template(s.message, depth)}"
    else:
        assert isinstance(s, Give)
        text = f"{pad}give {expr(s.value, depth)}"
    return _with_trailing(text.split("\n"), trailing, pad, header=False)


def _block_statement(s: Fork | Each | Repeat | If, depth: int, notes: Notes,
                     trailing: tuple[str, ...]) -> list[str]:
    pad = INDENT * depth
    if isinstance(s, (Fork, Each)):
        if isinstance(s, Fork):
            head = f"{pad}fork {s.count}{' isolated' if s.isolated else ''} into {s.into}"
        else:
            head = (f"{pad}for each {s.var} in {expr(s.source, depth)} at most {s.limit}"
                    f" into {s.into}")
        keep = _closing(s, "keep", depth, notes, f"{pad}{INDENT}keep {expr(s.keep, depth + 1)}")
        lines = [*head.split("\n"), *_block(s.body, depth + 1, notes), *keep]
    elif isinstance(s, Repeat):
        head = f"{pad}repeat at most {s.limit} until {expr(s.until, depth)}"
        lines = [*head.split("\n"), *_block(s.body, depth + 1, notes)]
    else:
        head = f"{pad}if {expr(s.cond, depth)}"
        lines = [*head.split("\n"), *_block(s.then, depth + 1, notes)]
        if s.orelse is not None:
            lines += _closing(s, "else", depth - 1, notes, f"{pad}else")
            lines += _block(s.orelse, depth + 1, notes)
    lines += _closing(s, "end", depth, notes, f"{pad}end")
    header_lines = len(head.split("\n"))
    top = _with_trailing(lines[:header_lines], trailing, pad, header=True)
    return top + lines[header_lines:]


def format_workflow(w: Workflow) -> str:
    notes = w.comments
    lines = [*_notes(notes, None, "lead")]
    lines += _with_trailing([f"workflow {w.name}"], _notes(notes, None, "trail"), "",
                            header=True)
    header_nodes: list[tuple[Sequence[object], str]] = []
    if w.params:
        parts = []
        for p in w.params:
            default = "" if p.default is None else f" = {expr(p.default, 1)}"
            parts.append(f"{p.name}: {p.type}{default}")
        header_nodes.append((list(w.params), f"{INDENT}needs {', '.join(parts)}"))
    header_nodes += [([r], f"{INDENT}role {r.name} can {', '.join(r.caps)}") for r in w.roles]
    if w.budget:
        limits = ", ".join(f"{_number(lim.amount)} {lim.unit}" for lim in w.budget)
        header_nodes.append((list(w.budget), f"{INDENT}budget at most {limits}"))
    for nodes, text in header_nodes:
        lines += [INDENT + c for node in nodes for c in _notes(notes, node, "lead")]
        trailing = tuple(c for node in nodes for c in _notes(notes, node, "trail"))
        lines += _with_trailing(text.split("\n"), trailing, INDENT, header=True)
    if w.body:
        if len(lines) > 1:
            lines.append("")
        lines += _block(w.body, 1, notes)
    lines += [INDENT + c for c in _notes(notes, None, "end")]
    lines += _with_trailing(["end"], _notes(notes, None, "trail-end"), "", header=False)
    lines += list(_notes(notes, None, "after"))
    return "\n".join(lines) + "\n"
