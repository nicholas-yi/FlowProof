"""Relay: a board of lamps where pressing one toggles a hidden pattern of lamps.

The pattern changes by level: a plus shape, then the four diagonals, then the
knight-move cells, then the rest of the pressed lamp's row. Every pattern
except the last also toggles the pressed lamp. The goal is to light every lamp.
Starting boards are made by pressing lamps from the all-on board, so every
level is solvable; seeds index a seeded shuffle of all boards whose optimal
solution length lies in the level's range, so nearby seeds never repeat.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache, lru_cache
from types import MappingProxyType

from flowproof.testbed.engine import Frame, status_of
from flowproof.testbed.games._grid import Pos, check_level

CANARY = "704adeaf43404ea16b1dc1d156627992"

OFF = "o"
ON = "*"
LEVELS = 4
SIZES: Mapping[int, tuple[int, int]] = MappingProxyType(
    {1: (3, 3), 2: (4, 4), 3: (4, 4), 4: (4, 4)}
)
# Inclusive range of optimal press counts a starting board may need.
DEPTHS: Mapping[int, tuple[int, int]] = MappingProxyType(
    {1: (2, 3), 2: (3, 4), 3: (3, 4), 4: (3, 4)}
)

_PLUS: tuple[Pos, ...] = ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1))
_DIAGONAL: tuple[Pos, ...] = ((0, 0), (-1, -1), (-1, 1), (1, -1), (1, 1))
_KNIGHT: tuple[Pos, ...] = (
    (0, 0), (-2, -1), (-2, 1), (-1, -2), (-1, 2), (1, -2), (1, 2), (2, -1), (2, 1),
)  # fmt: skip


def pattern(level: int, rows: int, cols: int, row: int, col: int) -> frozenset[Pos]:
    """The cells a press at (row, col) toggles under `level`'s rule."""
    check_level(level, LEVELS)
    if level == 4:
        return frozenset((row, c) for c in range(cols) if c != col)
    offsets = {1: _PLUS, 2: _DIAGONAL, 3: _KNIGHT}[level]
    return frozenset(
        (row + dr, col + dc) for dr, dc in offsets if 0 <= row + dr < rows and 0 <= col + dc < cols
    )


@dataclass(frozen=True)
class RelayState:
    level: int
    rows: int
    cols: int
    lit: int  # bit r*cols+c is set when that lamp is on


@cache
def _toggles(level: int, rows: int, cols: int) -> tuple[int, ...]:
    masks: list[int] = []
    for r in range(rows):
        for c in range(cols):
            mask = 0
            for pr, pc in pattern(level, rows, cols, r, c):
                mask |= 1 << (pr * cols + pc)
            masks.append(mask)
    return tuple(masks)


@cache
def _actions(rows: int, cols: int) -> tuple[str, ...]:
    return tuple(f"press {r} {c}" for r in range(rows) for c in range(cols))


@cache
def _action_index(rows: int, cols: int) -> Mapping[str, int]:
    return MappingProxyType({a: i for i, a in enumerate(_actions(rows, cols))})


@cache
def _candidates(level: int) -> tuple[int, ...]:
    """Every board whose optimal solution has a length inside the level's range.

    Presses commute and undo themselves, so the distance from the solved board
    to a board equals that board's optimal solution length.
    """
    rows, cols = SIZES[level]
    low, high = DEPTHS[level]
    full = (1 << (rows * cols)) - 1
    toggles = _toggles(level, rows, cols)
    distance = {full: 0}
    frontier = [full]
    for depth in range(1, high + 1):
        reached: list[int] = []
        for mask in frontier:
            for toggle in toggles:
                board = mask ^ toggle
                if board not in distance:
                    distance[board] = depth
                    reached.append(board)
        frontier = reached
    return tuple(sorted(m for m, d in distance.items() if d >= low))


@lru_cache(maxsize=256)
def _shuffled(level: int, block: int) -> tuple[int, ...]:
    boards = list(_candidates(level))
    random.Random(f"relay:{level}:{block}").shuffle(boards)
    return tuple(boards)


@dataclass(frozen=True)
class Relay:
    name: str = field(default="relay", init=False)
    levels: int = field(default=LEVELS, init=False)

    def start(self, level: int, seed: int) -> RelayState:
        check_level(level, self.levels)
        rows, cols = SIZES[level]
        block, index = divmod(seed, len(_candidates(level)))
        return RelayState(level, rows, cols, _shuffled(level, block)[index])

    def step(self, state: RelayState, action: str) -> RelayState:
        if self.solved(state):
            return state
        index = _action_index(state.rows, state.cols).get(action)
        if index is None:
            return state
        toggle = _toggles(state.level, state.rows, state.cols)[index]
        return RelayState(state.level, state.rows, state.cols, state.lit ^ toggle)

    def frame(self, state: RelayState) -> Frame:
        grid = tuple(
            "".join(ON if state.lit >> (r * state.cols + c) & 1 else OFF for c in range(state.cols))
            for r in range(state.rows)
        )
        solved = self.solved(state)
        actions = () if solved else _actions(state.rows, state.cols)
        status = status_of(state.level, self.levels, solved)
        return Frame(grid, state.level, self.levels, actions, status)

    def solved(self, state: RelayState) -> bool:
        return state.lit == (1 << (state.rows * state.cols)) - 1

    def key(self, state: RelayState) -> tuple[int, int, int, int]:
        return (state.level, state.rows, state.cols, state.lit)


def state_from_rows(level: int, rows: Sequence[str]) -> RelayState:
    """Build a state from hand-drawn rows of `o` and `*`, played under `level`'s rule."""
    check_level(level, LEVELS)
    if not rows or any(len(row) != len(rows[0]) for row in rows):
        raise ValueError("rows must be a non-empty rectangle")
    lit = 0
    for r, row in enumerate(rows):
        for c, glyph in enumerate(row):
            if glyph not in (ON, OFF):
                raise ValueError(f"relay cells are {OFF!r} or {ON!r}, got {glyph!r}")
            if glyph == ON:
                lit |= 1 << (r * len(row) + c)
    return RelayState(level, len(rows), len(rows[0]), lit)
