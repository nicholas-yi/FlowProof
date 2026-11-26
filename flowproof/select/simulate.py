"""Seeded Monte Carlo of best-of-N selection, used to explain when fan-out pays.

The model, per task:

* Difficulty `d = task_difficulty_sd * z` with `z ~ N(0, 1)`. A candidate solves the task with
  probability `p = logistic(ability - d)`; `ability` is calibrated so the mean of `p` over
  tasks equals `p_solve_mean`.
* Correlated errors: each candidate copies one shared per-task draw with probability
  `sqrt(correlation)` and otherwise draws on its own, so two candidates' correctness on the
  same task has correlation `correlation`. Copiers write the same patch. Independent wrong
  candidates write unique patches; independent correct ones write the canonical correct patch
  with probability `convergence` (default 0), else a unique correct patch.
* Tests are drawn once per distinct patch: a correct patch passes with `test_sensitivity`, a
  wrong one fails with `test_specificity`.
* Each judge call (one candidate shown first) answers "first" with probability
  `judge_position_bias`; otherwise it reads content and names the correct one of a mixed pair
  with probability `judge_accuracy` (else the wrong one), and guesses on an equal pair.

Policies mirror `choose`: keep the best tier, then break ties uniformly. Each policy's score on
a task is the exact expected success of that uniform tie-break, so equal pools give equal
scores. Results use common random numbers: every policy and every N sees the same candidates
(N uses the first N), and a sweep changes only thresholds, not draws.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from typing import Any

POLICIES = ("single", "random", "tests", "pairwise", "tests+pairwise", "exact-vote", "oracle")
DEFAULT_NS = (1, 2, 4, 8, 16)
SWEEPABLE = (
    "p_solve_mean",
    "task_difficulty_sd",
    "correlation",
    "judge_accuracy",
    "judge_position_bias",
    "test_sensitivity",
    "test_specificity",
    "convergence",
)
STANDARD_SWEEPS: tuple[tuple[str, tuple[float, ...]], ...] = (
    ("correlation", (0.0, 0.25, 0.5, 0.75, 1.0)),
    ("judge_accuracy", (0.5, 0.6, 0.7, 0.8, 0.9, 1.0)),
    ("judge_position_bias", (0.0, 0.25, 0.5, 0.75, 1.0)),
    ("test_sensitivity", (0.5, 0.7, 0.9, 1.0)),
    ("test_specificity", (0.5, 0.7, 0.9, 1.0)),
)

_CORRECT_PATCH = -1
_SHARED_WRONG_PATCH = -2
_QUADRATURE = 241


def _logistic(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def calibrate_ability(p_solve_mean: float, task_difficulty_sd: float) -> float:
    """The ability whose mean solve probability over N(0, sd) difficulties is `p_solve_mean`."""
    if not 0.0 < p_solve_mean < 1.0:
        raise ValueError("p_solve_mean must be strictly between 0 and 1")
    if task_difficulty_sd == 0:
        return math.log(p_solve_mean / (1.0 - p_solve_mean))
    step = 12.0 / _QUADRATURE
    nodes = [-6.0 + step * (k + 0.5) for k in range(_QUADRATURE)]
    raw = [math.exp(-0.5 * z * z) for z in nodes]
    total = sum(raw)
    weights = [w / total for w in raw]
    low, high = -60.0, 60.0
    for _ in range(100):
        mid = 0.5 * (low + high)
        mean = sum(
            w * _logistic(mid - task_difficulty_sd * z) for w, z in zip(weights, nodes, strict=True)
        )
        if mean < p_solve_mean:
            low = mid
        else:
            high = mid
    return 0.5 * (low + high)


def _ns(n_candidates: int | Sequence[int]) -> list[int]:
    raw = list(range(1, n_candidates + 1)) if isinstance(n_candidates, int) else n_candidates
    ns = sorted({int(n) for n in raw})
    if not ns or ns[0] < 1:
        raise ValueError("n_candidates must be a positive int or a non-empty sequence of them")
    return ns


def _policies(policies: Sequence[str]) -> list[str]:
    if isinstance(policies, str):
        raise ValueError("policies must be a sequence of names, not a string")
    chosen = list(dict.fromkeys(policies))
    unknown = [p for p in chosen if p not in POLICIES]
    if unknown or not chosen:
        raise ValueError(f"unknown or missing policies {unknown}; expected some of {POLICIES}")
    return chosen


def _draw_task(
    rng: random.Random, n_max: int, ability: float, settings: dict[str, Any]
) -> tuple[list[bool], list[int], list[bool], list[int]]:
    """One task's candidates: correctness, patch keys, test verdicts and judge picks."""
    p = _logistic(ability - settings["task_difficulty_sd"] * rng.gauss(0.0, 1.0))
    shared = rng.random() < p
    copy_p = math.sqrt(settings["correlation"])
    convergence = settings["convergence"]
    correct: list[bool] = []
    patch: list[int] = []
    own_test: list[float] = []
    for i in range(n_max):
        u_copy, u_solve, u_converge, u_test = rng.random(), rng.random(), rng.random(), rng.random()
        if u_copy < copy_p:
            ok = shared
            key = _CORRECT_PATCH if ok else _SHARED_WRONG_PATCH
        else:
            ok = u_solve < p
            key = _CORRECT_PATCH if ok and u_converge < convergence else i
        correct.append(ok)
        patch.append(key)
        own_test.append(u_test)
    u_correct_test, u_wrong_test = rng.random(), rng.random()
    pass_if_correct = settings["test_sensitivity"]
    pass_if_wrong = 1.0 - settings["test_specificity"]
    passed: list[bool] = []
    for i in range(n_max):
        if patch[i] == _CORRECT_PATCH:
            u = u_correct_test
        elif patch[i] == _SHARED_WRONG_PATCH:
            u = u_wrong_test
        else:
            u = own_test[i]
        passed.append(u < (pass_if_correct if correct[i] else pass_if_wrong))
    bias = settings["judge_position_bias"]
    accuracy = settings["judge_accuracy"]
    picks = [0] * (n_max * n_max)
    for i in range(n_max):
        for j in range(n_max):
            if i == j:
                continue
            u_position, u_content = rng.random(), rng.random()
            if u_position < bias:
                winner = i
            elif correct[i] != correct[j]:
                better, worse = (i, j) if correct[i] else (j, i)
                winner = better if u_content < accuracy else worse
            else:
                winner = i if u_content < 0.5 else j
            picks[i * n_max + j] = winner
    return correct, patch, passed, picks


def _expected(pool: Sequence[int], correct: Sequence[bool]) -> float:
    return sum(1 for i in pool if correct[i]) / len(pool)


def _copeland_best(pool: Sequence[int], picks: Sequence[int], n_max: int) -> list[int]:
    """The top Copeland tier when a win needs both presentation orders to agree."""
    if len(pool) < 2:
        return list(pool)
    score = dict.fromkeys(pool, 0)
    for x, i in enumerate(pool):
        for j in pool[x + 1 :]:
            winner = picks[i * n_max + j]
            if winner == picks[j * n_max + i]:
                loser = j if winner == i else i
                score[winner] += 1
                score[loser] -= 1
    best = max(score.values())
    return [i for i in pool if score[i] == best]


def _score_policy(
    policy: str,
    pool: list[int],
    passers: list[int],
    task: tuple[list[bool], list[int], list[bool], list[int]],
    n_max: int,
) -> tuple[float, int]:
    correct, patch, _, picks = task
    if policy == "single":
        return (1.0 if correct[0] else 0.0), 0
    if policy == "random":
        return _expected(pool, correct), 0
    if policy == "oracle":
        return (1.0 if any(correct[i] for i in pool) else 0.0), 0
    if policy == "tests":
        return _expected(passers, correct), 0
    if policy == "pairwise":
        return _expected(_copeland_best(pool, picks, n_max), correct), len(pool) * (len(pool) - 1)
    if policy == "tests+pairwise":
        calls = len(passers) * (len(passers) - 1)
        return _expected(_copeland_best(passers, picks, n_max), correct), calls
    counts: dict[int, int] = {}
    for i in pool:
        counts[patch[i]] = counts.get(patch[i], 0) + 1
    top = max(counts.values())
    return _expected([i for i in pool if counts[patch[i]] == top], correct), 0


def simulate(
    n_candidates: int | Sequence[int] = DEFAULT_NS,
    *,
    tasks: int = 1000,
    p_solve_mean: float = 0.3,
    task_difficulty_sd: float = 1.0,
    correlation: float = 0.3,
    judge_accuracy: float = 0.7,
    judge_position_bias: float = 0.2,
    test_sensitivity: float = 0.9,
    test_specificity: float = 0.8,
    seed: int = 0,
    policies: Sequence[str] = POLICIES,
    convergence: float = 0.0,
) -> dict[str, Any]:
    """Success rate and expected judge calls per policy and per N, deterministic for a seed.

    `n_candidates` is a maximum N (meaning 1..N) or an explicit sequence of Ns. The result is
    a plain dictionary: `success[policy][k]`, `stderr[policy][k]` and `judge_calls[policy][k]`
    are aligned with `ns[k]`.
    """
    ns = _ns(n_candidates)
    chosen = _policies(policies)
    if tasks < 1:
        raise ValueError("tasks must be at least 1")
    if task_difficulty_sd < 0:
        raise ValueError("task_difficulty_sd must be non-negative")
    settings: dict[str, Any] = {
        "task_difficulty_sd": task_difficulty_sd,
        "correlation": correlation,
        "judge_accuracy": judge_accuracy,
        "judge_position_bias": judge_position_bias,
        "test_sensitivity": test_sensitivity,
        "test_specificity": test_specificity,
        "convergence": convergence,
    }
    for name, value in settings.items():
        if name != "task_difficulty_sd" and not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be between 0 and 1")
    ability = calibrate_ability(p_solve_mean, task_difficulty_sd)
    n_max = ns[-1]
    rng = random.Random(seed)
    sums = {p: [0.0] * len(ns) for p in chosen}
    squares = {p: [0.0] * len(ns) for p in chosen}
    calls = {p: [0] * len(ns) for p in chosen}
    for _ in range(tasks):
        task = _draw_task(rng, n_max, ability, settings)
        passed = task[2]
        for k, n in enumerate(ns):
            pool = list(range(n))
            passers = [i for i in pool if passed[i]] or pool
            for policy in chosen:
                value, used = _score_policy(policy, pool, passers, task, n_max)
                sums[policy][k] += value
                squares[policy][k] += value * value
                calls[policy][k] += used
    success = {p: [s / tasks for s in sums[p]] for p in chosen}
    stderr = {
        p: [
            math.sqrt(max(squares[p][k] / tasks - success[p][k] ** 2, 0.0) / tasks)
            for k in range(len(ns))
        ]
        for p in chosen
    }
    return {
        "model": "flowproof.select.simulate/1",
        "params": {
            "n_candidates": ns,
            "tasks": tasks,
            "p_solve_mean": p_solve_mean,
            "seed": seed,
            "policies": chosen,
            **settings,
        },
        "ability": ability,
        "ns": ns,
        "policies": chosen,
        "success": success,
        "stderr": stderr,
        "judge_calls": {p: [c / tasks for c in calls[p]] for p in chosen},
    }


def sweep(parameter: str, values: Sequence[float], **settings: Any) -> dict[str, Any]:
    """Run `simulate` once per value of one parameter, holding the others at `settings`.

    `success[policy][v][k]` is the success rate at `values[v]` and `ns[k]`.
    """
    if parameter not in SWEEPABLE:
        raise ValueError(f"cannot sweep {parameter!r}; expected one of {SWEEPABLE}")
    if parameter in settings:
        raise ValueError(f"{parameter!r} is swept; do not also fix it")
    if not values:
        raise ValueError("sweep needs at least one value")
    runs: list[dict[str, Any]] = []
    for value in values:
        params: dict[str, Any] = {**settings, parameter: value}
        runs.append(simulate(**params))
    policies: list[str] = runs[0]["policies"]
    return {
        "parameter": parameter,
        "values": list(values),
        "ns": runs[0]["ns"],
        "policies": policies,
        "success": {p: [run["success"][p] for run in runs] for p in policies},
        "judge_calls": {p: [run["judge_calls"][p] for run in runs] for p in policies},
        "runs": runs,
    }


def standard_sweeps(**settings: Any) -> dict[str, dict[str, Any]]:
    """The sweeps in `STANDARD_SWEEPS`, by default over N in (1, 2, 4, 8) and 400 tasks."""
    base: dict[str, Any] = {"tasks": 400, "n_candidates": (1, 2, 4, 8), **settings}
    return {
        name: sweep(name, values, **base) for name, values in STANDARD_SWEEPS if name not in base
    }
