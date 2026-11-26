"""Evidence-based policies: frozen tests, behaviour clusters and exact patch votes."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Hashable, Sequence
from typing import Any

from flowproof.select.types import Candidate, Ranking, per_test_outcomes

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")


def ordered(cands: Sequence[Candidate]) -> list[Candidate]:
    """Candidates sorted by id, so results never depend on input order. Ids must be unique."""
    by_id: dict[str, Candidate] = {}
    for cand in cands:
        if cand.id in by_id:
            raise ValueError(f"duplicate candidate id {cand.id!r}")
        by_id[cand.id] = cand
    return [by_id[key] for key in sorted(by_id)]


def grade_gap(cand: Candidate) -> str | None:
    """Why the candidate has no usable test evidence, or None when it has some."""
    if not cand.outcome.is_ok:
        return f"outcome-{cand.outcome.status.value}"
    if cand.grade is None:
        return "no-grade"
    if cand.grade.true_passed is None:
        return "grade-unknown"
    return None


def _tampered(cands: Sequence[Candidate]) -> tuple[str, ...]:
    return tuple(c.id for c in cands if c.grade is not None and c.grade.tamper.tampered)


def _count_layers(group: Sequence[Candidate]) -> list[tuple[str, ...]]:
    """Order one pass/fail class by tests passed; counts are compared only when both are known.

    A candidate whose count is unknown is beaten by nobody, so it joins the top layer.
    """
    known: dict[int, list[str]] = {}
    unknown: list[str] = []
    for cand in group:
        count = cand.grade.tests_passed if cand.grade is not None else None
        if count is None:
            unknown.append(cand.id)
        else:
            known.setdefault(count, []).append(cand.id)
    layers = [sorted(known[count]) for count in sorted(known, reverse=True)]
    if not layers:
        return [tuple(sorted(unknown))]
    layers[0] = sorted(layers[0] + unknown)
    return [tuple(layer) for layer in layers]


def tests_ranking(cands: Sequence[Candidate]) -> Ranking:
    """Rank by true (post-restore) test results.

    Untampered before tampered, then passed before failed, then more tests passed first when
    both counts are known. Candidates that did not finish OK, have no grade, or whose true
    run could not be judged are unranked.
    """
    pool = ordered(cands)
    classes: dict[tuple[bool, bool], list[Candidate]] = {}
    unranked: list[str] = []
    reasons: dict[str, str] = {}
    evidence: dict[str, dict[str, Any]] = {}
    for cand in pool:
        grade = cand.grade
        if grade is not None:
            evidence[cand.id] = {
                "true_passed": grade.true_passed,
                "tests_passed": grade.tests_passed,
                "tests_failed": grade.tests_failed,
                "tampered": grade.tamper.tampered,
            }
        gap = grade_gap(cand)
        if gap is not None or grade is None:
            unranked.append(cand.id)
            reasons[cand.id] = gap or "no-grade"
            continue
        classes.setdefault((grade.tamper.tampered, not grade.true_passed), []).append(cand)
    tiers: list[tuple[str, ...]] = []
    for key in sorted(classes):
        tiers.extend(_count_layers(classes[key]))
    tampered = _tampered(pool)
    return Ranking(
        policy="tests",
        tiers=tuple(tiers),
        unranked=tuple(unranked),
        flagged={"tampered": tampered} if tampered else {},
        detail={"unranked_reasons": reasons, "evidence": evidence},
    )


def _group(keys: dict[str, Hashable]) -> list[tuple[str, ...]]:
    """Group ids with equal keys; largest group first, ties by smallest member id."""
    groups: dict[Hashable, list[str]] = {}
    for cid in sorted(keys):
        groups.setdefault(keys[cid], []).append(cid)
    return sorted((tuple(members) for members in groups.values()), key=lambda m: (-len(m), m[0]))


def _tiers_by_size(groups: Sequence[tuple[str, ...]]) -> tuple[tuple[str, ...], ...]:
    """Merge equal-sized groups into one tier: a vote between equal groups is a tie."""
    by_size: dict[int, list[str]] = {}
    for members in groups:
        by_size.setdefault(len(members), []).extend(members)
    return tuple(tuple(sorted(by_size[size])) for size in sorted(by_size, reverse=True))


def behavior_ranking(cands: Sequence[Candidate]) -> Ranking:
    """Rank by the size of each candidate's behaviour cluster.

    Clusters use per-test outcome vectors when every ranked candidate has them, otherwise the
    coarser (true_passed, tests_passed, tests_failed) summary. Unranked as in `tests_ranking`.
    """
    pool = ordered(cands)
    gaps = {c.id: grade_gap(c) for c in pool}
    reasons = {cid: gap for cid, gap in gaps.items() if gap is not None}
    ranked = [c for c in pool if gaps[c.id] is None]
    unranked = tuple(sorted(reasons))
    vectors = {c.id: per_test_outcomes(c.grade) for c in ranked}
    if not ranked:
        mode = "none"
    elif all(vector is not None for vector in vectors.values()):
        mode = "per-test"
    else:
        mode = "summary"
    keys: dict[str, Hashable] = {}
    for cand in ranked:
        grade = cand.grade
        vector = vectors[cand.id]
        if mode == "per-test" and vector is not None:
            keys[cand.id] = tuple(sorted((str(k), bool(v)) for k, v in vector.items()))
        elif grade is not None:
            keys[cand.id] = (grade.true_passed, grade.tests_passed, grade.tests_failed)
    groups = _group(keys)
    tampered = _tampered(pool)
    return Ranking(
        policy="behavior",
        tiers=_tiers_by_size(groups),
        unranked=unranked,
        flagged={"tampered": tampered} if tampered else {},
        detail={"mode": mode, "clusters": [list(g) for g in groups], "unranked_reasons": reasons},
    )


def normalise_patch(patch: str) -> str:
    """Canonical text of a unified diff for equality voting.

    Drops `index` lines, hunk header line numbers, trailing whitespace and lines that are
    blank or add, remove or keep only a blank line.
    """
    out: list[str] = []
    for raw in patch.split("\n"):
        line = raw.rstrip()
        if not line or line in ("+", "-") or line.startswith("index "):
            continue
        header = _HUNK_HEADER.match(line)
        if header is not None:
            line = "@@ @@" + line[header.end() :]
        out.append(line)
    return "\n".join(out)


def exact_ranking(cands: Sequence[Candidate]) -> Ranking:
    """Rank by how many candidates produced the same normalised patch.

    Empty changes form their own group. Candidates without a change, or whose run did not
    finish OK, are unranked.
    """
    pool = ordered(cands)
    keys: dict[str, Hashable] = {}
    digests: dict[str, str] = {}
    unranked: list[str] = []
    reasons: dict[str, str] = {}
    empty: list[str] = []
    for cand in pool:
        if not cand.outcome.is_ok or cand.change is None:
            unranked.append(cand.id)
            reasons[cand.id] = (
                "no-change" if cand.outcome.is_ok else f"outcome-{cand.outcome.status.value}"
            )
            continue
        if cand.change.empty:
            empty.append(cand.id)
            keys[cand.id] = ("empty",)
            digests[cand.id] = "empty"
            continue
        text = normalise_patch(cand.change.patch)
        keys[cand.id] = ("patch", text)
        digests[cand.id] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    groups = _group(keys)
    return Ranking(
        policy="exact",
        tiers=_tiers_by_size(groups),
        unranked=tuple(unranked),
        flagged={"empty-patch": tuple(empty)} if empty else {},
        detail={
            "groups": [list(g) for g in groups],
            "digests": digests,
            "unranked_reasons": reasons,
        },
    )


def by_tests(cands: Sequence[Candidate]) -> list[list[str]]:
    """Tiers of ids by true test results, best first; unranked candidates form the last tier."""
    return tests_ranking(cands).as_lists()


def behavior_clusters(cands: Sequence[Candidate]) -> list[list[str]]:
    """Behaviour clusters, largest first (ties by smallest id); unranked ones form a last group."""
    ranking = behavior_ranking(cands)
    clusters: list[list[str]] = [list(c) for c in ranking.detail["clusters"]]
    if ranking.unranked:
        clusters.append(list(ranking.unranked))
    return clusters


def exact_vote(cands: Sequence[Candidate]) -> list[list[str]]:
    """Groups of identical normalised patches, largest first; unranked ones form a last group."""
    ranking = exact_ranking(cands)
    groups: list[list[str]] = [list(g) for g in ranking.detail["groups"]]
    if ranking.unranked:
        groups.append(list(ranking.unranked))
    return groups
