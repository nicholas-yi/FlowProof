from __future__ import annotations

import pytest

from research.stats import (
    bootstrap_mean_ci,
    clopper_pearson,
    fisher_exact,
    holm,
    mcnemar_exact,
    sign_flip_p,
    wilson,
)


def test_wilson_matches_a_textbook_value():
    lo, hi = wilson(8, 10)
    assert lo == pytest.approx(0.4902, abs=1e-3) and hi == pytest.approx(0.9433, abs=1e-3)
    assert wilson(0, 0) == (0.0, 1.0)


def test_clopper_pearson_matches_known_values():
    lo, hi = clopper_pearson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.3085, abs=1e-3)
    lo, hi = clopper_pearson(5, 10)
    assert lo == pytest.approx(0.1871, abs=1e-3) and hi == pytest.approx(0.8129, abs=1e-3)


def test_fisher_exact_matches_the_tea_tasting_table():
    assert fisher_exact(3, 1, 1, 3) == pytest.approx(0.4857, abs=1e-3)
    assert fisher_exact(12, 0, 0, 12) < 1e-5


def test_mcnemar_exact():
    assert mcnemar_exact(0, 6) == pytest.approx(0.03125)
    assert mcnemar_exact(3, 3) == 1.0
    assert mcnemar_exact(0, 0) == 1.0


def test_sign_flip_is_exact():
    assert sign_flip_p([1.0] * 5) == pytest.approx(2 / 32)
    assert sign_flip_p([1.0, -1.0]) == 1.0


def test_holm_is_monotone_and_bounded():
    assert holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    assert holm([0.9, 0.8]) == [1.0, 1.0]  # 2 x 0.8 caps at 1, and order is kept


def test_bootstrap_is_seeded():
    values = [0.1, 0.4, 0.35, 0.8]
    assert bootstrap_mean_ci(values, reps=2000) == bootstrap_mean_ci(values, reps=2000)
    lo, hi = bootstrap_mean_ci(values, reps=2000)
    assert lo <= sum(values) / 4 <= hi
