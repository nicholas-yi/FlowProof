"""Headroom: how much a selection policy recovers of what best-of-N could offer."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from flowproof.select.types import Candidate, Selection


@dataclass(frozen=True)
class HeadroomRow:
    """One field: its size, how many candidates were correct, and what was chosen."""

    size: int
    correct: int
    pass_at_1: float
    oracle: float
    selected: float
    chosen: str | None

    @property
    def abstained(self) -> bool:
        return self.chosen is None

    def to_json(self) -> dict[str, Any]:
        return {
            "size": self.size,
            "correct": self.correct,
            "pass_at_1": self.pass_at_1,
            "oracle": self.oracle,
            "selected": self.selected,
            "chosen": self.chosen,
        }


@dataclass(frozen=True)
class Headroom:
    """Averages over `n` fields.

    `pass_at_1` is the expected success of one random attempt and `random_choice` the expected
    success of picking uniformly from the field; they are equal by construction and both are
    kept so reports name the baseline explicitly. `oracle` is the share of fields with any
    correct candidate, `selected` the share where the chosen candidate was correct (abstaining
    counts as not correct), and `regret = oracle - selected`.
    """

    n: int
    pass_at_1: float
    random_choice: float
    oracle: float
    selected: float
    regret: float
    abstained: int = 0
    rows: tuple[HeadroomRow, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "pass_at_1": self.pass_at_1,
            "random_choice": self.random_choice,
            "oracle": self.oracle,
            "selected": self.selected,
            "regret": self.regret,
            "abstained": self.abstained,
            "rows": [row.to_json() for row in self.rows],
        }


def headroom(
    fields: Sequence[tuple[Sequence[Candidate], Selection]],
    correct: Callable[[Candidate], bool],
) -> Headroom:
    """Score selections against ground truth. A field with no candidates scores zero."""
    if not fields:
        raise ValueError("headroom needs at least one field")
    rows: list[HeadroomRow] = []
    for cands, selection in fields:
        verdicts = {cand.id: bool(correct(cand)) for cand in cands}
        if len(verdicts) != len(cands):
            raise ValueError("candidate ids within a field must be unique")
        if selection.chosen is not None and selection.chosen not in verdicts:
            raise ValueError(f"chosen candidate {selection.chosen!r} is not in its field")
        hits = sum(verdicts.values())
        won = selection.chosen is not None and verdicts[selection.chosen]
        rows.append(
            HeadroomRow(
                size=len(verdicts),
                correct=hits,
                pass_at_1=hits / len(verdicts) if verdicts else 0.0,
                oracle=1.0 if hits else 0.0,
                selected=1.0 if won else 0.0,
                chosen=selection.chosen,
            )
        )
    count = len(rows)
    pass_at_1 = sum(row.pass_at_1 for row in rows) / count
    oracle = sum(row.oracle for row in rows) / count
    selected = sum(row.selected for row in rows) / count
    return Headroom(
        n=count,
        pass_at_1=pass_at_1,
        random_choice=pass_at_1,
        oracle=oracle,
        selected=selected,
        regret=oracle - selected,
        abstained=sum(row.abstained for row in rows),
        rows=tuple(rows),
    )
