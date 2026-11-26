"""Efficiency and level-weighted episode scores."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


def level_efficiency(optimal: int, actions: int) -> float:
    """`min(1, optimal / actions)` for a cleared level."""
    if optimal < 0 or actions < 0:
        raise ValueError("action counts are non-negative")
    if actions == 0:
        return 1.0
    return min(1.0, optimal / actions)


@dataclass(frozen=True)
class LevelResult:
    level: int
    actions: int
    optimal: int
    cleared: bool

    @property
    def efficiency(self) -> float:
        return level_efficiency(self.optimal, self.actions) if self.cleared else 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "actions": self.actions,
            "optimal": self.optimal,
            "cleared": self.cleared,
            "efficiency": self.efficiency,
        }


def episode_score(results: Iterable[LevelResult], levels: int) -> float:
    """Mean efficiency over levels 1..`levels` weighted by level number.

    Levels missing from `results` or not cleared score 0.
    """
    if levels < 1:
        raise ValueError("a game has at least one level")
    efficiency = {r.level: r.efficiency for r in results if 1 <= r.level <= levels}
    total = sum(level * efficiency.get(level, 0.0) for level in range(1, levels + 1))
    return total / (levels * (levels + 1) / 2)
