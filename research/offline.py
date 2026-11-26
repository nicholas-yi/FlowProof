"""Offline studies: no model, deterministic for a seed.

    python -m research.offline        # writes research/results/offline.json

O1 (accounting): inject missing usage measurements and compare a ledger that reads them as
zero with Flowproof's fail-closed ledger. O2 (selection): best-of-N success under the
selection policies, and the effect of judge position bias, from `flowproof.select.simulate`.
"""

from __future__ import annotations

import json
import math
import random
import statistics
from typing import Any

from flowproof.accounting import AccountingUnknown, Ledger, LimitReached, Usage
from flowproof.select.simulate import simulate, sweep

from .campaign import ROOT

RESULTS = ROOT / "research" / "results"
MISSING_RATES = (0.0, 0.01, 0.02, 0.05, 0.1, 0.2)


def _costs(rng: random.Random, mean: float) -> float:
    sigma = 0.6
    return rng.lognormvariate(math.log(mean) - sigma * sigma / 2, sigma)


def run_naive(rng: random.Random, ceiling: float, missing: float, mean: float,
              max_calls: int) -> tuple[float, float]:
    """A ledger that reads an unmeasured call as free. Returns (reported, true) spend."""
    reported = true = 0.0
    for _ in range(max_calls):
        if reported >= ceiling:
            break
        cost = _costs(rng, mean)
        true += cost
        if rng.random() >= missing:
            reported += cost
    return reported, true


def run_fail_closed(rng: random.Random, ceiling: float, missing: float, mean: float,
                    max_calls: int) -> tuple[float, bool]:
    """Flowproof's ledger. Returns (true spend, stopped because spend became unknown)."""
    ledger = Ledger({"credits": ceiling}, max_calls)
    true = 0.0
    while True:
        try:
            ledger.admit()
        except LimitReached:
            return true, False
        except AccountingUnknown:
            return true, True
        cost = _costs(rng, mean)
        true += cost
        measured = rng.random() >= missing
        ledger.settle(Usage.of(credits=cost) if measured else Usage.unknown("credits"))


def accounting_study(*, runs: int = 4000, ceiling: float = 20.0, seed: int = 0) -> dict[str, Any]:
    rows = []
    for missing in MISSING_RATES:
        rng = random.Random(f"{seed}-{missing}")
        naive = [run_naive(rng, ceiling, missing, 1.0, 200) for _ in range(runs)]
        closed = [run_fail_closed(rng, ceiling, missing, 1.0, 200) for _ in range(runs)]
        over = [true / ceiling - 1 for _, true in naive]
        rows.append({
            "missing_rate": missing,
            "naive_reported_over_true": statistics.fmean(r / t for r, t in naive if t > 0),
            "naive_runs_over_ceiling_10pct": sum(o > 0.10 for o in over) / runs,
            "naive_mean_overshoot": statistics.fmean(over),
            "naive_p95_overshoot": sorted(over)[int(0.95 * runs)],
            "closed_runs_over_ceiling_10pct": sum(t / ceiling - 1 > 0.10 for t, _ in closed) / runs,
            "closed_stopped_unknown": sum(u for _, u in closed) / runs,
            "closed_p95_overshoot": sorted(t / ceiling - 1 for t, _ in closed)[int(0.95 * runs)],
        })
    # A comparison that flips: setup B costs 10% more per call but its provider fails to
    # report 20% of calls; a naive ledger ranks it cheaper than setup A.
    rng = random.Random(f"{seed}-flip")
    a_true, a_rep, b_true, b_rep = [], [], [], []
    for _ in range(runs):
        cost_a = [_costs(rng, 1.0) for _ in range(30)]
        cost_b = [_costs(rng, 1.1) for _ in range(30)]
        a_true.append(sum(cost_a))
        a_rep.append(sum(cost_a))
        b_true.append(sum(cost_b))
        b_rep.append(sum(c for c in cost_b if rng.random() >= 0.2))
    flip = {
        "calls_per_run": 30, "b_cost_ratio": 1.1, "b_missing_rate": 0.2,
        "a_true_mean": statistics.fmean(a_true), "b_true_mean": statistics.fmean(b_true),
        "a_reported_mean": statistics.fmean(a_rep), "b_reported_mean": statistics.fmean(b_rep),
        "naive_says_b_cheaper": statistics.fmean(b_rep) < statistics.fmean(a_rep),
        "truth_b_cheaper": statistics.fmean(b_true) < statistics.fmean(a_true),
    }
    return {"ceiling": ceiling, "runs_per_rate": runs, "rows": rows, "flip": flip, "seed": seed}


def selection_study(*, tasks: int = 2000, seed: int = 0) -> dict[str, Any]:
    ns = [1, 2, 4, 8, 16]
    curve = simulate(ns, tasks=tasks, seed=seed)
    bias = sweep("judge_position_bias", [0.0, 0.2, 0.4, 0.6, 0.8, 1.0], n_candidates=[8],
                 tasks=tasks, seed=seed)
    correlation = sweep("correlation", [0.0, 0.3, 0.6, 0.9], n_candidates=[8], tasks=tasks,
                        seed=seed)
    return {"curve": curve, "position_bias": bias, "correlation": correlation}


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = {"O1_accounting": accounting_study(), "O2_selection": selection_study()}
    path = RESULTS / "offline.json"
    path.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for row in out["O1_accounting"]["rows"]:
        print({k: round(v, 3) if isinstance(v, float) else v for k, v in row.items()})
    print(out["O1_accounting"]["flip"])
    curve = out["O2_selection"]["curve"]
    for policy in curve["policies"]:
        print(policy, [round(x, 3) for x in curve["success"][policy]])
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
