"""Selection types and the structural views of changes and grades that selection reads.

Selection never imports `flowproof.workspace` or `flowproof.guards` at runtime. The protocols
below describe exactly the attributes it reads, so the real `Change` and `Grade` satisfy them
and tests can use small fakes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, TypeAlias

from flowproof.outcomes import Outcome


class ChangeLike(Protocol):
    """What selection reads from a `flowproof.workspace.Change`."""

    @property
    def patch(self) -> str: ...

    @property
    def empty(self) -> bool: ...


class TamperLike(Protocol):
    """What selection reads from a `flowproof.guards.TamperReport`."""

    @property
    def tampered(self) -> bool: ...


class GradeLike(Protocol):
    """What selection reads from a `flowproof.guards.Grade`.

    A grade may also carry `per_test: Mapping[str, bool]` (test id -> passed). It is optional
    and read with `per_test_outcomes`.
    """

    @property
    def true_passed(self) -> bool | None: ...

    @property
    def tests_passed(self) -> int | None: ...

    @property
    def tests_failed(self) -> int | None: ...

    @property
    def tamper(self) -> TamperLike: ...


def per_test_outcomes(grade: GradeLike | None) -> Mapping[str, bool] | None:
    """The optional per-test outcome mapping of a grade, or None when it has none."""
    if grade is None:
        return None
    value = getattr(grade, "per_test", None)
    return value if isinstance(value, Mapping) else None


@dataclass(frozen=True)
class Candidate:
    """One attempt at a task: its change, its grade and how its run ended."""

    id: str
    change: ChangeLike | None
    grade: GradeLike | None
    outcome: Outcome


class Preference(StrEnum):
    """A judge's answer for a pair shown in a fixed order."""

    FIRST = "first"
    SECOND = "second"
    TIE = "tie"
    INVALID = "invalid"


Judge: TypeAlias = Callable[[Candidate, Candidate], Preference]
"""Called as `judge(a, b)`: `a` is shown first and `b` second."""


@dataclass(frozen=True)
class Selection:
    """The result of `choose`.

    `chosen` is None when abstaining. `basis` names the policies that narrowed the pool, in
    order, plus `"incumbent-retained"` when the incumbent was kept. `fallback` names the
    tie-break that decided, if any.
    """

    chosen: str | None
    basis: tuple[str, ...]
    fallback: str | None
    trace: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Ranking:
    """A policy's verdict on a pool.

    `tiers` holds the ranked candidates, best first; ids in one tier are tied. `unranked`
    holds candidates the policy had no evidence for. They are not losers: `choose` drops them
    only when some candidate does have evidence, and never drops an unranked incumbent.
    """

    policy: str
    tiers: tuple[tuple[str, ...], ...]
    unranked: tuple[str, ...] = ()
    flagged: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_lists(self) -> list[list[str]]:
        """Ranked tiers followed by the unranked candidates as a final tier."""
        out = [list(tier) for tier in self.tiers]
        if self.unranked:
            out.append(list(self.unranked))
        return out

    def tier_of(self, candidate_id: str) -> int | None:
        """Index of the tier holding the candidate, or None if it is unranked or absent."""
        for index, tier in enumerate(self.tiers):
            if candidate_id in tier:
                return index
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "tiers": [list(tier) for tier in self.tiers],
            "unranked": list(self.unranked),
            "flagged": {key: list(ids) for key, ids in sorted(self.flagged.items())},
            "detail": dict(self.detail),
        }
