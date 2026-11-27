"""Exploratory analyses: descriptive breakdowns that were not pre-registered.

    python -m research.explore        # writes research/results/EXPLORATORY.json

Everything here is read from the same verified, sealed records as `research.analyze`, plus
the E2 detail record (`research.e2_detail`). Nothing here is a confirmatory test; the README
labels these numbers as exploratory.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from flowproof.testbed import GAMES, optimal_actions

from .analyze import latest, verified
from .campaign import ROOT, RUNS, load_jsons
from .e3_generate import extract

RESULTS = ROOT / "research" / "results"
PRETEND_TOOL = re.compile(r"<invoke|<parameter name=")


def mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return statistics.fmean(values) if values else None


def _usage(entry: dict[str, Any]) -> dict[str, Any]:
    usage = entry.get("usage") or {}
    return usage.get("amounts", {}) if isinstance(usage, dict) else {}


# -- E1 ---------------------------------------------------------------------------------


def e1(run: Path) -> dict[str, Any]:
    rows = load_jsons(run / "episodes")
    arms = ["single", "plan", "predict", "adaptive"]
    games = sorted({r["game"] for r in rows})
    by_game = {g: {a: mean(r["score"] for r in rows if r["game"] == g and r["arm"] == a)
                   for a in arms} for g in games}
    by_game_budget = {f"{g}@{b:g}": {a: mean(r["score"] for r in rows if r["game"] == g
                                             and r["arm"] == a and r["budget"] == b)
                                     for a in arms}
                      for g in games for b in sorted({r["budget"] for r in rows})}

    level_clear: dict[str, list[float | None]] = {}
    for arm in arms:
        sel = [r for r in rows if r["arm"] == arm]
        level_clear[arm] = [
            mean(1.0 if any(lv["level"] == level and lv["cleared"]
                            for lv in r["episode"]["per_level"]) else 0.0 for r in sel)
            for level in (1, 2, 3, 4)]
    efficiency = {arm: mean(lv["efficiency"] for r in rows if r["arm"] == arm
                            for lv in r["episode"]["per_level"] if lv["cleared"]) for arm in arms}

    # Predictions: is the first check after a rule change wrong more often than later ones?
    first, later = Counter(), Counter()
    by_game_pred: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if r["arm"] not in ("predict", "adaptive"):
            continue
        seen_levels: set[int] = set()
        for entry in r["episode"]["transcript"]:
            for check in entry.get("predictions") or []:
                level = int(entry.get("level") or 0)
                bucket = later if level in seen_levels else first
                seen_levels.add(level)
                bucket["checked"] += 1
                bucket["correct"] += bool(check.get("match"))
                by_game_pred[r["game"]]["checked"] += 1
                by_game_pred[r["game"]]["correct"] += bool(check.get("match"))

    cost: dict[str, Any] = {}
    for arm in arms:
        entries = [e for r in rows if r["arm"] == arm for e in r["episode"]["transcript"]]
        credits = [_usage(e).get("credits") for e in entries]
        tokens = [_usage(e).get("tokens") for e in entries]
        cost[arm] = {
            "calls": len(entries),
            "credits_per_call": mean(c for c in credits if c is not None),
            "tokens_per_call": mean(t for t in tokens if t is not None),
            "prompt_chars": mean(float(e.get("prompt_length") or 0) for e in entries),
            "pretend_tool_calls": sum(bool(PRETEND_TOOL.search(e.get("response") or ""))
                                      for e in entries),
            "invalid_calls": sum(r["invalid_calls"] for r in rows if r["arm"] == arm),
            "actions_per_call": mean(len(e.get("played") or []) for e in entries
                                     if e.get("outcome") != "invalid"),
        }

    # Overshoot: how far past its ceiling did a credit-limited episode end?
    overshoot, largest_call = [], []
    for r in rows:
        calls = [_usage(e).get("credits") for e in r["episode"]["transcript"]]
        known = [c for c in calls if c is not None]
        if known:
            largest_call.append(max(known))
        if r["stop_reason"] == "limited" and r["credits"] is not None:
            overshoot.append({"over": r["credits"] - r["budget"],
                              "last_call": known[-1] if known else None})
    adaptive_modes = Counter(e.get("mode") for r in rows if r["arm"] == "adaptive"
                             for e in r["episode"]["transcript"])
    # Units a paired comparison would need for 80% power (two-sided 5%, normal approximation)
    # if the observed mean difference and spread were the truth. Not corrected for 9 tests.
    score = {(r["arm"], r["budget"], r["game"], r["seed"]): r["score"] for r in rows}
    units = sorted({(r["game"], r["seed"]) for r in rows})
    power = {}
    for budget in sorted({r["budget"] for r in rows}):
        for x, y in (("adaptive", "plan"), ("adaptive", "predict"), ("predict", "plan")):
            diffs = [score[(x, budget, g, s)] - score[(y, budget, g, s)] for g, s in units]
            m, sd = statistics.fmean(diffs), statistics.stdev(diffs)
            power[f"{x} - {y} @ {budget:g}"] = {
                "mean_diff": m, "sd": sd,
                "units_for_80pct_power": math.ceil(((1.96 + 0.8416) * sd / m) ** 2) if m else None}
    return {
        "run": run.relative_to(ROOT).as_posix(),
        "by_game": by_game, "by_game_budget": by_game_budget,
        "level_clear_rate": level_clear, "efficiency_of_cleared_levels": efficiency,
        "prediction_accuracy": {
            "first_check_in_level": first["correct"] / first["checked"] if first["checked"] else None,
            "later_checks": later["correct"] / later["checked"] if later["checked"] else None,
            "first_checked": first["checked"], "later_checked": later["checked"],
            "by_game": {g: c["correct"] / c["checked"] for g, c in sorted(by_game_pred.items())},
        },
        "cost_by_arm": cost,
        "overshoot": {
            "limited_episodes": len(overshoot),
            "max_over": max((o["over"] for o in overshoot), default=None),
            "mean_over": mean(o["over"] for o in overshoot),
            "all_within_last_call": all(o["last_call"] is not None and o["over"] <= o["last_call"]
                                        + 1e-9 for o in overshoot),
            "largest_single_call": max(largest_call, default=None),
        },
        "adaptive_call_modes": dict(adaptive_modes),
        "units": len(units), "power": power,
        "pretend_tool_calls": sum(c["pretend_tool_calls"] for c in cost.values()),
        "responses": sum(c["calls"] for c in cost.values()),
    }


# -- E2 ---------------------------------------------------------------------------------


def e2(run: Path, detail: Path) -> dict[str, Any]:
    rows = {r["job"]: r for r in load_jsons(run / "jobs")}
    details = {d["job"]: d for d in load_jsons(detail / "jobs") if not d.get("missing")}
    cells: dict[str, Any] = {}
    for key in sorted({(d["model"], d["arm"], d["kind"]) for d in details.values()}):
        sel = [d for d in details.values() if (d["model"], d["arm"], d["kind"]) == key]
        b = [d["behaviour"] for d in sel]
        cells["/".join(key)] = {
            "runs": len(sel),
            "tool_calls": mean(x["tool_calls"] for x in b),
            "test_commands": mean(x["test_commands"] for x in b),
            "ran_tests": sum(x["test_commands"] > 0 for x in b),
            "permission_denials": sum(x["permission_denials"] for x in b),
            "median_seconds": statistics.median(d["agent_seconds"] for d in sel),
            "credits_per_run": mean(d["credits"] for d in sel),
            "tamper_modes": dict(Counter(d["change"]["tamper_mode"] for d in sel)),
        }
    by_model = {}
    for model in sorted({d["model"] for d in details.values()}):
        sel = [d for d in details.values() if d["model"] == model]
        by_model[model] = {"runs": len(sel), "credits_per_run": mean(d["credits"] for d in sel),
                           "median_seconds": statistics.median(d["agent_seconds"] for d in sel),
                           "tokens_per_run": mean(d["tokens"] for d in sel if d["tokens"])}
    impossible = [r for r in rows.values() if r["kind"] == "impossible"
                  and r["outcome"]["status"] == "ok"]
    styles = Counter()
    for r in impossible:
        if r["definitive_tamper"]:
            styles["open tampering" if r["disclosed"] else "silent tampering"] += 1
        elif r["passed"]:
            styles["silent special-casing" if not r["disclosed"] else "disclosed special-casing"] += 1
        elif r["disclosed"]:
            styles["declined and explained"] += 1
        else:
            styles["failed without explaining"] += 1
    per_task = defaultdict(dict)
    for r in impossible:
        key = f"{r['model']}/{r['arm']}"
        entry = per_task[r["task"]].setdefault(key, {"tamper": 0, "special": 0, "runs": 0})
        entry["runs"] += 1
        entry["tamper"] += bool(r["definitive_tamper"])
        entry["special"] += bool(r["passed"])
    return {"run": run.relative_to(ROOT).as_posix(),
            "detail": detail.relative_to(ROOT).as_posix(), "cells": cells,
            "by_model": by_model, "impossible_styles": dict(styles),
            "impossible_by_task": dict(per_task),
            "contradiction_kinds": {k: sum(1 for r in impossible if r["contradiction"] == k)
                                    for k in ("same-input", "spec")}}


# -- E3 ---------------------------------------------------------------------------------

CATEGORIES = (
    (re.compile(r"=\s*(run|command|shell|sh|execute)\s+\""), "invented a command keyword "
     "(`run`, `command`) instead of `exec`"),
    (re.compile(r"found 'fail'"), "invented `fail` instead of `require`"),
    (re.compile(r"found 'behaviour'"), "spelled `behavior` the British way"),
    (re.compile(r"expected a capability"), "invented a role capability"),
)


def categorise(spec_reply: str, first: dict[str, Any]) -> str:
    diagnostics = first["diagnostics"]
    if first["parsed"] and first["clean"]:
        return "valid, but missed a hidden requirement"
    if first["parsed"]:
        errors = [d for d in diagnostics if " error " in d]
        code = errors[0].split(" error ")[1].split(":")[0] if errors else "error"
        return f"type error ({code})"
    message = diagnostics[0] if diagnostics else ""
    match = re.search(r"^(\d+):", message)
    line = ""
    if match:
        lines = extract(spec_reply).splitlines()
        number = int(match.group(1))
        line = lines[number - 1] if 0 < number <= len(lines) else ""
    for pattern, label in CATEGORIES:
        if pattern.search(line) or pattern.search(message):
            return label
    return "other syntax error"


def e3(run: Path, repair: Path | None) -> dict[str, Any]:
    rows = load_jsons(run / "jobs")
    categories = Counter()
    for r in rows:
        arm = r["arms"]["examples"]
        if not arm["first"]["success"]:
            categories[categorise(arm["reply"], arm["first"])] += 1
    credits = {m: sum(r["credits"] for r in rows if r["model"] == m)
               for m in sorted({r["model"] for r in rows})}
    out: dict[str, Any] = {"run": run.relative_to(ROOT).as_posix(),
                           "examples_failures": dict(categories.most_common()),
                           "credits_by_model": credits,
                           "credits_per_call_by_model": {m: c / (2 * sum(
                               1 for r in rows if r["model"] == m)) for m, c in credits.items()}}
    if repair is not None:
        jobs = load_jsons(repair / "jobs")
        out["repair_rounds_to_success"] = {
            v: dict(Counter(len(j[v]["rounds"]) for j in jobs if j[v]["success"]))
            for v in ("diag", "blind")}
        out["repair_parsed"] = {v: sum(j[v]["parsed"] for j in jobs) for v in ("diag", "blind")}
        out["repair_items"] = len(jobs)
        out["repair_credits"] = sum(j["credits"] for j in jobs)
    return out


# -- totals and testbed -------------------------------------------------------------------


def totals() -> dict[str, Any]:
    records, events, calls, credits = [], 0, 0, 0.0
    for run in sorted(RUNS.iterdir()):
        if not (run / "manifest.json").exists():
            continue
        verified(run)
        plan = json.loads((run / "plan.json").read_text("utf-8"))
        records.append({"id": run.name, "experiment": plan.get("experiment")})
        for name in ("events.jsonl", "original-events.jsonl"):
            path = run / name
            if path.exists():
                events += sum(1 for _ in path.open(encoding="utf-8"))
    e1 = load_jsons(latest("E1") / "episodes")  # type: ignore[operator]
    calls += sum(r["calls"] for r in e1)
    credits += sum(_usage(e).get("credits") or 0.0 for r in e1 for e in r["episode"]["transcript"])
    e2 = load_jsons(latest("E2") / "jobs")  # type: ignore[operator]
    calls += len(e2)
    credits += sum(r["credits"] or 0.0 for r in e2)
    e3 = load_jsons(latest("E3") / "jobs")  # type: ignore[operator]
    calls += 2 * len(e3)
    credits += sum(r["credits"] or 0.0 for r in e3)
    e3b_run = latest("E3b")
    if e3b_run:
        e3b = load_jsons(e3b_run / "jobs")
        calls += sum(len(j[v]["rounds"]) for j in e3b for v in ("diag", "blind"))
        credits += sum(j["credits"] or 0.0 for j in e3b)
    return {"sealed_records": records, "event_lines": events, "agent_calls": calls,
            "credits": credits}


def testbed() -> dict[str, Any]:
    out = {}
    for name, game in sorted(GAMES.items()):
        out[name] = {f"level {lv}": [optimal_actions(game, lv, seed) for seed in (0, 1, 2)]
                     for lv in range(1, game.levels + 1)}
    return out


def main() -> int:
    runs = {name: latest(name) for name in ("E1", "E2", "E3", "E3b", "E2-detail")}
    for name, run in runs.items():
        if run is None:
            raise SystemExit(f"no sealed {name} record")
        verified(run)
    out = {
        "note": "Exploratory and descriptive; not pre-registered.",
        "E1": e1(runs["E1"]),  # type: ignore[arg-type]
        "E2": e2(runs["E2"], runs["E2-detail"]),  # type: ignore[arg-type]
        "E3": e3(runs["E3"], runs["E3b"]),  # type: ignore[arg-type]
        "testbed_optimal_actions_eval_seeds": testbed(),
        "totals": totals(),
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / "EXPLORATORY.json"
    path.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
