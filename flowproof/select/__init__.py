"""Selection: pick one candidate from a best-of-N field without being fooled.

Policies rank a pool on frozen-test results (`by_tests`), behaviour agreement
(`behavior_clusters`), a judge asked in both presentation orders (`pairwise`) or identical
normalised patches (`exact_vote`). `choose` chains them, `headroom` scores the outcome against
ground truth and `flowproof.select.simulate` models when fan-out pays. See docs/SELECTION.md.
"""

from __future__ import annotations

from flowproof.select.chain import FALLBACKS, POLICIES, choose, rank
from flowproof.select.judging import PairwiseResult, pairwise
from flowproof.select.ranking import behavior_clusters, by_tests, exact_vote, normalise_patch
from flowproof.select.report import Headroom, HeadroomRow, headroom
from flowproof.select.types import (
    Candidate,
    ChangeLike,
    GradeLike,
    Judge,
    Preference,
    Ranking,
    Selection,
    TamperLike,
)

__all__ = [
    "FALLBACKS",
    "POLICIES",
    "Candidate",
    "ChangeLike",
    "GradeLike",
    "Headroom",
    "HeadroomRow",
    "Judge",
    "PairwiseResult",
    "Preference",
    "Ranking",
    "Selection",
    "TamperLike",
    "behavior_clusters",
    "by_tests",
    "choose",
    "exact_vote",
    "headroom",
    "normalise_patch",
    "pairwise",
    "rank",
]
