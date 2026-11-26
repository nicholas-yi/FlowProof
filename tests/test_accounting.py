from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flowproof.accounting import AccountingUnknown, Amount, Ledger, LimitReached, Usage

values = st.one_of(st.none(), st.floats(min_value=0, max_value=1e6, allow_nan=False))


@given(st.lists(values, min_size=1, max_size=20))
def test_a_sum_is_known_only_when_every_part_is(parts):
    total = Amount("credits", 0.0)
    for value in parts:
        total = total + Amount("credits", value)
    if any(v is None for v in parts):
        assert total.value is None
    else:
        assert total.value == pytest.approx(sum(parts))


def test_units_never_mix():
    with pytest.raises(ValueError):
        _ = Amount("credits", 1.0) + Amount("usd", 1.0)


def test_a_unit_one_side_never_measured_is_unknown_in_the_total():
    measured = Usage.of(credits=2.0)
    unmeasured = Usage({}, calls=1)
    assert (measured + unmeasured).get("credits").value is None
    # ...unless that side made no calls at all.
    assert (measured + Usage.none()).get("credits").value == 2.0


def test_missing_units_read_as_unknown_not_zero():
    assert Usage.of(tokens=10).get("credits").value is None


def test_the_ceiling_is_checked_between_calls():
    ledger = Ledger({"credits": 10})
    ledger.admit()
    ledger.settle(Usage.of(credits=7))
    ledger.admit()  # 7 < 10: the next call may start...
    ledger.settle(Usage.of(credits=7))  # ...and end above the ceiling
    with pytest.raises(LimitReached):
        ledger.admit()  # but nothing starts after that
    assert ledger.remaining("credits") == 0


def test_an_unmeasured_call_stops_a_capped_run():
    ledger = Ledger({"credits": 100})
    ledger.settle(Usage({}, calls=1))
    with pytest.raises(AccountingUnknown):
        ledger.admit()
    assert ledger.remaining("credits") is None


def test_uncapped_units_do_not_block():
    ledger = Ledger({"credits": 100})
    ledger.settle(Usage.of(credits=1, tokens=500))
    ledger.settle(Usage.of(credits=1))  # tokens unmeasured: unknown, but not capped
    ledger.admit()
    assert ledger.spent.get("tokens").value is None


def test_call_cap():
    ledger = Ledger(max_calls=2)
    for _ in range(2):
        ledger.admit()
        ledger.settle(Usage({}, calls=1))
    with pytest.raises(LimitReached):
        ledger.admit()


@given(st.lists(st.floats(min_value=0, max_value=50, allow_nan=False), max_size=30),
       st.floats(min_value=1, max_value=500, allow_nan=False))
def test_overshoot_is_bounded_by_one_call(costs, ceiling):
    ledger = Ledger({"credits": ceiling})
    for cost in costs:
        try:
            ledger.admit()
        except LimitReached:
            break
        ledger.settle(Usage.of(credits=cost))
    spent = ledger.spent.get("credits").value
    assert spent is not None
    assert spent < ceiling + max(costs, default=0) + 1e-9


def test_round_trip_json():
    usage = Usage({"credits": Amount("credits", 3.5), "tokens": Amount("tokens", None)}, 4)
    assert Usage.from_json(usage.to_json()) == usage


@pytest.mark.parametrize("bad", [-1.0, float("inf"), float("nan")])
def test_amounts_reject_impossible_values(bad):
    with pytest.raises(ValueError):
        Amount("credits", bad)
