"""`choose`: apply selection policies in order, never treating missing evidence as a loss."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from flowproof.select.judging import pairwise_ranking
from flowproof.select.ranking import behavior_ranking, exact_ranking, ordered, tests_ranking
from flowproof.select.types import Candidate, Judge, Ranking, Selection

POLICIES = ("tests", "behavior", "pairwise", "exact")
FALLBACKS = ("incumbent", "first", "abstain")
RETAINED = "incumbent-retained"


def rank(policy: str, cands: Sequence[Candidate], *, judge: Judge | None = None) -> Ranking:
    """Run one named policy over a pool."""
    if policy == "tests":
        return tests_ranking(cands)
    if policy == "behavior":
        return behavior_ranking(cands)
    if policy == "exact":
        return exact_ranking(cands)
    if policy == "pairwise":
        if judge is None:
            raise ValueError("policy 'pairwise' needs a judge")
        return pairwise_ranking(cands, judge)[0]
    raise ValueError(f"unknown selection policy {policy!r}; expected one of {POLICIES}")


def _validate(
    cands: Sequence[Candidate],
    chain: Sequence[str],
    judge: Judge | None,
    incumbent: str | None,
    fallback: str,
) -> dict[str, Candidate]:
    if isinstance(chain, str):
        raise ValueError("chain must be a sequence of policy names, not a string")
    unknown = [name for name in chain if name not in POLICIES]
    if unknown:
        raise ValueError(f"unknown selection policies {unknown}; expected some of {POLICIES}")
    if "pairwise" in chain and judge is None:
        raise ValueError("policy 'pairwise' needs a judge")
    if fallback not in FALLBACKS:
        raise ValueError(f"unknown fallback {fallback!r}; expected one of {FALLBACKS}")
    by_id = {c.id: c for c in ordered(cands)}
    if incumbent is not None and incumbent not in by_id:
        raise ValueError(f"incumbent {incumbent!r} is not among the candidates")
    return by_id


def choose(
    cands: Sequence[Candidate],
    chain: Sequence[str],
    *,
    judge: Judge | None = None,
    incumbent: str | None = None,
    fallback: str = "abstain",
) -> Selection:
    """Narrow the pool policy by policy, keeping only the best tier each time.

    Unranked candidates are dropped only when some candidate in the pool has evidence. The
    incumbent leaves the pool only when a policy ranks it strictly below the best tier; when
    it has no evidence under a policy it stays. If the incumbent survives to a final tie it is
    retained whatever `fallback` says. Otherwise a final tie is broken by `fallback`:
    `"first"` takes the smallest id and `"abstain"` (or `"incumbent"` with no incumbent left)
    chooses nothing.
    """
    by_id = _validate(cands, chain, judge, incumbent, fallback)
    pool = list(by_id)
    basis: list[str] = []
    steps: list[dict[str, Any]] = []
    skipped: list[str] = []
    judge_calls = 0
    eliminated_by: dict[str, Any] | None = None
    for index, policy in enumerate(chain):
        if len(pool) <= 1:
            skipped.append(policy)
            continue
        ranking = rank(policy, [by_id[cid] for cid in pool], judge=judge)
        judge_calls += int(ranking.detail.get("calls", 0)) if policy == "pairwise" else 0
        kept = set(ranking.tiers[0]) if ranking.tiers else set(pool)
        incumbent_note: str | None = None
        if incumbent is not None and incumbent in pool:
            if incumbent in kept:
                incumbent_note = "kept"
            elif incumbent in ranking.unranked:
                kept.add(incumbent)
                incumbent_note = "kept-without-evidence"
            else:
                incumbent_note = "eliminated"
                eliminated_by = {"policy": policy, "step": index, "best": list(ranking.tiers[0])}
        after = sorted(kept)
        steps.append(
            {
                "step": index,
                "policy": policy,
                "pool": list(pool),
                **ranking.to_json(),
                "kept": after,
                "dropped_unranked": [cid for cid in ranking.unranked if cid not in kept],
                "incumbent": incumbent_note,
            }
        )
        if after != pool:
            basis.append(policy)
        pool = after
    trace: dict[str, Any] = {
        "candidates": list(by_id),
        "chain": list(chain),
        "incumbent": incumbent,
        "fallback_requested": fallback,
        "steps": steps,
        "skipped": skipped,
        "final_pool": list(pool),
        "judge_calls": judge_calls,
        "incumbent_eliminated_by": eliminated_by,
    }
    chosen: str | None
    used: str | None = None
    if len(pool) <= 1:
        chosen = pool[0] if pool else None
    elif incumbent is not None and incumbent in pool:
        chosen, used = incumbent, "incumbent"
    elif fallback == "first":
        chosen, used = pool[0], "first"
    else:
        chosen, used = None, "abstain"
    if chosen is not None and chosen == incumbent:
        basis.append(RETAINED)
    return Selection(chosen=chosen, basis=tuple(basis), fallback=used, trace=trace)
