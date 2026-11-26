"""Twins: two avatars, `A` and `B`, both respond to every move.

The goal is `A` resting on `a` while `B` rests on `b`. Walls block each
avatar on its own; the avatars can neither share a cell nor swap places (a move
that would do either leaves both where they were). How `B` responds is hidden
and changes by level: level 1 it copies `A`, level 2 it mirrors left and right,
level 3 it mirrors up and down, and level 4 it copies `A` but only on every
second action of the level. Layouts are redrawn from a per-(level, seed) random
stream until a search confirms they are solvable within `MIN_MOVES` to
`MAX_MOVES` moves and cannot be solved by carrying over the level 1 rule.
"""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from types import MappingProxyType

from flowproof.testbed.engine import Frame, status_of
from flowproof.testbed.games._grid import (
    DELTAS,
    MOVES,
    Pos,
    blocked,
    check_level,
    free_cells,
    overlay,
    take_markers,
    walled_grid,
)

CANARY = "34aeb332114fb18acb6d34e331a8db94"

AVATAR_A = "A"
AVATAR_B = "B"
GOAL_A = "a"
GOAL_B = "b"
LEVELS = 4
SIZES: Mapping[int, int] = MappingProxyType({1: 7, 2: 8, 3: 8, 4: 9})
MIN_MOVES = 4
MAX_MOVES = 24
MAX_ATTEMPTS = 10_000
_MIRROR_H: Mapping[str, str] = MappingProxyType({"left": "right", "right": "left"})
_MIRROR_V: Mapping[str, str] = MappingProxyType({"up": "down", "down": "up"})

_Node = tuple[Pos, Pos, int]


@dataclass(frozen=True)
class TwinsState:
    level: int
    rows: tuple[str, ...]  # walls and floor; avatars and goals are kept apart
    goal_a: Pos
    goal_b: Pos
    a: Pos
    b: Pos
    moves: int = 0  # actions taken so far on this level


def b_direction(rule: int, direction: str, number: int) -> str | None:
    """How B responds to the `number`-th action (1-based) of a level, or None if it rests."""
    if rule == 2:
        return _MIRROR_H.get(direction, direction)
    if rule == 3:
        return _MIRROR_V.get(direction, direction)
    if rule == 4 and number % 2:
        return None
    return direction


def _target(rows: Sequence[str], pos: Pos, direction: str) -> Pos:
    dr, dc = DELTAS[direction]
    there = (pos[0] + dr, pos[1] + dc)
    return pos if blocked(rows, there) else there


def move(
    rows: Sequence[str], a: Pos, b: Pos, direction: str, number: int, rule: int
) -> tuple[Pos, Pos]:
    """Both avatars' positions after the `number`-th action of a level under `rule`."""
    to_a = _target(rows, a, direction)
    b_way = b_direction(rule, direction, number)
    to_b = b if b_way is None else _target(rows, b, b_way)
    if to_a == to_b or (to_a == b and to_b == a):
        return a, b
    return to_a, to_b


def _solve(
    rows: Sequence[str], a: Pos, b: Pos, goals: tuple[Pos, Pos], rule: int, taken: int = 0
) -> list[str] | None:
    if (a, b) == goals:
        return []
    period = 2 if rule == 4 else 1
    start: _Node = (a, b, taken % period)
    parents: dict[_Node, tuple[_Node, str] | None] = {start: None}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        here_a, here_b, phase = node
        for direction in MOVES:
            next_a, next_b = move(rows, here_a, here_b, direction, phase + 1, rule)
            child: _Node = (next_a, next_b, (phase + 1) % period)
            if child in parents:
                continue
            parents[child] = (node, direction)
            if (next_a, next_b) == goals:
                path: list[str] = []
                link = parents[child]
                while link is not None:
                    path.append(link[1])
                    link = parents[link[0]]
                return path[::-1]
            queue.append(child)
    return None


def _reaches(
    rows: Sequence[str], a: Pos, b: Pos, goals: tuple[Pos, Pos], moves: Sequence[str], rule: int
) -> bool:
    for number, direction in enumerate(moves, start=1):
        a, b = move(rows, a, b, direction, number, rule)
        if (a, b) == goals:
            return True
    return (a, b) == goals


def _candidate(level: int, rng: random.Random) -> tuple[tuple[str, ...], Pos, Pos, Pos, Pos] | None:
    size = SIZES[level]
    grid = walled_grid(rng, size, rng.uniform(0.10, 0.20))
    free = free_cells(grid)
    if len(free) < 2 * size:
        return None
    a, b, goal_a, goal_b = rng.sample(free, 4)
    return tuple("".join(row) for row in grid), a, b, goal_a, goal_b


def _acceptable(level: int, rows: tuple[str, ...], a: Pos, b: Pos, ga: Pos, gb: Pos) -> bool:
    path = _solve(rows, a, b, (ga, gb), level)
    if path is None or not MIN_MOVES <= len(path) <= MAX_MOVES:
        return False
    if level > 1:
        plain = _solve(rows, a, b, (ga, gb), 1)
        if plain is not None and _reaches(rows, a, b, (ga, gb), plain, level):
            return False
    return True


@lru_cache(maxsize=512)
def _layout(level: int, seed: int) -> tuple[tuple[str, ...], Pos, Pos, Pos, Pos]:
    rng = random.Random(f"twins:{level}:{seed}")
    for _ in range(MAX_ATTEMPTS):
        candidate = _candidate(level, rng)
        if candidate is not None and _acceptable(level, *candidate):
            return candidate
    raise RuntimeError(f"no acceptable twins layout for level {level}, seed {seed}")


@dataclass(frozen=True)
class Twins:
    name: str = field(default="twins", init=False)
    levels: int = field(default=LEVELS, init=False)

    def start(self, level: int, seed: int) -> TwinsState:
        check_level(level, self.levels)
        rows, a, b, goal_a, goal_b = _layout(level, seed)
        return TwinsState(level, rows, goal_a, goal_b, a, b)

    def step(self, state: TwinsState, action: str) -> TwinsState:
        if action not in DELTAS or self.solved(state):
            return state
        a, b = move(state.rows, state.a, state.b, action, state.moves + 1, state.level)
        return TwinsState(
            state.level, state.rows, state.goal_a, state.goal_b, a, b, state.moves + 1
        )

    def frame(self, state: TwinsState) -> Frame:
        grid = overlay(
            state.rows,
            (
                (state.goal_a, GOAL_A),
                (state.goal_b, GOAL_B),
                (state.a, AVATAR_A),
                (state.b, AVATAR_B),
            ),
        )
        solved = self.solved(state)
        actions = () if solved else MOVES
        status = status_of(state.level, self.levels, solved)
        return Frame(grid, state.level, self.levels, actions, status)

    def solved(self, state: TwinsState) -> bool:
        return state.a == state.goal_a and state.b == state.goal_b

    def key(self, state: TwinsState) -> tuple[int, Pos, Pos, int]:
        return (state.level, state.a, state.b, state.moves % 2 if state.level == 4 else 0)


def state_from_rows(level: int, rows: Sequence[str], *, moves: int = 0) -> TwinsState:
    """Build a state from hand-drawn rows containing one each of `A`, `B`, `a` and `b`."""
    check_level(level, LEVELS)
    base, marks = take_markers(rows, AVATAR_A + AVATAR_B + GOAL_A + GOAL_B, "#.")
    return TwinsState(
        level, base, marks[GOAL_A], marks[GOAL_B], marks[AVATAR_A], marks[AVATAR_B], moves
    )
