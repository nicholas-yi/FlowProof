from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flowproof.outcomes import Outcome
from flowproof.select import (
    POLICIES,
    Candidate,
    Preference,
    Selection,
    behavior_clusters,
    by_tests,
    choose,
    exact_vote,
    headroom,
    normalise_patch,
    pairwise,
    rank,
)


@dataclass(frozen=True)
class FakeTamper:
    tampered: bool = False


@dataclass(frozen=True)
class FakeGrade:
    true_passed: bool | None
    tests_passed: int | None = None
    tests_failed: int | None = None
    tamper: FakeTamper = field(default_factory=FakeTamper)
    per_test: Mapping[str, bool] | None = None


@dataclass(frozen=True)
class FakeChange:
    patch: str = ""

    @property
    def empty(self) -> bool:
        return not self.patch.strip()


def cand(
    cid: str,
    passed: bool | None = True,
    *,
    count: int | None = None,
    failed: int | None = None,
    tampered: bool = False,
    per_test: Mapping[str, bool] | None = None,
    patch: str | None = "",
    outcome: Outcome | None = None,
    graded: bool = True,
) -> Candidate:
    grade = FakeGrade(passed, count, failed, FakeTamper(tampered), per_test) if graded else None
    change = FakeChange(patch) if patch is not None else None
    return Candidate(cid, change, grade, outcome or Outcome.ok())


def table_judge(table: Mapping[tuple[str, str], Preference]):
    """A judge answering from a table keyed by (shown first, shown second); records calls."""
    calls: list[tuple[str, str]] = []

    def judge(a: Candidate, b: Candidate) -> Preference:
        calls.append((a.id, b.id))
        return table.get((a.id, b.id), Preference.TIE)

    return judge, calls


def beats_judge(*wins: tuple[str, str]):
    """A position-consistent judge naming each (winner, loser) pair's winner in both orders."""
    table: dict[tuple[str, str], Preference] = {}
    for winner, loser in wins:
        table[(winner, loser)] = Preference.FIRST
        table[(loser, winner)] = Preference.SECOND
    return table_judge(table)


def always_first(a: Candidate, b: Candidate) -> Preference:
    return Preference.FIRST


def never_called(a: Candidate, b: Candidate) -> Preference:
    raise AssertionError("the judge should not have been asked")


# --- by_tests -------------------------------------------------------------------------------


def test_tests_rank_untampered_passes_then_failures_then_tampered_with_unranked_last():
    field_ = [
        cand("a", True, count=5),
        cand("b", True, count=3),
        cand("c", False, count=4),
        cand("d", True, count=9, tampered=True),
        cand("e", False, tampered=True),
        cand("f", True, outcome=Outcome.invalid("timeout")),
        cand("g", graded=False),
        cand("h", None),
    ]
    assert by_tests(field_) == [["a"], ["b"], ["c"], ["d"], ["e"], ["f", "g", "h"]]
    ranking = rank("tests", field_)
    assert ranking.unranked == ("f", "g", "h")
    assert ranking.detail["unranked_reasons"] == {
        "f": "outcome-invalid",
        "g": "no-grade",
        "h": "grade-unknown",
    }
    assert ranking.flagged["tampered"] == ("d", "e")


def test_test_counts_are_compared_only_when_both_are_known():
    field_ = [
        cand("a", True, count=5),
        cand("b", True),
        cand("c", True, count=5),
        cand("d", True, count=2),
    ]
    # "b" has no count, so nothing beats it: it shares the top tier.
    assert by_tests(field_) == [["a", "b", "c"], ["d"]]


def test_a_field_without_any_test_evidence_is_one_unranked_tier():
    field_ = [cand("a", None), cand("b", graded=False)]
    assert by_tests(field_) == [["a", "b"]]
    selection = choose(field_, ["tests"], fallback="first")
    assert (selection.chosen, selection.basis, selection.fallback) == ("a", (), "first")


def test_unranked_candidates_are_dropped_only_when_someone_has_evidence():
    selection = choose([cand("a", False), cand("b", graded=False)], ["tests"])
    assert selection.chosen == "a"
    assert selection.basis == ("tests",)
    assert selection.trace["steps"][0]["dropped_unranked"] == ["b"]
    assert selection.trace["steps"][0]["unranked"] == ["b"]


# --- incumbent retention ----------------------------------------------------------------------


def test_an_equal_challenger_does_not_replace_the_incumbent_whatever_the_fallback():
    field_ = [cand("inc", True, count=3), cand("a", True, count=3)]
    for fallback in ("first", "abstain", "incumbent"):
        selection = choose(field_, ["tests"], incumbent="inc", fallback=fallback)
        assert selection.chosen == "inc"
        assert selection.basis == ("incumbent-retained",)
        assert selection.fallback == "incumbent"
        assert selection.trace["final_pool"] == ["a", "inc"]


def test_a_strictly_better_challenger_replaces_the_incumbent():
    field_ = [cand("inc", True, count=3), cand("a", True, count=4)]
    selection = choose(field_, ["tests"], incumbent="inc")
    assert (selection.chosen, selection.basis, selection.fallback) == ("a", ("tests",), None)
    assert selection.trace["incumbent_eliminated_by"]["policy"] == "tests"
    passing = choose([cand("inc", False), cand("a", True)], ["tests"], incumbent="inc")
    assert passing.chosen == "a"


def test_an_incumbent_without_evidence_is_not_displaced_by_missing_evidence():
    field_ = [cand("inc", graded=False), cand("a", True)]
    selection = choose(field_, ["tests"], incumbent="inc")
    assert selection.chosen == "inc"
    assert selection.basis == ("incumbent-retained",)
    assert selection.trace["steps"][0]["incumbent"] == "kept-without-evidence"
    # ...but a later policy that does have evidence about it may still remove it.
    judge, _ = beats_judge(("a", "inc"))
    later = choose(field_, ["tests", "pairwise"], judge=judge, incumbent="inc")
    assert (later.chosen, later.basis) == ("a", ("pairwise",))


def test_a_decisively_best_incumbent_records_both_the_policy_and_the_retention():
    selection = choose([cand("inc", True), cand("a", False)], ["tests"], incumbent="inc")
    assert (selection.chosen, selection.basis) == ("inc", ("tests", "incumbent-retained"))


def test_a_pairwise_draw_with_the_incumbent_keeps_it():
    field_ = [cand("inc"), cand("a")]
    selection = choose(field_, ["pairwise"], judge=always_first, incumbent="inc", fallback="first")
    assert (selection.chosen, selection.fallback) == ("inc", "incumbent")


# --- pairwise -----------------------------------------------------------------------------------


def test_pairwise_asks_every_pair_in_both_orders_and_wins_need_both():
    judge, calls = beats_judge(("a", "b"), ("a", "c"), ("b", "c"))
    result = pairwise([cand("c"), cand("a"), cand("b")], judge)
    assert sorted(calls) == sorted((x, y) for x in "abc" for y in "abc" if x != y)
    assert result.calls == 6
    assert result.wins["a"]["b"] == 1
    assert result.wins["b"]["a"] == 0
    assert dict(result.scores) == {"a": 2, "b": 0, "c": -2}
    assert result.ranking() == [["a"], ["b"], ["c"]]
    assert result.consistency == 1.0
    assert result.first_position_rate == 0.5
    assert result.cycles == ()


def test_an_always_first_judge_wins_nothing_and_is_never_consistent():
    result = pairwise([cand("a"), cand("b"), cand("c")], always_first)
    assert all(v == 0 for row in result.wins.values() for v in row.values())
    assert result.inconsistent == result.draws == (("a", "b"), ("a", "c"), ("b", "c"))
    assert result.consistency == 0.0
    assert (result.first_position_rate, result.second_position_rate) == (1.0, 0.0)
    assert result.tiers == (("a", "b", "c"),)
    selection = choose([cand("a"), cand("b")], ["pairwise"], judge=always_first)
    assert (selection.chosen, selection.fallback) == (None, "abstain")


def test_a_three_way_cycle_is_reported():
    judge, _ = beats_judge(("a", "b"), ("b", "c"), ("c", "a"))
    field_ = [cand("a"), cand("b"), cand("c")]
    result = pairwise(field_, judge)
    assert result.cycles == (("a", "b", "c"),)
    assert dict(result.scores) == {"a": 0, "b": 0, "c": 0}
    assert result.tiers == (("a", "b", "c"),)
    assert rank("pairwise", field_, judge=judge).flagged["in-cycle"] == ("a", "b", "c")


def test_tie_in_both_orders_is_a_consistent_draw_but_a_one_sided_tie_is_not():
    judge, _ = table_judge({("a", "c"): Preference.FIRST})
    result = pairwise([cand("a"), cand("b"), cand("c")], judge)
    # (a, b) and (b, c) are TIE twice; (a, c) is FIRST then TIE.
    assert result.draws == (("a", "b"), ("a", "c"), ("b", "c"))
    assert result.inconsistent == (("a", "c"),)
    assert result.consistency == pytest.approx(2 / 3)
    assert result.first_position_rate == 1.0


def test_an_invalid_answer_in_either_order_leaves_the_pair_unjudged():
    judge, _ = table_judge(
        {
            ("a", "b"): Preference.FIRST,
            ("b", "a"): Preference.SECOND,
            ("a", "c"): Preference.FIRST,
            ("c", "a"): Preference.INVALID,
            ("b", "c"): Preference.INVALID,
            ("c", "b"): Preference.SECOND,
        }
    )
    field_ = [cand("a"), cand("b"), cand("c")]
    result = pairwise(field_, judge)
    assert result.unjudged == (("a", "c"), ("b", "c"))
    assert result.unranked == ("c",)
    assert result.ranking() == [["a"], ["b"], ["c"]]
    assert result.consistency == 1.0
    assert result.wins["a"]["c"] == 0
    # "c" has no judged pair, so pairwise cannot remove it when it is the incumbent.
    assert choose(field_, ["pairwise"], judge=judge, incumbent="c").chosen == "c"


def test_choose_stops_once_one_candidate_remains():
    field_ = [cand("a", True), cand("b", False)]
    selection = choose(field_, ["tests", "pairwise"], judge=never_called)
    assert (selection.chosen, selection.basis) == ("a", ("tests",))
    assert selection.trace["skipped"] == ["pairwise"]
    assert selection.trace["judge_calls"] == 0


def test_tests_then_pairwise_judges_only_the_survivors():
    judge, calls = beats_judge(("b", "a"))
    field_ = [cand("a", True), cand("b", True), cand("c", False)]
    selection = choose(field_, ["tests", "pairwise"], judge=judge)
    assert (selection.chosen, selection.basis) == ("b", ("tests", "pairwise"))
    assert sorted(calls) == [("a", "b"), ("b", "a")]
    assert selection.trace["judge_calls"] == 2


# --- behaviour clusters -------------------------------------------------------------------------


def test_behavior_clusters_use_per_test_vectors_when_every_candidate_has_them():
    v1 = {"t1": True, "t2": False}
    v2 = {"t1": False, "t2": True}
    field_ = [
        cand("d", False, count=1, failed=1, per_test=v2),
        cand("a", False, count=1, failed=1, per_test=v1),
        cand("c", False, count=1, failed=1, per_test=v1),
        cand("x", graded=False),
    ]
    assert behavior_clusters(field_) == [["a", "c"], ["d"], ["x"]]
    assert rank("behavior", field_).detail["mode"] == "per-test"


def test_behavior_clusters_fall_back_to_the_summary_triple():
    field_ = [
        cand("a", False, count=1, failed=1, per_test={"t1": True, "t2": False}),
        cand("b", False, count=1, failed=1),
        cand("c", True, count=2, failed=0, per_test={"t1": True, "t2": True}),
    ]
    assert behavior_clusters(field_) == [["a", "b"], ["c"]]
    assert rank("behavior", field_).detail["mode"] == "summary"


def test_equal_sized_clusters_are_a_tie_not_a_decision():
    field_ = [
        cand("a", True, count=2),
        cand("b", False),
        cand("c", True, count=2),
        cand("d", False),
    ]
    assert behavior_clusters(field_) == [["a", "c"], ["b", "d"]]
    selection = choose(field_, ["behavior"])
    assert (selection.chosen, selection.basis, selection.fallback) == (None, (), "abstain")


# --- exact vote ---------------------------------------------------------------------------------

BASE = (
    "diff --git a/x.py b/x.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/x.py\n"
    "+++ b/x.py\n"
    "@@ -1,3 +1,4 @@ def f():\n"
    "     x = 1\n"
    "+    y = 2\n"
    " \n"
    "     return x\n"
)
VARIANT = (
    "diff --git a/x.py b/x.py\r\n"
    "index 9999999..8888888 100644\r\n"
    "--- a/x.py\r\n"
    "+++ b/x.py\r\n"
    "@@ -10,3 +10,5 @@ def f():\r\n"
    "     x = 1   \r\n"
    "+    y = 2\r\n"
    "+\r\n"
    "\r\n"
    "     return x\r\n"
)


def test_patch_normalisation_ignores_index_lines_offsets_whitespace_and_blank_lines():
    assert normalise_patch(BASE) == normalise_patch(VARIANT)
    assert "@@ @@ def f():" in normalise_patch(BASE)
    assert normalise_patch(BASE) != normalise_patch(BASE.replace("y = 2", "y = 3"))


def test_exact_vote_groups_identical_patches_and_keeps_empty_ones_apart():
    field_ = [
        cand("a", patch=BASE),
        cand("b", patch=VARIANT),
        cand("c", patch=BASE.replace("y = 2", "y = 3")),
        cand("d", patch=""),
        cand("e", patch="\n"),
        cand("f", patch=None),
    ]
    assert exact_vote(field_) == [["a", "b"], ["d", "e"], ["c"], ["f"]]
    ranking = rank("exact", field_)
    assert ranking.flagged["empty-patch"] == ("d", "e")
    assert ranking.tiers == (("a", "b", "d", "e"), ("c",))
    assert ranking.unranked == ("f",)


# --- choose -------------------------------------------------------------------------------------


def test_choose_rejects_bad_arguments():
    field_ = [cand("a"), cand("b")]
    with pytest.raises(ValueError, match="unknown selection policies"):
        choose(field_, ["tests", "vibes"])
    with pytest.raises(ValueError, match="needs a judge"):
        choose(field_, ["pairwise"])
    with pytest.raises(ValueError, match="unknown fallback"):
        choose(field_, ["tests"], fallback="coin")
    with pytest.raises(ValueError, match="incumbent"):
        choose(field_, ["tests"], incumbent="zz")
    with pytest.raises(ValueError, match="duplicate"):
        choose([cand("a"), cand("a")], ["tests"])
    with pytest.raises(ValueError, match="sequence"):
        choose(field_, "tests")


def test_every_fallback_on_a_final_tie():
    field_ = [cand("b", True), cand("a", True), cand("c", False)]
    first = choose(field_, ["tests"], fallback="first")
    assert (first.chosen, first.basis, first.fallback) == ("a", ("tests",), "first")
    abstain = choose(field_, ["tests"], fallback="abstain")
    assert (abstain.chosen, abstain.fallback) == (None, "abstain")
    assert abstain.trace["final_pool"] == ["a", "b"]
    incumbent = choose(field_, ["tests"], incumbent="b", fallback="incumbent")
    assert (incumbent.chosen, incumbent.basis, incumbent.fallback) == (
        "b",
        ("tests", "incumbent-retained"),
        "incumbent",
    )
    # With no incumbent, or once evidence has removed it, "incumbent" abstains.
    no_incumbent = choose(field_, ["tests"], fallback="incumbent")
    assert (no_incumbent.chosen, no_incumbent.fallback) == (None, "abstain")
    eliminated = choose(field_, ["tests"], incumbent="c", fallback="incumbent")
    assert (eliminated.chosen, eliminated.fallback) == (None, "abstain")
    assert eliminated.trace["incumbent_eliminated_by"]["best"] == ["a", "b"]


def test_an_empty_chain_goes_straight_to_the_fallback_and_an_empty_field_chooses_nothing():
    assert choose([cand("b"), cand("a")], [], fallback="first").chosen == "a"
    assert choose([cand("only")], []).chosen == "only"
    empty = choose([], ["tests"])
    assert (empty.chosen, empty.fallback) == (None, None)


def test_a_full_chain_narrows_step_by_step():
    judge, _ = beats_judge(("b", "c"))
    field_ = [
        cand("a", False, patch=BASE),
        cand("b", True, count=4, per_test={"t": True}, patch=BASE),
        cand("c", True, count=4, per_test={"t": True}, patch=VARIANT),
        cand("d", True, count=4, per_test={"t": True}, patch=""),
    ]
    selection = choose(field_, ["tests", "exact", "pairwise"], judge=judge)
    # tests keeps b, c, d; exact keeps the identical b and c; the judge prefers b.
    assert (selection.chosen, selection.basis) == ("b", ("tests", "exact", "pairwise"))
    kept = [step["kept"] for step in selection.trace["steps"]]
    assert kept == [["b", "c", "d"], ["b", "c"], ["b"]]


# --- headroom -----------------------------------------------------------------------------------


def test_headroom_arithmetic():
    f1 = [cand("1a"), cand("1b"), cand("1c")]
    f2 = [cand("2a"), cand("2b")]
    f3 = [cand("3a"), cand("3b"), cand("3c"), cand("3d")]
    right = {"1a", "3a", "3b"}
    report = headroom(
        [
            (f1, Selection("1a", ("tests",), None)),
            (f2, Selection("2b", ("tests",), None)),
            (f3, Selection(None, (), "abstain")),
        ],
        lambda c: c.id in right,
    )
    assert report.n == 3
    assert report.pass_at_1 == pytest.approx((1 / 3 + 0 + 1 / 2) / 3)
    assert report.random_choice == report.pass_at_1
    assert report.oracle == pytest.approx(2 / 3)
    assert report.selected == pytest.approx(1 / 3)
    assert report.regret == pytest.approx(1 / 3)
    assert report.abstained == 1
    rows = [(r.size, r.correct, r.oracle, r.selected) for r in report.rows]
    assert rows == [(3, 1, 1.0, 1.0), (2, 0, 0.0, 0.0), (4, 2, 1.0, 0.0)]


def test_headroom_rejects_nothing_to_score_and_foreign_choices():
    with pytest.raises(ValueError):
        headroom([], lambda c: True)
    with pytest.raises(ValueError):
        headroom([([cand("a")], Selection("b", (), None))], lambda c: True)


# --- properties ---------------------------------------------------------------------------------

grades = st.one_of(
    st.none(),
    st.builds(
        FakeGrade,
        true_passed=st.sampled_from([True, False, None]),
        tests_passed=st.one_of(st.none(), st.integers(0, 3)),
        tests_failed=st.one_of(st.none(), st.integers(0, 3)),
        tamper=st.builds(FakeTamper, st.booleans()),
        per_test=st.one_of(
            st.none(), st.fixed_dictionaries({"t1": st.booleans(), "t2": st.booleans()})
        ),
    ),
)
changes = st.one_of(st.none(), st.builds(FakeChange, st.sampled_from(["", BASE, VARIANT, "+z"])))
outcomes = st.sampled_from([Outcome.ok(), Outcome.ok(), Outcome.invalid("timeout")])


def content_judge(a: Candidate, b: Candidate) -> Preference:
    left = len(a.change.patch) if a.change else -1
    right = len(b.change.patch) if b.change else -1
    if left == right:
        return Preference.FIRST
    return Preference.FIRST if left > right else Preference.SECOND


@given(st.lists(st.tuples(grades, changes, outcomes), min_size=1, max_size=6), st.data())
def test_selection_does_not_depend_on_input_order(parts, data):
    field_ = [Candidate(f"c{i}", ch, gr, out) for i, (gr, ch, out) in enumerate(parts)]
    chain = data.draw(st.lists(st.sampled_from(POLICIES), max_size=4))
    incumbent = data.draw(st.one_of(st.none(), st.sampled_from([c.id for c in field_])))
    fallback = data.draw(st.sampled_from(["incumbent", "first", "abstain"]))
    shuffled = data.draw(st.permutations(field_))

    def run(pool: list[Candidate]) -> Selection:
        return choose(pool, chain, judge=content_judge, incumbent=incumbent, fallback=fallback)

    assert run(shuffled) == run(field_)
    assert by_tests(shuffled) == by_tests(field_)
    assert behavior_clusters(shuffled) == behavior_clusters(field_)
    assert exact_vote(shuffled) == exact_vote(field_)
