"""Native-unit, fail-closed accounting.

Usage is kept in the unit each provider bills in. A quantity that was not
measured is *unknown*, not zero: it propagates through every sum, and a ledger
with a ceiling in that unit refuses further work rather than pretend the run
was cheap.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Amount:
    """A quantity in one unit; `value is None` means it was not measured."""

    unit: str
    value: float | None

    def __post_init__(self) -> None:
        if not self.unit:
            raise ValueError("an amount needs a unit")
        if self.value is not None and (not math.isfinite(self.value) or self.value < 0):
            raise ValueError(f"amounts are finite and non-negative, got {self.value!r}")

    @property
    def known(self) -> bool:
        return self.value is not None

    def __add__(self, other: Amount) -> Amount:
        if other.unit != self.unit:
            raise ValueError(f"cannot add {other.unit} to {self.unit}")
        if self.value is None or other.value is None:
            return Amount(self.unit, None)
        return Amount(self.unit, self.value + other.value)

    def __str__(self) -> str:
        return f"unknown {self.unit}" if self.value is None else f"{self.value:g} {self.unit}"


@dataclass(frozen=True)
class Usage:
    """What one or more calls consumed, by unit, plus how many calls there were.

    A unit that is absent from `amounts` was not measured at all. `get` reports
    such a unit as unknown, so code cannot accidentally read it as zero.
    """

    amounts: Mapping[str, Amount] = field(default_factory=dict)
    calls: int = 0

    def __post_init__(self) -> None:
        if self.calls < 0:
            raise ValueError("call counts are non-negative")
        for unit, amount in self.amounts.items():
            if amount.unit != unit:
                raise ValueError(f"amount filed under {unit!r} is in {amount.unit!r}")

    @staticmethod
    def of(calls: int = 1, **values: float) -> Usage:
        return Usage({u: Amount(u, float(v)) for u, v in values.items()}, calls)

    @staticmethod
    def unknown(*units: str, calls: int = 1) -> Usage:
        return Usage({u: Amount(u, None) for u in units}, calls)

    @staticmethod
    def none() -> Usage:
        return Usage({}, 0)

    def get(self, unit: str) -> Amount:
        return self.amounts.get(unit, Amount(unit, None))

    def __add__(self, other: Usage) -> Usage:
        units = set(self.amounts) | set(other.amounts)
        merged: dict[str, Amount] = {}
        for unit in sorted(units):
            mine, theirs = self.amounts.get(unit), other.amounts.get(unit)
            if mine is None or theirs is None:
                # A unit one side never measured makes the total unknown,
                # unless that side made no calls at all.
                known_side = mine if theirs is None else theirs
                assert known_side is not None
                missing_calls = other.calls if theirs is None else self.calls
                merged[unit] = known_side if missing_calls == 0 else Amount(unit, None)
            else:
                merged[unit] = mine + theirs
        return Usage(merged, self.calls + other.calls)

    def to_json(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "amounts": {u: a.value for u, a in sorted(self.amounts.items())},
        }

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> Usage:
        amounts = {u: Amount(u, None if v is None else float(v)) for u, v in data["amounts"].items()}
        return Usage(amounts, int(data["calls"]))


class LimitReached(Exception):
    """A ceiling has been reached; the next call must not start."""


class AccountingUnknown(Exception):
    """Spend in a capped unit is unknown, so the ceiling cannot be enforced."""


class Ledger:
    """Admits calls against ceilings and settles what each call consumed.

    The check happens between calls: a call admitted below a ceiling may end
    above it, and the next `admit` refuses. `remaining` tells callers how much
    room is left so they can pass a hard per-call cap to the provider.
    """

    def __init__(self, ceilings: Mapping[str, float] | None = None, max_calls: int | None = None):
        ceilings = dict(ceilings or {})
        for unit, limit in ceilings.items():
            if not math.isfinite(limit) or limit < 0:
                raise ValueError(f"ceiling for {unit} must be finite and non-negative")
        if max_calls is not None and max_calls < 0:
            raise ValueError("max_calls must be non-negative")
        self.ceilings: dict[str, float] = ceilings
        self.max_calls = max_calls
        self.spent = Usage({u: Amount(u, 0.0) for u in ceilings}, 0)

    def admit(self) -> None:
        if self.max_calls is not None and self.spent.calls >= self.max_calls:
            raise LimitReached(f"call cap {self.max_calls} reached")
        for unit, limit in self.ceilings.items():
            amount = self.spent.get(unit)
            if amount.value is None:
                raise AccountingUnknown(f"{unit} spent is unknown; ceiling {limit:g} unenforceable")
            if amount.value >= limit:
                raise LimitReached(f"{unit} ceiling {limit:g} reached ({amount.value:g} spent)")

    def settle(self, usage: Usage) -> None:
        missing = {u: Amount(u, None) for u in self.ceilings if u not in usage.amounts}
        if missing and usage.calls:
            usage = Usage({**usage.amounts, **missing}, usage.calls)
        self.spent = self.spent + usage

    def remaining(self, unit: str) -> float | None:
        if unit not in self.ceilings:
            return None
        spent = self.spent.get(unit).value
        if spent is None:
            return None
        return max(0.0, self.ceilings[unit] - spent)

    def snapshot(self) -> dict[str, Any]:
        return {
            "ceilings": dict(self.ceilings),
            "max_calls": self.max_calls,
            "spent": self.spent.to_json(),
        }
