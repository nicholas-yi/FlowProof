"""E3: can models write Flowproof, and do checker diagnostics make repairs work?

    python -m research.e3_generate run --models claude-haiku-5.5,gpt-5.6-sol
    python -m research.e3_generate run --dry-run      # replies with a fixed workflow

Arms: `examples` (requirements and three example workflows) and `reference` (the same
plus docs/LANGUAGE.md). Workflows that fail on the first attempt get up to two repair
rounds, once with the checker's diagnostics (`diag`) and once with only "not accepted"
(`blind`), so the two repair variants are matched on rounds. Each requirement has hidden
property checks. See research/preregistration/E3.md.
"""

from __future__ import annotations

import argparse
import re
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flowproof.harness import CopilotHarness
from flowproof.harness.base import AgentRequest
from flowproof.lang import CheckReport, SyntaxProblem, Workflow, check, parse
from flowproof.lang.tree import (
    Ask,
    Choose,
    Do,
    Each,
    Edit,
    Exec,
    Fork,
    Give,
    Let,
    Note,
    Ref,
    Repeat,
    Require,
    Set,
    Test,
    Var,
    walk,
)

from .campaign import ROOT, Campaign

PREREG = ROOT / "research" / "preregistration" / "E3.md"
EXAMPLES = sorted((ROOT / "examples").glob("*.fp"))
PREAMBLE = ("Flowproof is a small language for orchestrating coding agents. Write one "
            "Flowproof workflow that meets every requirement below. Reply with only the "
            "workflow inside a single fenced code block.")
Check = Callable[[Workflow, CheckReport], bool]


def values(wf: Workflow) -> list[Any]:
    out: list[Any] = []
    for st in walk(wf.body):
        if isinstance(st, (Let, Var, Set)):
            out.append(st.value)
        elif isinstance(st, Do):
            out.append(st.action)
    return out


def of(wf: Workflow, kind: type) -> list[Any]:
    return [v for v in values(wf) if isinstance(v, kind)]


def stmts(wf: Workflow, kind: type) -> list[Any]:
    return [st for st in walk(wf.body) if isinstance(st, kind)]


def fork_with(wf: Workflow, n: int, isolated: bool, action: type, role: str) -> bool:
    for fork in stmts(wf, Fork):
        if fork.count == n and fork.isolated == isolated:
            inner = [st.value if isinstance(st, (Let, Var, Set)) else getattr(st, "action", None)
                     for st in walk(fork.body)]
            if any(isinstance(v, action) and v.role == role for v in inner):
                return True
    return False


def frozen(wf: Workflow, *patterns: str) -> bool:
    return any({p.text() for p in t.frozen} >= set(patterns) for t in of(wf, Test))


def chain(wf: Workflow, policies: Sequence[str], fallback: str) -> bool:
    for c in of(wf, Choose):
        names = [p.name if p.name != "pairwise" else f"pairwise {p.role}" for p in c.policies]
        if names == list(policies) and c.fallback == fallback:
            return True
    return False


def budget(**limits: float) -> Check:
    return lambda wf, _r: all(wf.ceiling(u) == v for u, v in limits.items())


def bound_within(calls: int) -> Check:
    return lambda _wf, r: r.calls.hi is not None and r.calls.hi <= calls


def asks(role: str, kind: str = "text") -> Check:
    return lambda wf, _r: any(a.role == role and a.kind == kind for a in of(wf, Ask))


def edits(role: str) -> Check:
    return lambda wf, _r: any(e.role == role for e in of(wf, Edit))


def execs(command: str) -> Check:
    return lambda wf, _r: any(e.command.text() == command for e in of(wf, Exec))


def tests_with(command: str) -> Check:
    return lambda wf, _r: any(t.command.text() == command for t in of(wf, Test))


def caps(role: str, *needed: str) -> Check:
    return lambda wf, _r: (r := wf.role(role)) is not None and set(needed) <= set(r.caps)


def param(name: str, kind: str, default: Any = None) -> Check:
    def ok(wf: Workflow, _r: CheckReport) -> bool:
        p = next((p for p in wf.params if p.name == name), None)
        if p is None or p.type != kind:
            return False
        return default is None or (p.default is not None and getattr(p.default, "value", None)
                                   == default)
    return ok


def has(kind: type) -> Check:
    return lambda wf, _r: bool(stmts(wf, kind))


def no_edits(wf: Workflow, _r: CheckReport) -> bool:
    return not of(wf, Edit)


def gives(wf: Workflow, _r: CheckReport) -> bool:
    return bool(wf.body) and isinstance(wf.body[-1], Give)


def feeds_output_back(wf: Workflow, _r: CheckReport) -> bool:
    return any(isinstance(p, Ref) and p.path[-1] == "out"
               for e in of(wf, Edit) for p in e.prompt.parts)


def repeat_at_most(n: int) -> Check:
    return lambda wf, _r: any(r.limit == n for r in stmts(wf, Repeat))


def each_at_most(n: int, action: type) -> Check:
    def ok(wf: Workflow, _r: CheckReport) -> bool:
        for e in stmts(wf, Each):
            inner = [st.value if isinstance(st, (Let, Var, Set)) else getattr(st, "action", None)
                     for st in walk(e.body)]
            if e.limit == n and any(isinstance(v, action) for v in inner):
                return True
        return False
    return ok


@dataclass(frozen=True)
class Spec:
    name: str
    text: str
    checks: tuple[tuple[str, Check], ...]


SPECS: tuple[Spec, ...] = (
    Spec("best_of_three", (
        "Input `issue` (text). Role `author` edits; role `reviewer` only answers. Make three "
        "isolated attempts in which the author fixes the issue. Grade every attempt with "
        "`python -m pytest -q`, freezing `tests/**`. Choose by tests, then let the reviewer "
        "judge pairwise; if still tied, abstain. Fail with a message if the chosen attempt did "
        "not pass. Give the chosen attempt. Budget: at most 40 credits and 10 calls."), (
        ("isolated fork of 3 author edits", lambda w, r: fork_with(w, 3, True, Edit, "author")),
        ("tests command", tests_with("python -m pytest -q")),
        ("frozen tests/**", lambda w, r: frozen(w, "tests/**")),
        ("tests then pairwise reviewer, abstain",
         lambda w, r: chain(w, ["tests", "pairwise reviewer"], "abstain")),
        ("require", has(Require)), ("gives", gives),
        ("budget", budget(credits=40, calls=10)), ("bound within 10", bound_within(10)),
    )),
    Spec("repair_loop", (
        "Input `task` (text). One role `author` that edits. The author attempts the task once; "
        "grade it with `python -m unittest`, freezing `tests/**`. If it did not pass, repeat at "
        "most three times: have the author fix the code, showing it the failing test output, "
        "then grade again. Finally require a pass and give the last grade. Budget: 4 calls."), (
        ("var and set", lambda w, r: bool(stmts(w, Var)) and bool(stmts(w, Set))),
        ("repeat at most 3", repeat_at_most(3)),
        ("tests command", tests_with("python -m unittest")),
        ("frozen tests/**", lambda w, r: frozen(w, "tests/**")),
        ("failure output fed back", feeds_output_back), ("require", has(Require)),
        ("gives", gives), ("budget", budget(calls=4)), ("bound within 4", bound_within(4)),
    )),
    Spec("review_fanout", (
        "Input `summary` (text). Roles `scout` and `reviewer` only answer. The scout lists the "
        "files likely affected by the summary (a list). The reviewer reviews at most four of "
        "them, one at a time. The scout then summarises the reviews. Give the summary. "
        "Budget: 6 calls."), (
        ("scout asks for a list", asks("scout", "list")),
        ("for each at most 4 asks", each_at_most(4, Ask)), ("no edits", no_edits),
        ("gives", gives), ("budget", budget(calls=6)), ("bound within 6", bound_within(6)),
    )),
    Spec("plan_then_edit", (
        "Input `goal` (text). Role `planner` only answers; role `coder` edits. The planner "
        "writes a plan for the goal, the coder implements the plan, and then the workflow "
        "runs `python -m pytest -q` as a command. Give the command's result. Budget: 2 calls "
        "and 10 credits."), (
        ("planner asks", asks("planner")), ("coder edits", edits("coder")),
        ("exec command", execs("python -m pytest -q")), ("gives", gives),
        ("budget", budget(calls=2, credits=10)), ("bound within 2", bound_within(2)),
    )),
    Spec("vote_exact", (
        "Input `issue` (text). Role `author` edits. Make five isolated attempts at the issue. "
        "Grade them all with `npm test`, freezing both `test/**` and `package.json`. Choose by "
        "tests, then by behaviour, then by exact patch votes, and fall back to the first "
        "candidate. Give it. Budget: 5 calls."), (
        ("isolated fork of 5 author edits", lambda w, r: fork_with(w, 5, True, Edit, "author")),
        ("tests command", tests_with("npm test")),
        ("both frozen", lambda w, r: frozen(w, "test/**", "package.json")),
        ("tests, behavior, exact, first",
         lambda w, r: chain(w, ["tests", "behavior", "exact"], "first")),
        ("gives", gives), ("budget", budget(calls=5)), ("bound within 5", bound_within(5)),
    )),
    Spec("guarded_question", (
        "Inputs `question` (text) and `strict` (bool, default true). Role `analyst` only "
        "answers. Ask the analyst whether the repository already implements what the question "
        "describes, as a yes/no answer. If strict and the answer is no, fail with the message "
        "'not implemented'. Give the answer. Budget: 1 call."), (
        ("bool ask", asks("analyst", "bool")), ("strict input", param("strict", "bool", True)),
        ("require", has(Require)), ("gives", gives), ("budget", budget(calls=1)),
        ("bound within 1", bound_within(1)),
    )),
    Spec("count_and_fanout", (
        "Input `topic` (text). Role `analyst` only answers. Ask the analyst how many independent "
        "sub-questions the topic has, as a number. Then ask three analysts at once, without "
        "isolating them because they only answer, for an answer to the topic each, and give "
        "all three answers. Budget: 4 calls."), (
        ("number ask", asks("analyst", "number")),
        ("shared fork of 3 asks", lambda w, r: fork_with(w, 3, False, Ask, "analyst")),
        ("gives", gives), ("budget", budget(calls=4)), ("bound within 4", bound_within(4)),
    )),
    Spec("judge_answers", (
        "Input `prompt` (text). Roles `writer` and `judge` only answer. Four writers each draft "
        "a response at once (no isolation needed). The judge picks the best one by pairwise "
        "comparison; if that is a tie, take the first. Give the choice. Budget: at most 20 "
        "calls."), (
        ("shared fork of 4 writer asks", lambda w, r: fork_with(w, 4, False, Ask, "writer")),
        ("pairwise judge, first", lambda w, r: chain(w, ["pairwise judge"], "first")),
        ("gives", gives), ("budget", budget(calls=20)), ("bound within 20", bound_within(20)),
    )),
    Spec("iterative_review", (
        "Input `task` (text). Role `author` edits; role `critic` only answers. The author makes "
        "a change for the task. Then, at most twice, until the critic approves: ask the critic "
        "whether the latest change is acceptable (yes/no), and if it is not, have the author "
        "revise it. Give the final change. Budget: 5 calls."), (
        ("var and set", lambda w, r: bool(stmts(w, Var)) and bool(stmts(w, Set))),
        ("repeat at most 2", repeat_at_most(2)), ("critic bool ask", asks("critic", "bool")),
        ("author edits", edits("author")), ("gives", gives), ("budget", budget(calls=5)),
        ("bound within 5", bound_within(5)),
    )),
    Spec("lint_gate", (
        "Input `ticket` (text). Role `author` edits. The author makes the change; write a note "
        "saying 'edit done'; run `make lint` as a command and require that it succeeded, with "
        "the message 'lint failed'; then give the change. Budget: 1 call and 5 credits."), (
        ("author edits", edits("author")),
        ("note", lambda w, r: any(n.message.text() == "edit done" for n in stmts(w, Note))),
        ("exec make lint", execs("make lint")), ("require", has(Require)), ("gives", gives),
        ("budget", budget(calls=1, credits=5)), ("bound within 1", bound_within(1)),
    )),
    Spec("isolated_shell", (
        "Input `target` (text). Role `builder` can edit and run shell commands. Make two "
        "isolated attempts by the builder to build the target. Grade each with `cargo test`, "
        "freezing `tests/**`. Choose by tests and otherwise abstain. Give the choice. Budget: "
        "2 calls."), (
        ("builder can edit and shell", caps("builder", "edit", "shell")),
        ("isolated fork of 2 builder edits", lambda w, r: fork_with(w, 2, True, Edit, "builder")),
        ("tests command", tests_with("cargo test")),
        ("frozen tests/**", lambda w, r: frozen(w, "tests/**")),
        ("tests, abstain", lambda w, r: chain(w, ["tests"], "abstain")),
        ("gives", gives), ("budget", budget(calls=2)), ("bound within 2", bound_within(2)),
    )),
    Spec("triage_and_fix", (
        "Input `pr` (text). Role `triager` only answers; role `fixer` edits. The triager lists "
        "the problems in the pull request (a list). For each problem, at most three, the fixer "
        "fixes it in the shared workspace (no forks). Then run `pytest` as a command and give "
        "its result. Budget: 4 calls."), (
        ("triager asks for a list", asks("triager", "list")),
        ("for each at most 3 edits", each_at_most(3, Edit)),
        ("no forks", lambda w, r: not stmts(w, Fork)), ("exec pytest", execs("pytest")),
        ("gives", gives), ("budget", budget(calls=4)), ("bound within 4", bound_within(4)),
    )),
)


def prompt_for(spec: Spec, arm: str) -> str:
    parts = [PREAMBLE, "", "Requirements:", spec.text, ""]
    if arm == "reference":
        parts += ["Language reference:", (ROOT / "docs" / "LANGUAGE.md").read_text("utf-8"), ""]
    parts.append("Example workflows:")
    for path in EXAMPLES:
        parts += [f"```flowproof\n{path.read_text('utf-8').strip()}\n```", ""]
    return "\n".join(parts)


def extract(text: str) -> str:
    match = re.search(r"```[\w-]*\n(.*?)```", text, flags=re.S)
    return (match.group(1) if match else text).strip() + "\n"


def evaluate(spec: Spec, source: str) -> dict[str, Any]:
    try:
        wf = parse(source, "generated.fp")
    except SyntaxProblem as exc:
        return {"parsed": False, "clean": False, "passed": [], "failed": [n for n, _ in spec.checks],
                "success": False, "diagnostics": [f"{exc.pos}: {exc.code}: {exc.message}"]}
    report = check(wf)
    passed, failed = [], []
    for name, fn in spec.checks:
        try:
            ok = bool(fn(wf, report))
        except Exception:
            ok = False
        (passed if ok else failed).append(name)
    clean = report.ok
    return {"parsed": True, "clean": clean, "passed": passed, "failed": failed,
            "success": clean and not failed,
            "diagnostics": [d.render("generated.fp") for d in report.diagnostics]}


def feedback(variant: str, result: dict[str, Any]) -> str:
    if variant == "blind":
        return "Your workflow was not accepted. Reply with a corrected workflow in one fenced block."
    lines = ["Your workflow was not accepted. The checker reported:"]
    lines += result["diagnostics"] or ["(no diagnostics)"]
    if result["parsed"] and result["clean"] and result["failed"]:
        lines.append("It parses and type-checks, but it does not yet meet all the requirements.")
    lines.append("Reply with a corrected workflow in one fenced block.")
    return "\n".join(lines)


class Model:
    def __init__(self, name: str, dry: bool):
        self.name = name
        self.dry = dry
        self.harness = CopilotHarness()

    def ask(self, prompt: str) -> tuple[str, float | None, str]:
        if self.dry:
            return f"```\n{EXAMPLES[0].read_text('utf-8')}```", 0.0, "ok"
        response = self.harness.invoke(AgentRequest("writer", prompt, "answer", None,
                                                    model=self.name, max_credits=30,
                                                    timeout_s=300))
        return (response.text, response.usage.get("credits").value,
                response.outcome.status.value)


def job_for(campaign: Campaign, spec: Spec, model: Model, rounds: int):
    name = f"{spec.name}-{model.name}"

    def job() -> dict[str, Any]:
        row: dict[str, Any] = {"job": name, "spec": spec.name, "model": model.name, "arms": {}}
        credits: list[float | None] = []
        for arm in ("examples", "reference"):
            prompt = prompt_for(spec, arm)
            text, cost, status = model.ask(prompt)
            credits.append(cost)
            first = evaluate(spec, extract(text))
            entry: dict[str, Any] = {"first": first, "status": status, "reply": text[-4000:]}
            if arm == "reference" and not first["success"]:
                for variant in ("diag", "blind"):
                    history, result, trail = prompt, first, []
                    reply = text
                    for _ in range(rounds):
                        history = f"{history}\n\n{reply}\n\n{feedback(variant, result)}"
                        reply, cost, status = model.ask(history)
                        credits.append(cost)
                        result = evaluate(spec, extract(reply))
                        trail.append({"result": result, "status": status})
                        if result["success"]:
                            break
                    entry[variant] = {"rounds": trail, "success": result["success"]}
            row["arms"][arm] = entry
        row["credits"] = None if any(c is None for c in credits) else sum(c or 0 for c in credits)
        campaign.save(f"jobs/{name}.json", row)
        return row

    return name, job


def summarise(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for model in sorted({r["model"] for r in rows}):
        sel = [r for r in rows if r["model"] == model]
        cell: dict[str, Any] = {"specs": len(sel)}
        for arm in ("examples", "reference"):
            firsts = [r["arms"][arm]["first"] for r in sel]
            cell[arm] = {
                "parsed": sum(f["parsed"] for f in firsts),
                "clean": sum(f["clean"] for f in firsts),
                "success": sum(f["success"] for f in firsts),
                "property_rate": statistics.fmean(
                    len(f["passed"]) / max(1, len(f["passed"]) + len(f["failed"]))
                    for f in firsts),
            }
        failing = [r for r in sel if "diag" in r["arms"]["reference"]]
        cell["repair"] = {"failing_first": len(failing),
                          "diag_success": sum(r["arms"]["reference"]["diag"]["success"]
                                              for r in failing),
                          "blind_success": sum(r["arms"]["reference"]["blind"]["success"]
                                               for r in failing)}
        credits = [r["credits"] for r in sel]
        cell["credits"] = None if any(c is None for c in credits) else sum(credits)
        out[model] = cell
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m research.e3_generate")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run")
    p.add_argument("--models", default="dry")
    p.add_argument("--specs", default=",".join(s.name for s in SPECS))
    p.add_argument("--rounds", type=int, default=2)
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--out", type=Path, default=ROOT / "research" / "runs")
    args = parser.parse_args(argv)
    specs = [s for s in SPECS if s.name in args.specs.split(",")]
    models = [Model(m, args.dry_run) for m in args.models.split(",")]
    plan = {"experiment": "E3", "models": [m.name for m in models], "rounds": args.rounds,
            "specs": [{"name": s.name, "text": s.text, "checks": [n for n, _ in s.checks]}
                      for s in specs], "dry_run": args.dry_run, "preamble": PREAMBLE}
    campaign = Campaign("E3" + ("-dry" if args.dry_run else ""), plan, root=args.out,
                        preregistration=PREREG if PREREG.exists() and not args.dry_run else None)
    rows = campaign.run([job_for(campaign, s, m, args.rounds) for m in models for s in specs],
                        workers=args.workers)
    summary = summarise(rows)
    root = campaign.seal(summary)
    print(f"sealed: {root}")
    for model, cell in summary.items():
        print(model, cell)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
