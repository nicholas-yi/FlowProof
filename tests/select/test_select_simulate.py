from __future__ import annotations

import math
import time

import pytest

from flowproof.select.simulate import (
    POLICIES,
    calibrate_ability,
    simulate,
    standard_sweeps,
    sweep,
)

QUICK = {"n_candidates": (1, 2, 4, 8), "tasks": 400}


def test_the_simulation_is_deterministic_for_a_seed():
    assert simulate(seed=7, **QUICK) == simulate(seed=7, **QUICK)
    assert simulate(seed=7, **QUICK)["success"] != simulate(seed=8, **QUICK)["success"]


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"correlation": 0.9, "test_specificity": 0.3},
        {"judge_accuracy": 0.2, "judge_position_bias": 0.6, "p_solve_mean": 0.7},
        {"convergence": 1.0, "correlation": 0.0, "task_difficulty_sd": 0.0},
    ],
)
def test_no_policy_beats_the_oracle(settings):
    result = simulate(**QUICK, **settings)
    oracle = result["success"]["oracle"]
    for policy in POLICIES:
        for k, value in enumerate(result["success"][policy]):
            assert value <= oracle[k] + 1e-12, (policy, result["ns"][k])


def test_perfect_tests_select_as_well_as_the_oracle():
    result = simulate(**QUICK, test_sensitivity=1.0, test_specificity=1.0)
    assert result["success"]["tests"] == pytest.approx(result["success"]["oracle"])


def test_a_coin_flip_judge_that_always_prefers_the_first_shown_is_no_better_than_random():
    result = simulate(**QUICK, judge_accuracy=0.5, judge_position_bias=1.0)
    assert result["success"]["pairwise"] == pytest.approx(result["success"]["random"])
    assert result["success"]["tests+pairwise"] == pytest.approx(result["success"]["tests"])
    assert result["judge_calls"]["pairwise"] == [n * (n - 1) for n in result["ns"]]


def test_a_perfect_unbiased_judge_selects_as_well_as_the_oracle():
    result = simulate(**QUICK, judge_accuracy=1.0, judge_position_bias=0.0)
    assert result["success"]["pairwise"] == pytest.approx(result["success"]["oracle"])


def test_exact_vote_helps_only_when_correct_answers_converge():
    independent = simulate(**QUICK, correlation=0.0, convergence=0.0)
    assert independent["success"]["exact-vote"] == pytest.approx(independent["success"]["random"])
    converging = simulate(**QUICK, correlation=0.0, convergence=1.0)
    assert converging["success"]["exact-vote"][-1] > converging["success"]["random"][-1] + 0.1


def test_the_mean_solve_rate_is_calibrated():
    assert calibrate_ability(0.5, 0.0) == pytest.approx(0.0)
    assert calibrate_ability(0.3, 1.5) < math.log(0.3 / 0.7)
    result = simulate(1, tasks=20_000, p_solve_mean=0.3, task_difficulty_sd=1.5)
    assert result["success"]["single"][0] == pytest.approx(0.3, abs=0.015)


def test_sweep_varies_one_parameter_and_keeps_the_rest():
    result = sweep("judge_accuracy", [0.5, 1.0], seed=1, **QUICK)
    assert result["values"] == [0.5, 1.0]
    assert len(result["success"]["pairwise"]) == 2
    low, high = result["success"]["pairwise"]
    assert high[-1] > low[-1]
    # Common random numbers: the judge does not touch the candidates or the tests.
    assert result["success"]["tests"][0] == result["success"]["tests"][1]
    with pytest.raises(ValueError):
        sweep("seed", [1, 2])
    with pytest.raises(ValueError):
        sweep("correlation", [0.1], correlation=0.2)


def test_bad_settings_are_rejected():
    with pytest.raises(ValueError):
        simulate(policies=["tests", "vibes"])
    with pytest.raises(ValueError):
        simulate(correlation=1.5)
    with pytest.raises(ValueError):
        simulate(p_solve_mean=1.0)
    with pytest.raises(ValueError):
        simulate(n_candidates=[0, 2])
    with pytest.raises(ValueError):
        simulate(tasks=0)


def test_the_standard_sweeps_are_fast():
    started = time.perf_counter()
    sweeps = standard_sweeps()
    elapsed = time.perf_counter() - started
    assert set(sweeps) == {
        "correlation",
        "judge_accuracy",
        "judge_position_bias",
        "test_sensitivity",
        "test_specificity",
    }
    assert elapsed < 5.0
