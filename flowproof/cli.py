"""The `flowproof` command line."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .lang import (
    CheckReport,
    HarnessInfo,
    SyntaxProblem,
    Workflow,
    check,
    format_workflow,
    mermaid,
    parse,
)
from .outcomes import Status

EXIT = {Status.OK: 0, Status.FAILED: 1, Status.INVALID: 3, Status.LIMITED: 4,
        Status.CANCELLED: 5}


def _load(path: str) -> tuple[Workflow, str]:
    text = Path(path).read_text(encoding="utf-8")
    try:
        return parse(text, path), text
    except SyntaxProblem as exc:
        print(f"{path}:{exc.pos.line}:{exc.pos.col}: error {exc.code}: {exc.message}",
              file=sys.stderr)
        raise SystemExit(1) from None


def _pairs(items: Sequence[str] | None, what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or ():
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise SystemExit(f"bad {what} {item!r}; expected NAME=VALUE")
        out[key] = value
    return out


def _infos(bindings: Sequence[str] | None) -> dict[str, HarnessInfo] | None:
    if not bindings:
        return None
    from .harness import make_harness
    from .runtime import parse_bindings
    from .runtime.executor import harness_info

    infos: dict[str, HarnessInfo] = {}
    for role, binding in parse_bindings(list(bindings)).items():
        infos[role] = harness_info(make_harness(binding.harness))
    return infos


def _print_report(report: CheckReport, path: str, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report.to_json(), indent=2, sort_keys=True))
        return
    for diag in report.diagnostics:
        print(diag.render(path))
    roles = ", ".join(f"{r} {i}" for r, i in sorted(report.calls_by_role.items()))
    print(f"{report.workflow.name}: agent calls {report.calls}" + (f" ({roles})" if roles else ""))
    if report.frozen:
        print(f"frozen before any agent runs: {', '.join(report.frozen)}")
    errors, warnings = len(report.errors), len(report.warnings)
    print(f"{errors} error(s), {warnings} warning(s)")


def cmd_check(args: argparse.Namespace) -> int:
    wf, _ = _load(args.file)
    report = check(wf, _infos(args.bind), workspace=args.workspace)
    _print_report(report, args.file, as_json=args.json)
    return 0 if report.ok else 1


def cmd_fmt(args: argparse.Namespace) -> int:
    wf, text = _load(args.file)
    formatted = format_workflow(wf)
    if args.check:
        if formatted != text.replace("\r\n", "\n"):
            print(f"{args.file} would be reformatted", file=sys.stderr)
            return 1
        return 0
    if args.write:
        Path(args.file).write_bytes(formatted.encode("utf-8"))
    else:
        sys.stdout.write(formatted)
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    wf, _ = _load(args.file)
    sys.stdout.write(mermaid(wf))
    return 0


def cmd_estimate(args: argparse.Namespace) -> int:
    wf, _ = _load(args.file)
    report = check(wf)
    prices = {role: float(v) for role, v in _pairs(args.price, "price").items()}
    try:
        lo, hi = report.credits(prices)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    ceiling = wf.ceiling("credits")
    high = "unbounded" if hi is None else f"{hi:g}"
    print(f"{wf.name}: agent calls {report.calls}; credits {lo:g} to {high}")
    if ceiling is not None:
        if lo > ceiling:
            print(f"error: even the cheapest path ({lo:g}) exceeds the credit ceiling {ceiling:g}")
            return 1
        if hi is None or hi > ceiling:
            print(f"warning: the worst case may exceed the credit ceiling {ceiling:g}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .harness import make_harness
    from .runtime import Executor, RunOptions, coerce_inputs, parse_bindings

    wf, text = _load(args.file)
    try:
        bindings = parse_bindings(list(args.bind or ()))
        inputs = coerce_inputs(wf, _pairs(args.input, "input"))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    harnesses: dict[str, Any] = {}
    shared: dict[str, Any] = {}
    for role, binding in bindings.items():
        if binding.harness not in shared:
            shared[binding.harness] = make_harness(binding.harness, script=args.mock_script,
                                                   reasoning_effort=args.reasoning_effort)
        harnesses[role] = shared[binding.harness]
    ceilings = {} if args.max_credits is None else {"credits": args.max_credits}
    options = RunOptions(source=Path(args.source), workspace=args.workspace,
                         evidence=None if args.no_evidence else Path(args.evidence),
                         timeout_s=args.timeout, command_timeout_s=args.timeout,
                         ceilings=ceilings, max_calls=args.max_calls, parallel=args.parallel,
                         keep_workspaces=args.keep_workspaces)
    executor = Executor(wf, harnesses, models={r: b.model for r, b in bindings.items()},
                        inputs=inputs, options=options, source_text=text)
    if not executor.report.ok:
        _print_report(executor.report, args.file, as_json=False)
        return 1
    result = executor.run()
    if args.json:
        print(json.dumps(result.to_json(), indent=2, sort_keys=True))
    else:
        outcome = result.outcome
        print(f"outcome: {outcome.status.value}" + (f" ({outcome.reason})" if outcome.reason
                                                    else ""))
        calls = ", ".join(f"{r} {n}" for r, n in sorted(result.calls_by_role.items()))
        print(f"calls: {sum(result.calls_by_role.values())} ({calls}); "
              f"static bound {result.report.calls}; held: {result.bound_held}")
        spent = ", ".join(str(a) for a in result.usage.amounts.values()) or "nothing measured"
        print(f"usage: {spent}")
        if result.evidence_root is not None:
            print(f"evidence: {result.evidence_root}")
            if result.result_patch:
                print(f"result patch: {result.evidence_root / 'result.patch'}")
    return EXIT[result.outcome.status]


def cmd_verify(args: argparse.Namespace) -> int:
    from .evidence import verify

    report = verify(Path(args.run))
    for problem in report.problems:
        print(problem)
    print("verified" if report.ok else f"{len(report.problems)} problem(s)")
    return 0 if report.ok else 1


def cmd_play(args: argparse.Namespace) -> int:
    from .accounting import Ledger
    from .testbed import GAMES, run_episode
    from .testbed.clients import NoisyPredictorClient, OracleClient, RandomClient

    game = GAMES[args.game]
    client: Any
    if args.client == "oracle":
        client = OracleClient(game, args.seed)
    elif args.client == "noisy":
        client = NoisyPredictorClient(game, args.seed, wrong_rate=0.3, noise_seed=args.seed)
    elif args.client == "random":
        client = RandomClient(args.seed)
    else:
        from .harness import CopilotHarness
        from .harness.adapters import HarnessClient

        client = HarnessClient(CopilotHarness(), model=args.model)
    ledger = Ledger({"credits": args.credits}, args.calls)
    episode = run_episode(game, args.seed, args.arm, client, ledger, K=args.k, M=args.m)
    data = episode.to_json()
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(f"{episode.game} seed {episode.seed} arm {episode.arm}: stop {episode.stop_reason}, "
              f"levels {episode.levels_cleared}/{episode.levels_total}, score {episode.score:.3f}, "
              f"calls {episode.calls}, credits {episode.usage.get('credits')}")
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    from .select.simulate import standard_sweeps

    print(json.dumps(standard_sweeps(seed=args.seed), indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flowproof", description=(
        "Typed, bounded workflows for coding agents, with evaluation that is hard to fool."))
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="parse, type-check and bound a workflow")
    p.add_argument("file")
    p.add_argument("--bind", action="append", metavar="ROLE=HARNESS[:MODEL]")
    p.add_argument("--workspace", choices=("inplace", "guarded", "copy", "worktree"))
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("fmt", help="print the canonical form")
    p.add_argument("file")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--write", action="store_true", help="rewrite the file in place")
    group.add_argument("--check", action="store_true", help="fail if the file is not canonical")
    p.set_defaults(fn=cmd_fmt)

    p = sub.add_parser("graph", help="print a Mermaid flowchart")
    p.add_argument("file")
    p.set_defaults(fn=cmd_graph)

    p = sub.add_parser("estimate", help="credit interval from per-call prices")
    p.add_argument("file")
    p.add_argument("--price", action="append", metavar="ROLE=CREDITS", required=True)
    p.set_defaults(fn=cmd_estimate)

    p = sub.add_parser("run", help="run a workflow")
    p.add_argument("file")
    p.add_argument("--bind", action="append", metavar="ROLE=HARNESS[:MODEL]", required=True)
    p.add_argument("--input", action="append", metavar="NAME=VALUE")
    p.add_argument("--source", default=".", help="directory the workspace is leased from")
    p.add_argument("--workspace", default="worktree",
                   choices=("inplace", "guarded", "copy", "worktree"))
    p.add_argument("--evidence", default=".flowproof/runs", help="evidence store directory")
    p.add_argument("--no-evidence", action="store_true")
    p.add_argument("--mock-script", help="JSON replies for the mock harness")
    p.add_argument("--reasoning-effort")
    p.add_argument("--max-credits", type=float, help="a stricter credit ceiling")
    p.add_argument("--max-calls", type=int, help="a stricter call ceiling")
    p.add_argument("--parallel", type=int, default=1, help="branches run at once")
    p.add_argument("--timeout", type=float, default=900.0, help="seconds per call or command")
    p.add_argument("--keep-workspaces", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("evidence", help="sealed run records")
    ev = p.add_subparsers(dest="evidence_command", required=True)
    v = ev.add_parser("verify", help="recompute hashes and the event chain of a run")
    v.add_argument("run")
    v.set_defaults(fn=cmd_verify)

    p = sub.add_parser("testbed", help="rule-learning games")
    tb = p.add_subparsers(dest="testbed_command", required=True)
    play = tb.add_parser("play", help="play one episode")
    play.add_argument("--game", default="relay", choices=("relay", "glide", "twins"))
    play.add_argument("--arm", default="predict", choices=("single", "plan", "predict", "adaptive"))
    play.add_argument("--client", default="oracle", choices=("oracle", "noisy", "random", "copilot"))
    play.add_argument("--model")
    play.add_argument("--seed", type=int, default=0)
    play.add_argument("--credits", type=float, default=30.0)
    play.add_argument("--calls", type=int, default=60)
    play.add_argument("--k", type=int, default=12)
    play.add_argument("--m", type=int, default=4)
    play.add_argument("--json", action="store_true")
    play.set_defaults(fn=cmd_play)

    p = sub.add_parser("select", help="selection tools")
    sel = p.add_subparsers(dest="select_command", required=True)
    sim = sel.add_parser("simulate", help="Monte Carlo of best-of-N selection")
    sim.add_argument("--seed", type=int, default=0)
    sim.set_defaults(fn=cmd_simulate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
