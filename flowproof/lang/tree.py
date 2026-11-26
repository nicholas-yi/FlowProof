"""Syntax tree for Flowproof workflows.

Source positions are excluded from equality so that two parses of
differently formatted but equivalent text compare equal.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Pos:
    line: int
    col: int

    def __str__(self) -> str:
        return f"{self.line}:{self.col}"


NOWHERE = Pos(0, 0)


@dataclass(frozen=True)
class Ref:
    """`{a.b}` inside a string."""

    path: tuple[str, ...]
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


@dataclass(frozen=True)
class Template:
    parts: tuple[str | Ref, ...]
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)

    @property
    def refs(self) -> tuple[Ref, ...]:
        return tuple(p for p in self.parts if isinstance(p, Ref))

    @property
    def literal(self) -> bool:
        return not self.refs

    def text(self) -> str:
        """The literal text; only meaningful when `literal` is true."""
        return "".join(p for p in self.parts if isinstance(p, str))


@dataclass(frozen=True)
class Num:
    value: int | float
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


@dataclass(frozen=True)
class Str:
    template: Template
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


@dataclass(frozen=True)
class Bool:
    value: bool
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


@dataclass(frozen=True)
class Name:
    name: str
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


@dataclass(frozen=True)
class Field:
    base: Expr
    name: str
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


@dataclass(frozen=True)
class Len:
    arg: Expr
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


@dataclass(frozen=True)
class Not:
    arg: Expr
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


@dataclass(frozen=True)
class BinOp:
    op: str  # "and", "or", "==", "!=", "<", "<=", ">", ">="
    left: Expr
    right: Expr
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


Expr = Num | Str | Bool | Name | Field | Len | Not | BinOp

ASK_KINDS = ("text", "number", "bool", "list")


@dataclass(frozen=True)
class Ask:
    role: str
    prompt: Template
    kind: str = "text"
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


@dataclass(frozen=True)
class Edit:
    role: str
    prompt: Template
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


@dataclass(frozen=True)
class Exec:
    command: Template
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


Action = Ask | Edit | Exec


@dataclass(frozen=True)
class Test:
    subject: Expr
    command: Template
    frozen: tuple[Template, ...] = ()
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


POLICIES = ("tests", "behavior", "exact", "pairwise")
FALLBACKS = ("abstain", "first")


@dataclass(frozen=True)
class Policy:
    name: str
    role: str | None = None
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


@dataclass(frozen=True)
class Choose:
    subject: Expr
    policies: tuple[Policy, ...]
    fallback: str = "abstain"
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


Value = Ask | Edit | Exec | Test | Choose | Num | Str | Bool | Name | Field | Len | Not | BinOp


@dataclass(frozen=True)
class Let:
    name: str
    value: Value
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


@dataclass(frozen=True)
class Var:
    """`var NAME = value`: a name that `set` may rebind."""

    name: str
    value: Value
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


@dataclass(frozen=True)
class Set:
    name: str
    value: Value
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


@dataclass(frozen=True)
class Do:
    action: Action
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


@dataclass(frozen=True)
class Fork:
    count: int
    isolated: bool
    into: str
    body: tuple[Stmt, ...]
    keep: Expr
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


@dataclass(frozen=True)
class Each:
    var: str
    source: Expr
    limit: int
    into: str
    body: tuple[Stmt, ...]
    keep: Expr
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


@dataclass(frozen=True)
class Repeat:
    limit: int
    until: Expr
    body: tuple[Stmt, ...]
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


@dataclass(frozen=True)
class If:
    cond: Expr
    then: tuple[Stmt, ...]
    orelse: tuple[Stmt, ...] | None = None
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


@dataclass(frozen=True)
class Require:
    cond: Expr
    message: Template
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


@dataclass(frozen=True)
class Note:
    message: Template
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


@dataclass(frozen=True)
class Give:
    value: Expr
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


Stmt = Let | Var | Set | Do | Fork | Each | Repeat | If | Require | Note | Give

PARAM_TYPES = ("text", "number", "bool")
CAPABILITIES = ("answer", "edit", "shell")


@dataclass(frozen=True)
class Param:
    name: str
    type: str
    default: Num | Str | Bool | None = None
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


@dataclass(frozen=True)
class Role:
    name: str
    caps: tuple[str, ...]
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)

    @property
    def can_answer(self) -> bool:
        return "answer" in self.caps or "edit" in self.caps

    @property
    def can_edit(self) -> bool:
        return "edit" in self.caps


@dataclass(frozen=True)
class Limit:
    amount: int | float
    unit: str
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)


@dataclass(frozen=True)
class Workflow:
    name: str
    params: tuple[Param, ...]
    roles: tuple[Role, ...]
    budget: tuple[Limit, ...]
    body: tuple[Stmt, ...]
    path: str = field(default="<input>", compare=False)
    pos: Pos = field(default=NOWHERE, compare=False, repr=False)
    # Comments by (id of the node they belong to, or 0 for the workflow; slot). Slots are
    # "lead" (lines before), "trail" (end of the line), "else", "keep", "end" and "after".
    comments: Mapping[tuple[int, str], tuple[str, ...]] = field(
        default_factory=dict, compare=False, repr=False
    )

    def role(self, name: str) -> Role | None:
        return next((r for r in self.roles if r.name == name), None)

    def ceiling(self, unit: str) -> int | float | None:
        return next((lim.amount for lim in self.budget if lim.unit == unit), None)


def children(stmt: Stmt) -> tuple[tuple[Stmt, ...], ...]:
    """The statement blocks directly nested in `stmt`."""
    if isinstance(stmt, (Fork, Each, Repeat)):
        return (stmt.body,)
    if isinstance(stmt, If):
        return (stmt.then,) if stmt.orelse is None else (stmt.then, stmt.orelse)
    return ()


def walk(body: tuple[Stmt, ...]):
    """Every statement in `body`, depth first, in source order."""
    for stmt in body:
        yield stmt
        for block in children(stmt):
            yield from walk(block)
