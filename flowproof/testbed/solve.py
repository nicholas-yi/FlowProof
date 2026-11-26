"""Breadth-first search over a game's states, keyed by `game.key`."""

from __future__ import annotations

from collections import deque
from collections.abc import Hashable
from functools import lru_cache
from typing import Any

from flowproof.testbed.engine import Game


class SearchLimitExceeded(RuntimeError):
    """The search visited more distinct states than its limit allows."""


class Unsolvable(ValueError):
    """Every reachable state was visited and none of them is solved."""


def shortest_path(game: Game, state: Any, *, limit: int = 2_000_000) -> list[str] | None:
    """An optimal action sequence from `state` to a solved state, or None if there is none.

    Actions are tried in the order the frame lists them, so the result is
    deterministic. Raises `SearchLimitExceeded` after `limit` distinct states.
    """
    if game.solved(state):
        return []
    start = game.key(state)
    parents: dict[Hashable, tuple[Hashable, str] | None] = {start: None}
    frontier: deque[tuple[Any, Hashable]] = deque([(state, start)])
    while frontier:
        current, current_key = frontier.popleft()
        for action in game.frame(current).actions:
            child = game.step(current, action)
            child_key = game.key(child)
            if child_key in parents:
                continue
            parents[child_key] = (current_key, action)
            if game.solved(child):
                path: list[str] = []
                link = parents[child_key]
                while link is not None:
                    path.append(link[1])
                    link = parents[link[0]]
                return path[::-1]
            if len(parents) > limit:
                raise SearchLimitExceeded(f"{game.name}: more than {limit} states")
            frontier.append((child, child_key))
    return None


def optimal_actions(game: Game, level: int, seed: int, *, limit: int = 2_000_000) -> int:
    """The fewest actions that clear `level` of `game` for `seed` (cached)."""
    return _optimal(game, level, seed, limit)


@lru_cache(maxsize=4096)
def _optimal(game: Game, level: int, seed: int, limit: int) -> int:
    path = shortest_path(game, game.start(level, seed), limit=limit)
    if path is None:
        raise Unsolvable(f"{game.name} level {level} seed {seed} cannot be solved")
    return len(path)
