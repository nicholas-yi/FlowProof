"""Execute a checked workflow against bound harnesses, with sealed evidence."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..accounting import AccountingUnknown, Ledger, LimitReached, Usage
from ..harness.base import AgentRequest, AgentResponse, Harness
from ..lang import CheckReport, HarnessInfo, check, format_workflow
from ..lang.tree import (
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
    walk,
)
from ..outcomes import Outcome
from .values import (
    UNKNOWN,
    ChangeValue,
    GradedValue,
    ResultValue,
    Unavailable,
    field_of,
    render,
    tail,
    to_json,
)

REPLY_FORMATS = {
    "number": "Reply with a single number and nothing else.",
    "bool": "Reply with exactly one word: yes or no.",
    "list": "Reply with a JSON array of strings and nothing else.",
}
JUDGE_PROMPT = """You are comparing two candidate results for the same task.
{task}Prefer the candidate that is more likely to be correct and complete. Ignore style,
length and the order in which the candidates are shown.

Candidate A:
<<<
{first}
>>>

Candidate B:
<<<
{second}
>>>

Answer with exactly one word: A, B, or TIE."""


class _Stop(Exception):
    """Ends the whole run (a budget stop, or a failure outside any branch)."""

    def __init__(self, outcome: Outcome):
        super().__init__(outcome.reason)
        self.outcome = outcome


class _Fail(Exception):
    """Ends the current branch or iteration; at the top level it ends the run."""

    def __init__(self, outcome: Outcome):
        super().__init__(outcome.reason)
        self.outcome = outcome


@dataclass
class RunOptions:
    source: Path = Path(".")
    workspace: str = "worktree"
    lease_root: Path | None = None
    evidence: Path | None = None
    timeout_s: float = 900.0
    command_timeout_s: float = 900.0
    ceilings: Mapping[str, float] = field(default_factory=dict)
    max_calls: int | None = None
    parallel: int = 1
    keep_workspaces: bool = False
    label: str = ""


@dataclass(frozen=True)
class RunResult:
    outcome: Outcome
    value: Any
    usage: Usage
    calls_by_role: Mapping[str, int]
    report: CheckReport
    bound_held: bool
    run_id: str | None = None
    evidence_root: Path | None = None
    result_patch: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "workflow": self.report.workflow.name,
            "outcome": self.outcome.to_json(),
            "value": self.value,
            "usage": self.usage.to_json(),
            "calls_by_role": dict(sorted(self.calls_by_role.items())),
            "static_bound": {
                "calls": self.report.calls.to_json(),
                "by_role": {r: i.to_json() for r, i in sorted(self.report.calls_by_role.items())},
            },
            "bound_held": self.bound_held,
            "run_id": self.run_id,
        }


def harness_info(harness: Harness) -> HarnessInfo:
    return HarnessInfo(harness.name, frozenset(harness.capabilities), frozenset(harness.reports))


def coerce_inputs(workflow: Workflow, given: Mapping[str, str]) -> dict[str, Any]:
    """Convert command-line inputs to the declared types, applying defaults."""
    unknown = sorted(set(given) - {p.name for p in workflow.params})
    if unknown:
        raise ValueError(f"unknown input(s): {', '.join(unknown)}")
    values: dict[str, Any] = {}
    for param in workflow.params:
        if param.name in given:
            raw = given[param.name]
            if param.type == "number":
                try:
                    values[param.name] = float(raw)
                except ValueError:
                    raise ValueError(f"input {param.name} must be a number, not {raw!r}") from None
            elif param.type == "bool":
                lowered = raw.strip().lower()
                if lowered not in ("true", "false", "yes", "no"):
                    raise ValueError(f"input {param.name} must be true or false, not {raw!r}")
                values[param.name] = lowered in ("true", "yes")
            else:
                values[param.name] = raw
        elif param.default is not None:
            default = param.default
            values[param.name] = (default.template.text() if isinstance(default, Str)
                                  else default.value)
        else:
            raise ValueError(f"missing input: {param.name} ({param.type})")
    return values


def parse_answer(kind: str, text: str) -> Any:
    """Strictly parse an `ask ... as kind` reply; raises ValueError when it does not fit."""
    body = text.strip()
    fence = re.fullmatch(r"```[\w-]*\n?(.*?)\n?```", body, flags=re.S)
    if fence:
        body = fence.group(1).strip()
    if kind == "text":
        return text
    if kind == "number":
        candidate = body.rstrip(".").strip()
        if re.fullmatch(r"[-+]?\d+(\.\d+)?", candidate):
            return float(candidate)
        raise ValueError("expected a single number")
    if kind == "bool":
        word = body.strip(" .!").lower()
        if word in ("yes", "true"):
            return True
        if word in ("no", "false"):
            return False
        raise ValueError("expected yes or no")
    start, end = body.find("["), body.rfind("]")
    if start >= 0 and end > start:
        try:
            items = json.loads(body[start : end + 1])
        except ValueError:
            items = None
        if isinstance(items, list) and all(isinstance(i, str) for i in items):
            return list(items)
    bullets = [line.strip()[2:].strip() for line in body.splitlines()
               if line.strip().startswith(("- ", "* "))]
    if bullets:
        return bullets
    raise ValueError("expected a JSON array of strings or a '- ' list")


def parse_verdict(text: str) -> str | None:
    found = {m.upper() for m in re.findall(r"\b(A|B|TIE)\b", text.upper())}
    return found.pop() if len(found) == 1 else None


@dataclass
class _Context:
    frames: list[dict[str, Any]]
    lease: Any | None
    branch: bool = False


class Executor:
    def __init__(self, workflow: Workflow, harnesses: Mapping[str, Harness], *,
                 models: Mapping[str, str | None] | None = None,
                 inputs: Mapping[str, Any] | None = None, options: RunOptions | None = None,
                 source_text: str | None = None):
        self.wf = workflow
        self.harnesses = dict(harnesses)
        self.models = dict(models or {})
        self.options = options or RunOptions()
        self.inputs = dict(inputs or {})
        self.source_text = source_text
        self.report = check(workflow, {r: harness_info(h) for r, h in self.harnesses.items()},
                            workspace=self.options.workspace)
        ceilings = {lim.unit: float(lim.amount) for lim in workflow.budget if lim.unit != "calls"}
        for unit, limit in self.options.ceilings.items():
            ceilings[unit] = min(limit, ceilings.get(unit, limit))
        calls = workflow.ceiling("calls")
        caps = [int(c) for c in (calls, self.options.max_calls) if c is not None]
        self.ledger = Ledger(ceilings, min(caps) if caps else None)
        self.lock = threading.RLock()
        self.calls_by_role: dict[str, int] = {}
        self.seq = 0
        self.leases: list[Any] = []
        self.frozen: dict[tuple[str, ...], Any] = {}
        self.record: Any | None = None
        self.counter = 0

    # -- evidence ------------------------------------------------------------

    def event(self, kind: str, **fields: Any) -> None:
        if self.record is None:
            return
        with self.lock:
            self.record.event(kind, **fields)

    def write_json(self, rel: str, value: Any) -> None:
        if self.record is None:
            return
        with self.lock:
            self.record.write_json(rel, value)

    def write_text(self, rel: str, text: str) -> None:
        if self.record is None:
            return
        with self.lock:
            self.record.write_text(rel, text)

    # -- workspaces ------------------------------------------------------------

    def lease(self, source: Path, label: str, backend_name: str) -> Any:
        from ..workspace import WorkspaceError, backend_for

        backend = backend_for(backend_name, lease_root=self.options.lease_root)
        try:
            lease = backend.lease(source, label=label)
        except WorkspaceError:
            if backend_name != "worktree":
                raise
            lease = backend_for("copy", lease_root=self.options.lease_root).lease(
                source, label=label)
        with self.lock:
            self.leases.append(lease)
        self.event("lease", label=label, tier=lease.tier.name.lower(), base=lease.base,
                   id=lease.id)
        return lease

    def child_lease(self, parent: Any | None, label: str) -> Any:
        source = Path(parent.root) if parent is not None else self.options.source
        return self.lease(source, label, "worktree")

    # -- agent calls -------------------------------------------------------------

    def call(self, role: str, prompt: str, mode: str, ctx: _Context, label: str) -> AgentResponse:
        harness = self.harnesses[role]
        role_decl = self.wf.role(role)
        with self.lock:
            try:
                self.ledger.admit()
            except LimitReached as exc:
                raise _Stop(Outcome.limited("budget", detail=str(exc))) from None
            except AccountingUnknown as exc:
                raise _Stop(Outcome.invalid("accounting-unknown", detail=str(exc))) from None
            self.seq += 1
            seq = self.seq
            remaining = self.ledger.remaining("credits")
        workspace = Path(ctx.lease.root) if ctx.lease is not None else None
        request = AgentRequest(role=role, prompt=prompt, mode=mode, workspace=workspace,
                               model=self.models.get(role), max_credits=remaining,
                               timeout_s=self.options.timeout_s, label=label,
                               shell=bool(role_decl and "shell" in role_decl.caps))
        try:
            response = harness.invoke(request)
        except Exception as exc:  # a harness bug must not look like a cheap success
            response = AgentResponse("", Usage.unknown(*harness.reports),
                                     Outcome.invalid("harness-error", error=repr(exc)))
        with self.lock:
            self.ledger.settle(response.usage)
            self.calls_by_role[role] = self.calls_by_role.get(role, 0) + 1
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        self.write_json(f"calls/{seq:04d}.json", {
            "seq": seq, "role": role, "label": label, "mode": mode,
            "harness": harness.name, "model": self.models.get(role),
            "prompt": prompt, "prompt_sha256": digest, "response": response.text,
            "usage": response.usage.to_json(), "outcome": response.outcome.to_json(),
            "raw": {k: v for k, v in response.raw.items() if k != "usage_file"},
            "provider_usage": response.raw.get("usage_file"),
        })
        self.event("call", call=seq, role=role, label=label, mode=mode,
                   prompt_sha256=digest, usage=response.usage.to_json(),
                   outcome=response.outcome.to_json(), spent=self.ledger.spent.to_json())
        return response

    # -- expressions ---------------------------------------------------------------

    def lookup(self, ctx: _Context, name: str) -> Any:
        for frame in reversed(ctx.frames):
            if name in frame:
                return frame[name]
        raise _Fail(Outcome.invalid("unbound-name", name=name))

    def template(self, t: Template, ctx: _Context) -> str:
        out = []
        for part in t.parts:
            if isinstance(part, Ref):
                value = self.lookup(ctx, part.path[0])
                for name in part.path[1:]:
                    value = field_of(value, name)
                out.append(render(value))
            else:
                out.append(part)
        return "".join(out)

    def expr(self, e: Expr, ctx: _Context) -> Any:
        if isinstance(e, Num):
            return float(e.value)
        if isinstance(e, Str):
            return self.template(e.template, ctx)
        if isinstance(e, Bool):
            return e.value
        if isinstance(e, Name):
            return self.lookup(ctx, e.name)
        if isinstance(e, Field):
            return field_of(self.expr(e.base, ctx), e.name)
        if isinstance(e, Len):
            arg = self.expr(e.arg, ctx)
            return float(len(arg)) if isinstance(arg, (str, list)) else UNKNOWN
        if isinstance(e, Not):
            arg = self.expr(e.arg, ctx)
            return (not arg) if isinstance(arg, bool) else UNKNOWN
        return self.binop(e, ctx)

    def binop(self, e: BinOp, ctx: _Context) -> Any:
        left = self.expr(e.left, ctx)
        if e.op in ("and", "or"):
            # Kleene logic: unknown only matters when it could change the answer.
            decisive = e.op == "or"
            if left is decisive:
                return decisive
            right = self.expr(e.right, ctx)
            if right is decisive:
                return decisive
            if isinstance(left, bool) and isinstance(right, bool):
                return not decisive
            return UNKNOWN
        right = self.expr(e.right, ctx)
        if not all(isinstance(v, (bool, int, float, str)) for v in (left, right)):
            return UNKNOWN
        ops: dict[str, Callable[[Any, Any], bool]] = {
            "==": lambda a, b: a == b, "!=": lambda a, b: a != b, "<": lambda a, b: a < b,
            "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
        }
        return ops[e.op](left, right)

    def condition(self, e: Expr, ctx: _Context, what: str) -> bool:
        value = self.expr(e, ctx)
        if not isinstance(value, bool):
            raise _Fail(Outcome.invalid("unknown-condition", where=what))
        return value

    # -- values ------------------------------------------------------------------

    def label(self, stmt: Any, extra: str = "") -> str:
        line = getattr(stmt, "pos", None)
        where = f"line {line.line}" if line is not None and line.line else "stage"
        return f"{where}{extra}"

    def value(self, v: Value, ctx: _Context, name: str) -> Any:
        if isinstance(v, Ask):
            prompt = self.template(v.prompt, ctx)
            if v.kind in REPLY_FORMATS:
                prompt = f"{prompt}\n\n{REPLY_FORMATS[v.kind]}"
            response = self.call(v.role, prompt, "answer", ctx, self.label(v, f" ask {v.role}"))
            if not response.outcome.is_ok:
                raise _Fail(Outcome.invalid(f"agent-{response.outcome.reason}", role=v.role))
            try:
                return parse_answer(v.kind, response.text)
            except ValueError as exc:
                raise _Fail(Outcome.invalid("unparseable-answer", expected=v.kind,
                                            error=str(exc))) from None
        if isinstance(v, Edit):
            if ctx.lease is None:
                raise _Fail(Outcome.invalid("no-workspace"))
            prompt = self.template(v.prompt, ctx)
            response = self.call(v.role, prompt, "edit", ctx, self.label(v, f" edit {v.role}"))
            if not response.outcome.is_ok:
                raise _Fail(Outcome.invalid(f"agent-{response.outcome.reason}", role=v.role))
            change = ctx.lease.diff()
            self.event("change", stage=self.label(v), role=v.role, files=list(change.files),
                       lease=ctx.lease.id)
            return ChangeValue(change, ctx.lease, v.role, prompt)
        if isinstance(v, Exec):
            return self.exec(v, ctx)
        if isinstance(v, Test):
            return self.test(v, ctx)
        if isinstance(v, Choose):
            return self.choose(v, ctx, name)
        return self.expr(v, ctx)

    def exec(self, v: Exec, ctx: _Context) -> ResultValue:
        if ctx.lease is None:
            raise _Fail(Outcome.invalid("no-workspace"))
        command = v.command.text()
        result = ctx.lease.run(command, timeout_s=self.options.command_timeout_s)
        ok = UNKNOWN if result.exit_code is None else result.exit_code == 0
        self.event("exec", stage=self.label(v), command=command, exit_code=result.exit_code,
                   timed_out=result.timed_out, duration_s=round(result.duration_s, 3))
        exit_code = UNKNOWN if result.exit_code is None else result.exit_code
        return ResultValue(ok, exit_code, tail(result.stdout + result.stderr))

    def test(self, v: Test, ctx: _Context) -> Any:
        from ..guards import grade

        subject = self.expr(v.subject, ctx)
        command = v.command.text()
        frozen = self.frozen[tuple(p.text() for p in v.frozen)]

        def one(item: Any) -> GradedValue:
            if isinstance(item, Unavailable):
                return GradedValue(item, None, item.outcome)
            if not isinstance(item, ChangeValue):
                return GradedValue(Unavailable(Outcome.invalid("not-a-change")), None,
                                   Outcome.invalid("not-a-change"))
            if item.lease.released or item.lease.diff().patch != item.change.patch:
                return GradedValue(item, None, Outcome.invalid("stale-change"))
            g = grade(item.lease, command, frozen, timeout_s=self.options.command_timeout_s)
            self.event("grade", stage=self.label(v), lease=item.lease.id,
                       claimed_passed=g.claimed_passed, true_passed=g.true_passed,
                       false_pass=g.false_pass, tampered=g.tamper.tampered,
                       findings=[{"kind": f.kind, "path": f.path} for f in g.tamper.findings],
                       tests_passed=g.tests_passed, tests_failed=g.tests_failed)
            return GradedValue(item, g)

        if isinstance(subject, list):
            return self.map(one, subject)
        return one(subject)

    def map(self, fn: Callable[[Any], Any], items: Sequence[Any]) -> list[Any]:
        if self.options.parallel > 1 and len(items) > 1:
            with ThreadPoolExecutor(max_workers=self.options.parallel) as pool:
                return list(pool.map(fn, items))
        return [fn(item) for item in items]

    def choose(self, v: Choose, ctx: _Context, name: str) -> Any:
        from ..select import Candidate, Judge, Preference, choose

        subject = self.expr(v.subject, ctx)
        if not isinstance(subject, list):
            raise _Fail(Outcome.invalid("not-a-list"))
        values: dict[str, Any] = {}
        cands: list[Candidate] = []
        for index, item in enumerate(subject):
            cid = f"{index + 1:03d}"
            if isinstance(item, Unavailable) or (
                isinstance(item, GradedValue) and isinstance(item.change, Unavailable)
            ):
                continue  # a branch that ended early has nothing to choose
            change = grade_obj = None
            outcome = Outcome.ok()
            if isinstance(item, GradedValue):
                change = item.change.change if isinstance(item.change, ChangeValue) else None
                grade_obj, outcome = item.grade, item.outcome
            elif isinstance(item, ChangeValue):
                change = item.change
            values[cid] = item
            cands.append(Candidate(cid, change, grade_obj, outcome))
        if not cands:
            raise _Fail(Outcome.invalid("no-valid-candidates"))
        judge_role = next((p.role for p in v.policies if p.name == "pairwise"), None)
        judge: Judge | None = None
        if judge_role is not None:
            task = ""
            first_value = values[cands[0].id]
            origin = first_value.change if isinstance(first_value, GradedValue) else first_value
            if isinstance(origin, ChangeValue):
                task = f"The task was:\n<<<\n{origin.prompt}\n>>>\n\n"

            def ask_judge(a: Candidate, b: Candidate) -> Preference:
                prompt = JUDGE_PROMPT.format(task=task, first=render(values[a.id]),
                                             second=render(values[b.id]))
                response = self.call(judge_role, prompt, "answer", ctx,
                                     self.label(v, f" judge {a.id} vs {b.id}"))
                if not response.outcome.is_ok:
                    return Preference.INVALID
                verdict = parse_verdict(response.text)
                return {"A": Preference.FIRST, "B": Preference.SECOND,
                        "TIE": Preference.TIE}.get(verdict or "", Preference.INVALID)

            judge = ask_judge

        chain = [p.name for p in v.policies]
        selection = choose(cands, chain, judge=judge, fallback=v.fallback)
        self.write_json(f"selections/{name}-{self.next_id()}.json", {
            "stage": self.label(v), "chosen": selection.chosen, "basis": list(selection.basis),
            "fallback": selection.fallback, "trace": selection.trace,
        })
        self.event("choose", stage=self.label(v), chosen=selection.chosen,
                   basis=list(selection.basis), fallback=selection.fallback,
                   candidates=len(cands))
        if selection.chosen is None:
            raise _Fail(Outcome.failed("abstained", candidates=len(cands)))
        return values[selection.chosen]

    def next_id(self) -> int:
        with self.lock:
            self.counter += 1
            return self.counter

    # -- statements --------------------------------------------------------------

    def block(self, body: Sequence[Stmt], ctx: _Context) -> Any:
        result: Any = None
        for stmt in body:
            result = self.statement(stmt, ctx)
        return result

    def statement(self, s: Stmt, ctx: _Context) -> Any:
        if isinstance(s, (Let, Var)):
            ctx.frames[-1][s.name] = self.value(s.value, ctx, s.name)
        elif isinstance(s, Set):
            value = self.value(s.value, ctx, s.name)
            for frame in reversed(ctx.frames):
                if s.name in frame:
                    frame[s.name] = value
                    break
        elif isinstance(s, Do):
            self.value(s.action, ctx, "_")
        elif isinstance(s, Fork):
            ctx.frames[-1][s.into] = self.fork(s, ctx)
        elif isinstance(s, Each):
            ctx.frames[-1][s.into] = self.each(s, ctx)
        elif isinstance(s, Repeat):
            frame: dict[str, Any] = {}
            inner = _Context([*ctx.frames, frame], ctx.lease, ctx.branch)
            for iteration in range(1, s.limit + 1):
                self.block(s.body, inner)
                done = self.condition(s.until, inner, "until")
                self.event("repeat", stage=self.label(s), iteration=iteration, done=done)
                if done:
                    break
            ctx.frames[-1].update(frame)
        elif isinstance(s, If):
            taken = self.condition(s.cond, ctx, "if")
            frame = {}
            block = s.then if taken else (s.orelse or ())
            self.block(block, _Context([*ctx.frames, frame], ctx.lease, ctx.branch))
            # The checker only lets later statements read names bound in both arms.
            ctx.frames[-1].update(frame)
        elif isinstance(s, Require):
            if not self.condition(s.cond, ctx, "require"):
                message = self.template(s.message, ctx)
                self.event("require", stage=self.label(s), message=message)
                raise _Fail(Outcome.failed("requirement", message=message))
        elif isinstance(s, Note):
            self.event("note", stage=self.label(s), message=self.template(s.message, ctx))
        else:
            return self.expr(s.value, ctx)
        return None

    def branch(self, body: Sequence[Stmt], keep: Expr, ctx: _Context,
               frame: dict[str, Any], lease: Any | None) -> Any:
        inner = _Context([*ctx.frames, frame], lease, branch=True)
        try:
            self.block(body, inner)
            return self.expr(keep, inner)
        except _Fail as exc:
            self.event("branch-ended", outcome=exc.outcome.to_json())
            return Unavailable(exc.outcome)

    def fork(self, s: Fork, ctx: _Context) -> list[Any]:
        def run(index: int) -> Any:
            lease = ctx.lease
            if s.isolated:
                lease = self.child_lease(ctx.lease, f"{s.into}-{index}")
            return self.branch(s.body, s.keep, ctx, {"branch": float(index)}, lease)

        return self.map(run, list(range(1, s.count + 1)))

    def each(self, s: Each, ctx: _Context) -> list[Any]:
        source = self.expr(s.source, ctx)
        if not isinstance(source, list):
            raise _Fail(Outcome.invalid("not-a-list"))
        if len(source) > s.limit:
            self.event("truncated", stage=self.label(s), items=len(source), limit=s.limit)
        return [self.branch(s.body, s.keep, ctx, {s.var: item}, ctx.lease)
                for item in source[: s.limit]]

    # -- the run -------------------------------------------------------------------

    def run(self) -> RunResult:
        if self.options.evidence is not None:
            from ..evidence import EvidenceStore

            store = EvidenceStore(self.options.evidence)
            self.record = store.create_run(self.options.label or self.wf.name)
            self.write_text("workflow.fp", self.source_text or format_workflow(self.wf))
            self.write_json("check.json", self.report.to_json())
            self.write_json("bindings.json", {
                role: {"harness": h.name, "model": self.models.get(role)}
                for role, h in sorted(self.harnesses.items())})
            self.write_json("inputs.json", to_json(self.inputs))
        self.event("run-start", workflow=self.wf.name, workspace=self.options.workspace,
                   ledger=self.ledger.snapshot())
        outcome: Outcome
        value: Any = None
        result_patch: str | None = None
        try:
            if not self.report.ok:
                raise _Stop(Outcome.invalid("check-failed",
                                            errors=[d.to_json() for d in self.report.errors]))
            outcome, value, result_patch = self._execute()
        except _Stop as stop:
            outcome = stop.outcome
        except KeyboardInterrupt:
            outcome = Outcome.cancelled()
        finally:
            if not self.options.keep_workspaces:
                for lease in reversed(self.leases):
                    try:
                        lease.release()
                    except Exception as exc:  # report, but finish sealing the evidence
                        self.event("release-failed", lease=lease.id, error=repr(exc))
        held = all(self.within_bound(role, n) for role, n in self.calls_by_role.items())
        result = RunResult(outcome, to_json(value), self.ledger.spent, dict(self.calls_by_role),
                           self.report, held,
                           None if self.record is None else self.record.id,
                           None if self.record is None else Path(self.record.root),
                           result_patch)
        self.event("run-end", outcome=outcome.to_json(), spent=self.ledger.spent.to_json(),
                   bound_held=held)
        if self.record is not None:
            if result_patch:
                self.write_text("result.patch", result_patch)
            self.write_json("report.json", result.to_json())
            self.record.seal()
        return result

    def within_bound(self, role: str, calls: int) -> bool:
        interval = self.report.calls_by_role.get(role)
        return interval is not None and (interval.hi is None or calls <= interval.hi)

    def _execute(self) -> tuple[Outcome, Any, str | None]:
        tests = [st.value for st in walk(self.wf.body)
                 if isinstance(st, (Let, Var, Set)) and isinstance(st.value, Test)]
        if tests:
            from ..guards import FrozenTests

            # Snapshot graders from the source before any agent can touch them.
            for test in tests:
                key = tuple(p.text() for p in test.frozen)
                if key not in self.frozen:
                    self.frozen[key] = FrozenTests.capture(self.options.source, key)
                    self.event("frozen", patterns=list(key))
        root = self.lease(self.options.source, "root", self.options.workspace)
        ctx = _Context([dict(self.inputs)], root)
        try:
            value = self.block(self.wf.body, ctx)
        except _Fail as exc:
            return exc.outcome, None, None
        patch = None
        chosen = value.change if isinstance(value, GradedValue) else value
        if isinstance(chosen, ChangeValue):
            patch = chosen.change.patch
        return Outcome.ok(), value, patch


def execute(workflow: Workflow, harnesses: Mapping[str, Harness], **kwargs: Any) -> RunResult:
    return Executor(workflow, harnesses, **kwargs).run()


__all__ = ["Executor", "RunOptions", "RunResult", "coerce_inputs", "execute", "parse_answer",
           "parse_verdict"]
