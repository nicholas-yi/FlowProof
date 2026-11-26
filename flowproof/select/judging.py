"""Pairwise judging in both presentation orders, with Copeland scores and bias measurements."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from flowproof.select.ranking import ordered
from flowproof.select.types import Candidate, Judge, Preference, Ranking

MAX_CYCLES = 1000
_CYCLE_SEARCH_STEPS = 200_000


@dataclass(frozen=True)
class PairwiseResult:
    """Everything `pairwise` learned about a field.

    `wins[a][b]` is 1 when `a` beat `b` in both orders. `draws` lists every judged pair
    without a winner; `inconsistent` is the subset whose two answers disagreed. `unjudged`
    pairs had an INVALID answer. `tiers` ranks candidates with at least one judged pair by
    Copeland score (wins minus losses); `unranked` holds the rest. Rates are None when there
    was nothing to measure.
    """

    ids: tuple[str, ...]
    wins: Mapping[str, Mapping[str, int]]
    draws: tuple[tuple[str, str], ...]
    inconsistent: tuple[tuple[str, str], ...]
    unjudged: tuple[tuple[str, str], ...]
    scores: Mapping[str, int]
    tiers: tuple[tuple[str, ...], ...]
    unranked: tuple[str, ...]
    cycles: tuple[tuple[str, ...], ...]
    cycles_truncated: bool
    first_position_rate: float | None
    second_position_rate: float | None
    consistency: float | None
    calls: int
    answers: tuple[tuple[str, str, Preference], ...]

    def ranking(self) -> list[list[str]]:
        """Ranked tiers followed by the unranked candidates as a final tier."""
        out = [list(tier) for tier in self.tiers]
        if self.unranked:
            out.append(list(self.unranked))
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "ids": list(self.ids),
            "wins": [[a, b] for a in self.ids for b in self.ids if self.wins[a][b]],
            "draws": [list(p) for p in self.draws],
            "inconsistent": [list(p) for p in self.inconsistent],
            "unjudged": [list(p) for p in self.unjudged],
            "scores": dict(self.scores),
            "tiers": [list(t) for t in self.tiers],
            "unranked": list(self.unranked),
            "cycles": [list(c) for c in self.cycles],
            "cycles_truncated": self.cycles_truncated,
            "first_position_rate": self.first_position_rate,
            "second_position_rate": self.second_position_rate,
            "consistency": self.consistency,
            "calls": self.calls,
            "answers": [[a, b, p.value] for a, b, p in self.answers],
        }


def _ask(judge: Judge, first: Candidate, second: Candidate) -> Preference:
    answer: object = judge(first, second)
    if isinstance(answer, Preference):
        return answer
    try:
        return Preference(answer)
    except ValueError:
        return Preference.INVALID


def _pick(answer: Preference, first: str, second: str) -> str | None:
    if answer is Preference.FIRST:
        return first
    if answer is Preference.SECOND:
        return second
    return None


def _cycles(
    ids: Sequence[str], beats: Mapping[str, set[str]]
) -> tuple[tuple[tuple[str, ...], ...], bool]:
    """Elementary cycles of the win graph, each starting at its smallest id."""
    rank = {cid: index for index, cid in enumerate(ids)}
    found: list[tuple[str, ...]] = []
    steps = 0
    for start in ids:
        path = [start]
        on_path = {start}
        stack: list[Iterator[str]] = [iter(sorted(beats[start]))]
        while stack:
            nxt = next(stack[-1], None)
            if nxt is None:
                stack.pop()
                on_path.discard(path.pop())
                continue
            steps += 1
            if steps > _CYCLE_SEARCH_STEPS:
                return tuple(found), True
            if nxt == start:
                found.append(tuple(path))
                if len(found) >= MAX_CYCLES:
                    return tuple(found), True
            elif rank[nxt] > rank[start] and nxt not in on_path:
                path.append(nxt)
                on_path.add(nxt)
                stack.append(iter(sorted(beats[nxt])))
    return tuple(found), False


def pairwise(cands: Sequence[Candidate], judge: Judge) -> PairwiseResult:
    """Ask the judge about every unordered pair in both orders.

    A candidate wins a pair only when both answers name it. TIE in both orders is a
    consistent draw; any other disagreement, including preferring whichever was shown first,
    is a position-inconsistent draw. INVALID in either order leaves the pair unjudged.
    """
    pool = ordered(cands)
    ids = tuple(c.id for c in pool)
    wins = {a: dict.fromkeys(ids, 0) for a in ids}
    beats: dict[str, set[str]] = {a: set() for a in ids}
    scores = dict.fromkeys(ids, 0)
    judged_pairs = dict.fromkeys(ids, 0)
    draws: list[tuple[str, str]] = []
    inconsistent: list[tuple[str, str]] = []
    unjudged: list[tuple[str, str]] = []
    answers: list[tuple[str, str, Preference]] = []
    first_count = second_count = consistent = calls = 0
    for i, a in enumerate(pool):
        for b in pool[i + 1 :]:
            forward = _ask(judge, a, b)
            backward = _ask(judge, b, a)
            calls += 2
            answers.extend(((a.id, b.id, forward), (b.id, a.id, backward)))
            for answer in (forward, backward):
                if answer is Preference.FIRST:
                    first_count += 1
                elif answer is Preference.SECOND:
                    second_count += 1
            pair = (a.id, b.id)
            if Preference.INVALID in (forward, backward):
                unjudged.append(pair)
                continue
            judged_pairs[a.id] += 1
            judged_pairs[b.id] += 1
            pick_forward = _pick(forward, a.id, b.id)
            pick_backward = _pick(backward, b.id, a.id)
            if pick_forward != pick_backward:
                draws.append(pair)
                inconsistent.append(pair)
                continue
            consistent += 1
            if pick_forward is None:
                draws.append(pair)
                continue
            loser = b.id if pick_forward == a.id else a.id
            wins[pick_forward][loser] = 1
            beats[pick_forward].add(loser)
            scores[pick_forward] += 1
            scores[loser] -= 1
    ranked = [cid for cid in ids if judged_pairs[cid] > 0]
    levels = sorted({scores[cid] for cid in ranked}, reverse=True)
    tiers = tuple(tuple(cid for cid in ranked if scores[cid] == level) for level in levels)
    cycles, truncated = _cycles(ids, beats)
    positional = first_count + second_count
    judged = consistent + len(inconsistent)
    return PairwiseResult(
        ids=ids,
        wins=wins,
        draws=tuple(draws),
        inconsistent=tuple(inconsistent),
        unjudged=tuple(unjudged),
        scores=scores,
        tiers=tiers,
        unranked=tuple(cid for cid in ids if judged_pairs[cid] == 0),
        cycles=cycles,
        cycles_truncated=truncated,
        first_position_rate=first_count / positional if positional else None,
        second_position_rate=second_count / positional if positional else None,
        consistency=consistent / judged if judged else None,
        calls=calls,
        answers=tuple(answers),
    )


def pairwise_ranking(cands: Sequence[Candidate], judge: Judge) -> tuple[Ranking, PairwiseResult]:
    """`pairwise` expressed as a `Ranking` for `choose`."""
    result = pairwise(cands, judge)
    ranking = Ranking(
        policy="pairwise",
        tiers=result.tiers,
        unranked=result.unranked,
        flagged={"in-cycle": tuple(sorted({cid for c in result.cycles for cid in c}))}
        if result.cycles
        else {},
        detail=result.to_json(),
    )
    return ranking, result
