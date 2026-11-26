"""E1: plan batching, prediction gating and the adaptive arm under equal credit ceilings.

    python -m research.e1_arms preflight --models claude-haiku-5.5,claude-sonnet-5.5
    python -m research.e1_arms run --model MODEL --budgets 2,5,12 --workers 6
    python -m research.e1_arms run --client oracle ...   # an offline dry run, no model

Every episode gets its own ledger with the cell's credit ceiling; the harness passes the
remaining credits to the provider as a hard cap. See research/preregistration/E1.md.
"""

from __future__ import annotations

import argparse
import statistics
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from flowproof.accounting import Ledger
from flowproof.harness import CopilotHarness
from flowproof.harness.adapters import HarnessClient
from flowproof.testbed import GAMES, canary_hits, run_episode
from flowproof.testbed.clients import NoisyPredictorClient, OracleClient

from .campaign import ROOT, Campaign

PREREG = ROOT / "research" / "preregistration" / "E1.md"
ARMS = ("single", "plan", "predict", "adaptive")
DEV_SEEDS = (100, 101)
CALL_TIMEOUT_S = 300.0


def make_client(kind: str, model: str | None, game: Any, seed: int,
                effort: str | None = None) -> Any:
    if kind == "oracle":
        return OracleClient(game, seed)
    if kind == "noisy":
        return NoisyPredictorClient(game, seed, wrong_rate=0.3, noise_seed=seed)
    return HarnessClient(CopilotHarness(reasoning_effort=effort), model=model,
                         timeout_s=CALL_TIMEOUT_S)


def episode_job(campaign: Campaign, *, game: str, seed: int, arm: str, budget: float,
                max_calls: int, client_kind: str, model: str | None, k: int, m: int,
                effort: str | None = None):
    # Every factor that varies must be in the name, or records collide.
    name = f"{model or client_kind}-{effort or 'default'}-{game}-s{seed}-{arm}-b{budget:g}"

    def job() -> dict[str, Any]:
        client = make_client(client_kind, model, GAMES[game], seed, effort)
        ledger = Ledger({"credits": budget}, max_calls)
        episode = run_episode(GAMES[game], seed, arm, client, ledger, K=k, M=m)
        data = episode.to_json()
        hits = canary_hits(data["transcript"])
        row = {
            "job": name, "game": game, "seed": seed, "arm": arm, "budget": budget,
            "model": model, "effort": effort, "client": client_kind, "score": episode.score,
            "levels_cleared": episode.levels_cleared, "calls": episode.calls,
            "invalid_calls": episode.invalid_calls, "stop_reason": episode.stop_reason,
            "credits": episode.usage.get("credits").value,
            "predictions_checked": episode.predictions_checked,
            "predictions_correct": episode.predictions_correct,
            "mode_switches": len(episode.mode_switches), "canary_hits": hits,
        }
        campaign.save(f"episodes/{name}.json", {**row, "episode": data,
                                                "harness": getattr(client, "responses", [])})
        return row

    return name, job


def summarise(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    cells: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = f"{row['arm']}@{row['budget']:g}"
        cells.setdefault(key, {"arm": row["arm"], "budget": row["budget"], "rows": []})
        cells[key]["rows"].append(row)
    out = {}
    for key, cell in sorted(cells.items()):
        scores = [r["score"] for r in cell["rows"]]
        credits = [r["credits"] for r in cell["rows"]]
        out[key] = {
            "arm": cell["arm"], "budget": cell["budget"], "episodes": len(scores),
            "mean_score": statistics.fmean(scores),
            "mean_levels": statistics.fmean(r["levels_cleared"] for r in cell["rows"]),
            "mean_calls": statistics.fmean(r["calls"] for r in cell["rows"]),
            "credits_known": all(c is not None for c in credits),
            "mean_credits": None if any(c is None for c in credits) else statistics.fmean(credits),
            "invalid_calls": sum(r["invalid_calls"] for r in cell["rows"]),
            "stop_reasons": sorted({r["stop_reason"] for r in cell["rows"]}),
            "canary_hits": sum(len(r["canary_hits"]) for r in cell["rows"]),
        }
    return {"cells": out, "episodes": len(rows)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m research.e1_arms")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "run"):
        p = sub.add_parser(name)
        p.add_argument("--client", default="copilot", choices=("copilot", "oracle", "noisy"))
        p.add_argument("--games", default="relay,glide,twins")
        p.add_argument("--arms", default=",".join(ARMS))
        p.add_argument("--max-calls", type=int, default=120)
        p.add_argument("--workers", type=int, default=4)
        p.add_argument("--k", type=int, default=12)
        p.add_argument("--m", type=int, default=4)
        p.add_argument("--out", type=Path, default=ROOT / "research" / "runs")
        if name == "preflight":
            p.add_argument("--models", required=True)
            p.add_argument("--budget", type=float, default=3.0)
        else:
            p.add_argument("--model")
            p.add_argument("--budgets", required=True)
            p.add_argument("--seeds", default="0,1,2")
    args = parser.parse_args(argv)
    games = args.games.split(",")
    arms = args.arms.split(",")
    jobs = []
    if args.command == "preflight":
        models = args.models.split(",")
        plan = {"experiment": "E1-preflight", "models": models, "games": games, "arms": arms,
                "seeds": list(DEV_SEEDS), "budget": args.budget, "max_calls": args.max_calls,
                "client": args.client, "k": args.k, "m": args.m}
        campaign = Campaign("E1-preflight", plan, root=args.out)
        for model in models:
            for game in games:
                for arm in arms:
                    name, job = episode_job(campaign, game=game, seed=DEV_SEEDS[0], arm=arm,
                                            budget=args.budget, max_calls=args.max_calls,
                                            client_kind=args.client, model=model, k=args.k,
                                            m=args.m)
                    jobs.append((f"{model}:{name}", job))
    else:
        budgets = [float(b) for b in args.budgets.split(",")]
        seeds = [int(s) for s in args.seeds.split(",")]
        plan = {"experiment": "E1", "model": args.model, "games": games, "arms": arms,
                "seeds": seeds, "budgets": budgets, "max_calls": args.max_calls,
                "client": args.client, "k": args.k, "m": args.m,
                "call_timeout_s": CALL_TIMEOUT_S}
        campaign = Campaign("E1", plan, root=args.out,
                            preregistration=PREREG if PREREG.exists() else None)
        jobs.extend(
            episode_job(campaign, game=game, seed=seed, arm=arm, budget=budget,
                        max_calls=args.max_calls, client_kind=args.client, model=args.model,
                        k=args.k, m=args.m)
            for budget in budgets for game in games for seed in seeds for arm in arms
        )
    rows = campaign.run(jobs, workers=args.workers)
    summary = summarise(rows)
    if args.command == "preflight":
        by_model: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_model.setdefault(row["model"], []).append(row)
        summary["models"] = {
            model: {
                "episodes": len(rs),
                "credits_per_call": (None if any(r["credits"] is None for r in rs) else
                                     sum(r["credits"] for r in rs) / max(1, sum(r["calls"]
                                                                                 for r in rs))),
                "invalid_rate": sum(r["invalid_calls"] for r in rs) / max(1, sum(r["calls"]
                                                                                 for r in rs)),
                "mean_levels": statistics.fmean(r["levels_cleared"] for r in rs),
                "mean_score": statistics.fmean(r["score"] for r in rs),
            }
            for model, rs in sorted(by_model.items())
        }
    root = campaign.seal(summary)
    print(f"sealed: {root}")
    for key, cell in summary["cells"].items():
        print(f"{key:>16}: score {cell['mean_score']:.3f}, levels {cell['mean_levels']:.2f}, "
              f"calls {cell['mean_calls']:.1f}, credits {cell['mean_credits']}")
    for model, info in summary.get("models", {}).items():
        print(f"{model}: {info}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
