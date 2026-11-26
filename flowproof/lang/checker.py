"""Static checks and call bounds for Flowproof workflows."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

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
    Pos,
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
    walk,
)

# -- types -------------------------------------------------------------------


@dataclass(frozen=True)
class Type:
    kind: str  # text number bool change result graded list any
    item: Type | None = None

    def __str__(self) -> str:
        return f"list of {self.item}" if self.kind == "list" else self.kind


TEXT, NUMBER, BOOL = Type("text"), Type("number"), Type("bool")
CHANGE, RESULT, GRADED, ANY = Type("change"), Type("result"), Type("graded"), Type("any")


def list_of(item: Type) -> Type:
    return Type("list", item)


FIELDS: dict[str, dict[str, Type]] = {
    "change": {"patch": TEXT, "files": list_of(TEXT), "empty": BOOL},
    "result": {"ok": BOOL, "exit": NUMBER, "out": TEXT},
    "graded": {
        "passed": BOOL,
        "claimed": BOOL,
        "tampered": BOOL,
        "false_pass": BOOL,
        "tests_passed": NUMBER,
        "tests_failed": NUMBER,
        "change": CHANGE,
        "patch": TEXT,
        "out": TEXT,
    },
}
SCALARS = {"text": TEXT, "number": NUMBER, "bool": BOOL}
ASK_TYPES = {"text": TEXT, "number": NUMBER, "bool": BOOL, "list": list_of(TEXT)}


def compatible(a: Type, b: Type) -> bool:
    if a.kind == "any" or b.kind == "any":
        return True
    if a.kind != b.kind:
        return False
    if a.kind == "list":
        assert a.item is not None and b.item is not None
        return compatible(a.item, b.item)
    return True


# -- intervals -----------------------------------------------------------------


def _mul(a: int | None, b: int | None) -> int | None:
    if a == 0 or b == 0:
        return 0
    if a is None or b is None:
        return None
    return a * b


@dataclass(frozen=True)
class Interval:
    """A closed range of counts; `hi is None` means unbounded."""

    lo: int
    hi: int | None

    def __add__(self, other: Interval) -> Interval:
        hi = None if self.hi is None or other.hi is None else self.hi + other.hi
        return Interval(self.lo + other.lo, hi)

    def times(self, lo: int, hi: int | None) -> Interval:
        return Interval(_mul(self.lo, lo) or 0, _mul(self.hi, hi))

    def join(self, other: Interval) -> Interval:
        hi = None if self.hi is None or other.hi is None else max(self.hi, other.hi)
        return Interval(min(self.lo, other.lo), hi)

    def __str__(self) -> str:
        if self.hi == self.lo:
            return str(self.lo)
        return f"{self.lo}..{'unbounded' if self.hi is None else self.hi}"

    def to_json(self) -> dict[str, int | None]:
        return {"lo": self.lo, "hi": self.hi}


ZERO, ONE = Interval(0, 0), Interval(1, 1)


@dataclass(frozen=True)
class Cost:
    """Agent calls in total and per role."""

    total: Interval = ZERO
    roles: Mapping[str, Interval] = field(default_factory=dict)

    @staticmethod
    def call(role: str) -> Cost:
        return Cost(ONE, {role: ONE})

    def __add__(self, other: Cost) -> Cost:
        names = set(self.roles) | set(other.roles)
        return Cost(
            self.total + other.total,
            {r: self.roles.get(r, ZERO) + other.roles.get(r, ZERO) for r in sorted(names)},
        )

    def times(self, lo: int, hi: int | None) -> Cost:
        return Cost(self.total.times(lo, hi), {r: i.times(lo, hi) for r, i in self.roles.items()})

    def join(self, other: Cost) -> Cost:
        names = set(self.roles) | set(other.roles)
        return Cost(
            self.total.join(other.total),
            {r: self.roles.get(r, ZERO).join(other.roles.get(r, ZERO)) for r in sorted(names)},
        )


# -- diagnostics -----------------------------------------------------------------


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: str  # "error" or "warning"
    message: str
    pos: Pos

    def render(self, path: str) -> str:
        return f"{path}:{self.pos.line}:{self.pos.col}: {self.severity} {self.code}: {self.message}"

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "line": self.pos.line,
            "col": self.pos.col,
        }


@dataclass(frozen=True)
class HarnessInfo:
    """What the checker needs to know about the harness a role is bound to."""

    name: str
    capabilities: frozenset[str]
    reports: frozenset[str]


@dataclass(frozen=True)
class CheckReport:
    workflow: Workflow
    diagnostics: tuple[Diagnostic, ...]
    calls: Interval
    calls_by_role: Mapping[str, Interval]
    frozen: tuple[str, ...]
    result: Type | None

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        return tuple(d for d in self.diagnostics if d.severity == "error")

    @property
    def warnings(self) -> tuple[Diagnostic, ...]:
        return tuple(d for d in self.diagnostics if d.severity == "warning")

    @property
    def ok(self) -> bool:
        return not self.errors

    def credits(self, prices: Mapping[str, float]) -> tuple[float, float | None]:
        """Credit interval from per-call prices; every role that makes calls needs a price."""
        missing = [r for r, i in self.calls_by_role.items() if i.hi != 0 and r not in prices]
        if missing:
            raise ValueError(f"no price for role(s): {', '.join(missing)}")
        lo = sum(i.lo * prices.get(r, 0.0) for r, i in self.calls_by_role.items())
        hi: float | None = 0.0
        for role, interval in self.calls_by_role.items():
            price = prices.get(role, 0.0)
            if interval.hi is None and price > 0:
                hi = None
            elif hi is not None and interval.hi is not None:
                hi += interval.hi * price
        return lo, hi

    def to_json(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow.name,
            "ok": self.ok,
            "diagnostics": [d.to_json() for d in self.diagnostics],
            "calls": self.calls.to_json(),
            "calls_by_role": {r: i.to_json() for r, i in sorted(self.calls_by_role.items())},
            "frozen": list(self.frozen),
            "result": None if self.result is None else str(self.result),
        }


# -- checker -----------------------------------------------------------------------


@dataclass
class Sym:
    type: Type
    pos: Pos
    kind: str  # input, let, var, into, loop, branch
    size: Interval | None = None
    authors: frozenset[str] = frozenset()
    used: bool = False


@dataclass(frozen=True)
class Info:
    type: Type
    size: Interval | None = None
    authors: frozenset[str] = frozenset()


UNKNOWN = Info(ANY)


@dataclass(frozen=True)
class Context:
    shared_fork: bool = False  # inside a fork whose branches share one workspace
    isolated: bool = False  # inside an isolated fork
    top: bool = True


def _names(expr: Expr) -> Iterator[str]:
    if isinstance(expr, Name):
        yield expr.name
    elif isinstance(expr, Str):
        for ref in expr.template.refs:
            yield ref.path[0]
    elif isinstance(expr, (Field, Len, Not)):
        yield from _names(expr.base if isinstance(expr, Field) else expr.arg)
    elif isinstance(expr, BinOp):
        yield from _names(expr.left)
        yield from _names(expr.right)


class _Checker:
    def __init__(self, wf: Workflow, bindings: Mapping[str, HarnessInfo] | None,
                 workspace: str | None):
        self.wf = wf
        self.bindings = bindings
        self.workspace = workspace
        self.diags: list[Diagnostic] = []
        self.frames: list[dict[str, Sym]] = [{}]
        self.kinds: list[str] = ["top"]
        self.frozen: list[str] = []
        self.used_roles: set[str] = set()
        self.warned_inplace = False
        self.result: Type | None = None

    def report(self, code: str, message: str, pos: Pos) -> None:
        severity = "error" if code.startswith("E") else "warning"
        self.diags.append(Diagnostic(code, severity, message, pos))

    # -- scopes --------------------------------------------------------------

    def lookup(self, name: str) -> Sym | None:
        for frame in reversed(self.frames):
            if name in frame:
                return frame[name]
        return None

    def bind(self, name: str, sym: Sym) -> None:
        earlier = self.lookup(name)
        if earlier is not None:
            self.report("E202", f"'{name}' is already bound (at {earlier.pos})", sym.pos)
            return
        self.frames[-1][name] = sym

    def push(self, kind: str) -> None:
        self.frames.append({})
        self.kinds.append(kind)

    def pop(self, *, report_unused: bool = True) -> dict[str, Sym]:
        frame = self.frames.pop()
        self.kinds.pop()
        if report_unused:
            self.unused(frame)
        return frame

    def unused(self, frame: Mapping[str, Sym]) -> None:
        for name, sym in frame.items():
            if not sym.used and sym.kind != "branch" and not name.startswith("_"):
                what = "input" if sym.kind == "input" else "name"
                self.report("W301", f"{what} '{name}' is never used", sym.pos)

    # -- roles -----------------------------------------------------------------

    def role(self, name: str, need: str, pos: Pos) -> None:
        role = self.wf.role(name)
        if role is None:
            self.report("E203", f"role '{name}' is not declared (add: role {name} can {need})",
                        pos)
            return
        self.used_roles.add(name)
        ok = role.can_edit if need == "edit" else role.can_answer
        if not ok:
            self.report("E204", f"role '{name}' cannot {need} (it can {', '.join(role.caps)})",
                        pos)

    # -- expressions -------------------------------------------------------------

    def path(self, names: Iterable[str], pos: Pos) -> Info:
        parts = list(names)
        sym = self.lookup(parts[0])
        if sym is None:
            self.report("E201", f"'{parts[0]}' is not bound here", pos)
            return UNKNOWN
        sym.used = True
        info = Info(sym.type, sym.size, sym.authors)
        for name in parts[1:]:
            info = self.field(info, name, pos)
        return info

    def field(self, base: Info, name: str, pos: Pos) -> Info:
        kind = base.type.kind
        if kind == "any":
            return UNKNOWN
        if kind == "list":
            self.report("E206", f"a {base.type} has no field '{name}'; "
                        "pick one with choose or visit each with for each", pos)
            return UNKNOWN
        fields = FIELDS.get(kind, {})
        if name not in fields:
            known = ", ".join(fields) or "none"
            self.report("E206", f"{kind} has no field '{name}' (fields: {known})", pos)
            return UNKNOWN
        typ = fields[name]
        return Info(typ, Interval(0, None) if typ.kind == "list" else None, base.authors)

    def template(self, t: Template) -> None:
        for ref in t.refs:
            self.path(ref.path, ref.pos)

    def literal_command(self, t: Template, what: str) -> None:
        if not t.literal:
            refs = ", ".join("{" + ".".join(r.path) + "}" for r in t.refs)
            self.report("E207", f"{what} must be literal; it interpolates {refs}", t.pos)

    def expr(self, e: Expr) -> Info:
        if isinstance(e, Num):
            return Info(NUMBER)
        if isinstance(e, Str):
            self.template(e.template)
            return Info(TEXT)
        if isinstance(e, Bool):
            return Info(BOOL)
        if isinstance(e, Name):
            return self.path((e.name,), e.pos)
        if isinstance(e, Field):
            return self.field(self.expr(e.base), e.name, e.pos)
        if isinstance(e, Len):
            arg = self.expr(e.arg)
            if arg.type.kind not in ("list", "text", "any"):
                self.report("E205", f"len() needs a list or text, not {arg.type}", e.pos)
            return Info(NUMBER)
        if isinstance(e, Not):
            self.expect_bool(self.expr(e.arg), e.pos, "'not'")
            return Info(BOOL)
        left, right = self.expr(e.left), self.expr(e.right)
        if e.op in ("and", "or"):
            self.expect_bool(left, e.left.pos, f"'{e.op}'")
            self.expect_bool(right, e.right.pos, f"'{e.op}'")
        elif e.op in ("==", "!="):
            for side in (left, right):
                if side.type.kind not in ("text", "number", "bool", "any"):
                    self.report("E205", f"'{e.op}' compares text, numbers or bools, "
                                f"not {side.type}", e.pos)
            if not compatible(left.type, right.type):
                self.report("E205", f"cannot compare {left.type} with {right.type}", e.pos)
        else:
            for side in (left, right):
                if not compatible(side.type, NUMBER):
                    self.report("E205", f"'{e.op}' needs numbers, not {side.type}", e.pos)
        return Info(BOOL)

    def expect_bool(self, info: Info, pos: Pos, what: str) -> None:
        if not compatible(info.type, BOOL):
            self.report("E205", f"{what} needs a bool, not {info.type}", pos)

    # -- values ------------------------------------------------------------------

    def value(self, v: Value, ctx: Context) -> tuple[Info, Cost]:
        if isinstance(v, Ask):
            self.role(v.role, "answer", v.pos)
            self.template(v.prompt)
            typ = ASK_TYPES[v.kind]
            size = Interval(0, None) if typ.kind == "list" else None
            return Info(typ, size, frozenset({v.role})), Cost.call(v.role)
        if isinstance(v, Edit):
            self.role(v.role, "edit", v.pos)
            self.template(v.prompt)
            self.workspace_effect("edit", v.pos, ctx)
            return Info(CHANGE, None, frozenset({v.role})), Cost.call(v.role)
        if isinstance(v, Exec):
            self.literal_command(v.command, "an exec command")
            self.workspace_effect("exec", v.pos, ctx)
            return Info(RESULT), Cost()
        if isinstance(v, Test):
            return self.test(v, ctx), Cost()
        if isinstance(v, Choose):
            return self.choose(v)
        return self.expr(v), Cost()

    def workspace_effect(self, what: str, pos: Pos, ctx: Context) -> None:
        if ctx.shared_fork:
            self.report("E208", f"'{what}' inside a fork whose branches share a workspace; "
                        "write 'fork N isolated'", pos)
        if what == "exec":
            return
        if self.workspace == "guarded":
            self.report("E214", f"the guarded workspace is read-only, so '{what}' cannot run "
                        "(grading restores frozen files)" if what == "test" else
                        f"the guarded workspace is read-only, so '{what}' cannot run", pos)
        elif what == "edit" and self.workspace == "inplace" and not ctx.isolated \
                and not self.warned_inplace:
            self.warned_inplace = True
            self.report("W307", "edits in the in-place workspace change your files directly",
                        pos)

    def test(self, v: Test, ctx: Context) -> Info:
        subject = self.expr(v.subject)
        self.literal_command(v.command, "a test command")
        for pattern in v.frozen:
            self.literal_command(pattern, "a frozen pattern")
            if pattern.literal and pattern.text() not in self.frozen:
                self.frozen.append(pattern.text())
        if not v.frozen:
            self.report("W304", "test without 'frozen': an agent could edit the tests "
                        "and this would not be detected", v.pos)
        self.workspace_effect("test", v.pos, ctx)
        typ = subject.type
        if typ.kind == "any":
            return Info(ANY, None, subject.authors)
        if typ == CHANGE:
            return Info(GRADED, None, subject.authors)
        if typ.kind == "list" and typ.item is not None and typ.item.kind in ("change", "any"):
            return Info(list_of(GRADED), subject.size, subject.authors)
        self.report("E205", f"test needs a change or a list of changes, not {typ}", v.subject.pos)
        return UNKNOWN

    def choose(self, v: Choose) -> tuple[Info, Cost]:
        subject = self.expr(v.subject)
        typ = subject.type
        item = ANY
        if typ.kind == "list" and typ.item is not None:
            item = typ.item
        elif typ.kind != "any":
            self.report("E205", f"choose needs a list of candidates, not {typ}", v.subject.pos)
        size = subject.size or Interval(0, None)
        cost = Cost()
        evidence = False
        judges = [p for p in v.policies if p.name == "pairwise"]
        if len(judges) > 1:
            self.report("E221", "a choose may use pairwise once; pick one judge role",
                        judges[1].pos)
        for index, policy in enumerate(v.policies):
            if policy.name in ("tests", "behavior"):
                if item.kind not in ("graded", "any"):
                    self.report("E205", f"'{policy.name}' ranks graded candidates; "
                                f"run test on the {typ} first", policy.pos)
                evidence = True
            elif policy.name == "exact":
                if item.kind not in ("graded", "change", "any"):
                    self.report("E205", f"'exact' compares changes, not {item}", policy.pos)
            else:
                assert policy.role is not None
                self.role(policy.role, "answer", policy.pos)
                if policy.role in subject.authors:
                    self.report("W302", f"'{policy.role}' judges candidates it wrote itself",
                                policy.pos)
                if not evidence:
                    self.report("W303", "pairwise judging without 'tests' first relies on the "
                                "judge alone", policy.pos)
                if size.hi is None:
                    self.report("E209", "pairwise needs a list with a known size bound "
                                "(from fork N or for each ... at most N)", policy.pos)
                lo = size.lo * (size.lo - 1) if index == 0 else 0
                hi = None if size.hi is None else size.hi * (size.hi - 1)
                calls = Interval(lo, hi)
                cost = cost + Cost(calls, {policy.role: calls})
        return Info(item, None, subject.authors), cost

    # -- statements --------------------------------------------------------------

    def block(self, body: tuple[Stmt, ...], ctx: Context) -> Cost:
        total = Cost()
        for index, stmt in enumerate(body):
            if isinstance(stmt, Give) and (not ctx.top or index != len(body) - 1):
                where = "at the top level" if not ctx.top else "as the last statement"
                self.report("E216", f"'give' is only allowed once, {where}", stmt.pos)
            total = total + self.statement(stmt, ctx)
        return total

    def statement(self, s: Stmt, ctx: Context) -> Cost:
        if isinstance(s, Let):
            info, cost = self.value(s.value, ctx)
            self.bind(s.name, Sym(info.type, s.pos, "let", info.size, info.authors))
            return cost
        if isinstance(s, Var):
            info, cost = self.value(s.value, ctx)
            if info.type.kind == "list":
                self.report("E205", f"var cannot hold a {info.type}; use let", s.pos)
            self.bind(s.name, Sym(info.type, s.pos, "var", None, info.authors))
            return cost
        if isinstance(s, Set):
            return self.set_(s, ctx)
        if isinstance(s, Do):
            return self.value(s.action, ctx)[1]
        if isinstance(s, Fork):
            return self.fork(s, ctx)
        if isinstance(s, Each):
            return self.each(s, ctx)
        if isinstance(s, Repeat):
            return self.repeat(s, ctx)
        if isinstance(s, If):
            return self.if_(s, ctx)
        if isinstance(s, Require):
            self.expect_bool(self.expr(s.cond), s.cond.pos, "require")
            self.template(s.message)
            return Cost()
        if isinstance(s, Note):
            self.template(s.message)
            return Cost()
        self.result = self.expr(s.value).type
        return Cost()

    def set_(self, s: Set, ctx: Context) -> Cost:
        info, cost = self.value(s.value, ctx)
        for depth in range(len(self.frames) - 1, -1, -1):
            sym = self.frames[depth].get(s.name)
            if sym is None:
                continue
            if sym.kind != "var":
                self.report("E219", f"'{s.name}' was bound with {sym.kind}, not var, so it "
                            "cannot be set", s.pos)
            elif any(k in ("fork", "each") for k in self.kinds[depth + 1 :]):
                self.report("E220", f"cannot set '{s.name}' inside a fork or for-each branch; "
                            "keep a value instead", s.pos)
            elif not compatible(sym.type, info.type):
                self.report("E205", f"'{s.name}' holds {sym.type}, not {info.type}", s.pos)
            else:
                sym.authors = sym.authors | info.authors
            return cost
        self.report("E201", f"'{s.name}' is not bound here (declare it with var first)", s.pos)
        return cost

    def bound(self, n: int, what: str, pos: Pos) -> None:
        if n < 1:
            self.report("E218", f"{what} must be at least 1", pos)

    def fork(self, s: Fork, ctx: Context) -> Cost:
        self.bound(s.count, "a fork count", s.pos)
        inner = Context(shared_fork=not s.isolated, isolated=ctx.isolated or s.isolated, top=False)
        self.push("fork")
        # Each fork has its own `branch`; an inner fork's shadows an outer one's.
        self.frames[-1]["branch"] = Sym(NUMBER, s.pos, "branch")
        body = self.block(s.body, inner)
        keep = self.expr(s.keep)
        self.pop()
        n = max(s.count, 0)
        self.bind(s.into, Sym(list_of(keep.type), s.pos, "into", Interval(n, n), keep.authors))
        return body.times(n, n)

    def each(self, s: Each, ctx: Context) -> Cost:
        self.bound(s.limit, "a for-each limit", s.pos)
        source = self.expr(s.source)
        item = ANY
        if source.type.kind == "list" and source.type.item is not None:
            item = source.type.item
        elif source.type.kind != "any":
            self.report("E205", f"for each needs a list, not {source.type}", s.source.pos)
        size = source.size or Interval(0, None)
        n = max(s.limit, 0)
        lo, hi = min(size.lo, n), n if size.hi is None else min(size.hi, n)
        self.push("each")
        self.bind(s.var, Sym(item, s.pos, "loop", None, source.authors))
        body = self.block(s.body, Context(ctx.shared_fork, ctx.isolated, top=False))
        keep = self.expr(s.keep)
        self.pop()
        self.bind(s.into, Sym(list_of(keep.type), s.pos, "into", Interval(lo, hi), keep.authors))
        return body.times(lo, hi)

    def repeat(self, s: Repeat, ctx: Context) -> Cost:
        self.bound(s.limit, "a repeat limit", s.pos)
        self.push("repeat")
        body = self.block(s.body, Context(ctx.shared_fork, ctx.isolated, top=False))
        self.expect_bool(self.expr(s.until), s.until.pos, "until")
        frame = self.pop(report_unused=False)
        changed = set(frame) | {st.name for st in walk(s.body) if isinstance(st, Set)}
        if not any(name in changed for name in _names(s.until)):
            self.report("W306", "the until condition does not use anything bound in the loop, "
                        "so it cannot change between iterations", s.until.pos)
        for name, sym in frame.items():
            self.bind(name, sym)
        n = max(s.limit, 0)
        return Cost(Interval(body.total.lo, _mul(body.total.hi, n)),
                    {r: Interval(i.lo, _mul(i.hi, n)) for r, i in body.roles.items()})

    def if_(self, s: If, ctx: Context) -> Cost:
        self.expect_bool(self.expr(s.cond), s.cond.pos, "if")
        inner = Context(ctx.shared_fork, ctx.isolated, top=False)
        self.push("if")
        then = self.block(s.then, inner)
        then_frame = self.pop(report_unused=False)
        else_frame: dict[str, Sym] = {}
        orelse = Cost()
        if s.orelse is not None:
            self.push("if")
            orelse = self.block(s.orelse, inner)
            else_frame = self.pop(report_unused=False)
        shared = [n for n in then_frame if n in else_frame
                  and then_frame[n].type == else_frame[n].type]
        self.unused({n: v for n, v in then_frame.items() if n not in shared})
        self.unused({n: v for n, v in else_frame.items() if n not in shared})
        for name in shared:
            a, b = then_frame[name], else_frame[name]
            size = None if a.size is None or b.size is None else a.size.join(b.size)
            self.bind(name, Sym(a.type, a.pos, a.kind if a.kind == b.kind else "let", size, a.authors | b.authors,
                                a.used or b.used))
        return then.join(orelse)

    # -- workflow ------------------------------------------------------------------

    def run(self) -> CheckReport:
        wf = self.wf
        for param in wf.params:
            if param.default is not None:
                default = self.expr(param.default).type
                if default != SCALARS[param.type]:
                    self.report("E205", f"default for '{param.name}' is {default}, "
                                f"not {param.type}", param.pos)
            if param.name in self.frames[0]:
                self.report("E217", f"input '{param.name}' is declared twice", param.pos)
                continue
            self.frames[0][param.name] = Sym(SCALARS[param.type], param.pos, "input")
        seen_roles: set[str] = set()
        for role in wf.roles:
            if role.name in seen_roles:
                self.report("E217", f"role '{role.name}' is declared twice", role.pos)
            seen_roles.add(role.name)
        seen_units: set[str] = set()
        for limit in wf.budget:
            if limit.unit in seen_units:
                self.report("E217", f"budget unit '{limit.unit}' appears twice", limit.pos)
            seen_units.add(limit.unit)
            if limit.unit == "calls" and not float(limit.amount).is_integer():
                self.report("E205", "a calls ceiling must be a whole number", limit.pos)

        cost = self.block(wf.body, Context())
        self.unused(self.frames[0])
        for role in wf.roles:
            if role.name not in self.used_roles:
                self.report("W301", f"role '{role.name}' is never used", role.pos)

        calls = wf.ceiling("calls")
        if calls is not None:
            if cost.total.lo > calls:
                self.report("E210", f"at least {cost.total.lo} agent calls are needed but the "
                            f"budget allows {calls:g}", wf.pos)
            elif cost.total.hi is None or cost.total.hi > calls:
                self.report("W305", f"up to {cost.total} agent calls may be needed; the run "
                            f"stops at {calls:g}", wf.pos)
        if not wf.budget and cost.total.hi != 0:
            self.report("W308", "this workflow calls agents but declares no budget", wf.pos)
        if self.bindings is not None:
            self.check_bindings()
        return CheckReport(wf, tuple(sorted(self.diags, key=lambda d: (d.pos.line, d.pos.col))),
                           cost.total, dict(cost.roles), tuple(self.frozen), self.result)

    def check_bindings(self) -> None:
        assert self.bindings is not None
        wf = self.wf
        units = [lim.unit for lim in wf.budget if lim.unit != "calls"]
        for name in self.bindings:
            if wf.role(name) is None:
                self.report("E203", f"binding for '{name}', which the workflow does not declare",
                            wf.pos)
        for role in wf.roles:
            if role.name not in self.used_roles:
                continue
            harness = self.bindings.get(role.name)
            if harness is None:
                self.report("E213", f"role '{role.name}' has no binding "
                            f"(add --bind {role.name}=HARNESS[:MODEL])", role.pos)
                continue
            needs = set(role.caps) | ({"answer"} if "edit" in role.caps else set())
            missing = sorted(needs - harness.capabilities)
            if missing:
                self.report("E212", f"'{role.name}' is bound to {harness.name}, which cannot "
                            f"{', '.join(missing)}", role.pos)
            for unit in units:
                if unit not in harness.reports:
                    self.report("E211", f"the budget caps {unit}, but {harness.name} (bound to "
                                f"'{role.name}') cannot measure {unit}", role.pos)


def check(workflow: Workflow, bindings: Mapping[str, HarnessInfo] | None = None, *,
          workspace: str | None = None) -> CheckReport:
    """Type-check `workflow`, bound its agent calls, and (optionally) check bindings."""
    return _Checker(workflow, bindings, workspace).run()
