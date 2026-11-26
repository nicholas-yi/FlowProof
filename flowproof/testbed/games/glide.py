"""Glide: an avatar on ice slides in the chosen direction until a wall stops it.

The goal `G` counts only when the avatar comes to rest on it. Each level adds
its own kind of tile with a hidden effect on a slide that enters it: level 1
has none, level 2 `+` stops the slide there, level 3 `>`, `<`, `^` and `v`
turn the slide to face that way, and level 4 `~` reverses the slide (at most
once per slide). Layouts are drawn from a per-(level, seed) random stream and
redrawn until a search confirms they are solvable, need at least `MIN_MOVES`
moves, and cannot be solved by carrying over the plain sliding rule of level 1.
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
    WALL,
    Pos,
    blocked,
    check_level,
    free_cells,
    overlay,
    take_markers,
    walled_grid,
)

CANARY = "37994b8d553e0fb410f08ad73e950efd"

AVATAR = "@"
GOAL = "G"
STOP = "+"
REVERSE = "~"
ARROWS: Mapping[str, str] = MappingProxyType({">": "right", "<": "left", "^": "up", "v": "down"})
OPPOSITE: Mapping[str, str] = MappingProxyType(
    {"up": "down", "down": "up", "left": "right", "right": "left"}
)
LEVELS = 4
TILES: Mapping[int, str] = MappingProxyType({1: "", 2: STOP, 3: "><^v", 4: REVERSE})
SIZES: Mapping[int, int] = MappingProxyType({1: 7, 2: 8, 3: 9, 4: 9})
TILE_COUNTS: Mapping[int, tuple[int, int]] = MappingProxyType(
    {1: (0, 0), 2: (2, 4), 3: (2, 4), 4: (2, 3)}
)
MIN_MOVES = 3
MAX_ATTEMPTS = 10_000


@dataclass(frozen=True)
class GlideState:
    level: int
    rows: tuple[str, ...]  # walls, ice and tiles; the avatar and goal are kept apart
    goal: Pos
    pos: Pos


def _slide(rows: Sequence[str], pos: Pos, direction: str, rule: int) -> tuple[Pos, bool]:
    r, c = pos
    reversed_once = False
    seen: set[tuple[int, int, str, bool]] = set()
    while True:
        dr, dc = DELTAS[direction]
        if blocked(rows, (r + dr, c + dc)):
            return (r, c), False
        r, c = r + dr, c + dc
        tile = rows[r][c]
        if rule == 2 and tile == STOP:
            return (r, c), False
        if rule == 3 and tile in ARROWS:
            direction = ARROWS[tile]
        elif rule == 4 and tile == REVERSE and not reversed_once:
            direction = OPPOSITE[direction]
            reversed_once = True
        marker = (r, c, direction, reversed_once)
        if marker in seen:
            return pos, True
        seen.add(marker)


def slide(rows: Sequence[str], pos: Pos, direction: str, rule: int) -> Pos:
    """Where a slide ends under `rule`; a slide that would cycle forever goes nowhere."""
    return _slide(rows, pos, direction, rule)[0]


def _solve(rows: Sequence[str], start: Pos, goal: Pos, rule: int) -> list[str] | None:
    if start == goal:
        return []
    parents: dict[Pos, tuple[Pos, str] | None] = {start: None}
    queue = deque([start])
    while queue:
        here = queue.popleft()
        for move in MOVES:
            there = slide(rows, here, move, rule)
            if there in parents:
                continue
            parents[there] = (here, move)
            if there == goal:
                path: list[str] = []
                link = parents[there]
                while link is not None:
                    path.append(link[1])
                    link = parents[link[0]]
                return path[::-1]
            queue.append(there)
    return None


def _reaches(rows: Sequence[str], start: Pos, goal: Pos, moves: Sequence[str], rule: int) -> bool:
    pos = start
    for move in moves:
        pos = slide(rows, pos, move, rule)
        if pos == goal:
            return True
    return pos == goal


def _cycles(rows: Sequence[str], rule: int) -> bool:
    return any(
        _slide(rows, (r, c), move, rule)[1]
        for r, row in enumerate(rows)
        for c, glyph in enumerate(row)
        if glyph != WALL
        for move in MOVES
    )


def _candidate(level: int, rng: random.Random) -> tuple[tuple[str, ...], Pos, Pos] | None:
    size = SIZES[level]
    grid = walled_grid(rng, size, rng.uniform(0.12, 0.22))
    free = free_cells(grid)
    low, high = TILE_COUNTS[level]
    count = rng.randint(low, high)
    if len(free) < count + size:
        return None
    picks = rng.sample(free, count + 2)
    for r, c in picks[2:]:
        grid[r][c] = rng.choice(TILES[level])
    return tuple("".join(row) for row in grid), picks[0], picks[1]


def _acceptable(level: int, rows: tuple[str, ...], start: Pos, goal: Pos) -> bool:
    if level == 3 and _cycles(rows, level):
        return False
    path = _solve(rows, start, goal, level)
    if path is None or len(path) < MIN_MOVES:
        return False
    if level > 1:
        plain = _solve(rows, start, goal, 1)
        if plain is not None and _reaches(rows, start, goal, plain, level):
            return False
    return True


@lru_cache(maxsize=512)
def _layout(level: int, seed: int) -> tuple[tuple[str, ...], Pos, Pos]:
    rng = random.Random(f"glide:{level}:{seed}")
    for _ in range(MAX_ATTEMPTS):
        candidate = _candidate(level, rng)
        if candidate is not None and _acceptable(level, *candidate):
            return candidate
    raise RuntimeError(f"no acceptable glide layout for level {level}, seed {seed}")


@dataclass(frozen=True)
class Glide:
    name: str = field(default="glide", init=False)
    levels: int = field(default=LEVELS, init=False)

    def start(self, level: int, seed: int) -> GlideState:
        check_level(level, self.levels)
        rows, start, goal = _layout(level, seed)
        return GlideState(level, rows, goal, start)

    def step(self, state: GlideState, action: str) -> GlideState:
        if action not in DELTAS or self.solved(state):
            return state
        pos = slide(state.rows, state.pos, action, state.level)
        return GlideState(state.level, state.rows, state.goal, pos)

    def frame(self, state: GlideState) -> Frame:
        grid = overlay(state.rows, ((state.goal, GOAL), (state.pos, AVATAR)))
        solved = self.solved(state)
        actions = () if solved else MOVES
        status = status_of(state.level, self.levels, solved)
        return Frame(grid, state.level, self.levels, actions, status)

    def solved(self, state: GlideState) -> bool:
        return state.pos == state.goal

    def key(self, state: GlideState) -> tuple[int, Pos]:
        return (state.level, state.pos)


def state_from_rows(level: int, rows: Sequence[str]) -> GlideState:
    """Build a state from hand-drawn rows containing one `@` and one `G`."""
    check_level(level, LEVELS)
    base, marks = take_markers(rows, AVATAR + GOAL, "#." + TILES[level])
    return GlideState(level, base, marks[GOAL], marks[AVATAR])
