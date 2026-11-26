"""Observation types shared by every testbed game.

A game is a pure state machine. A model is only ever shown `frame_text` of the
current state (plus the cell changes its own actions caused), so a game's rules
can be learned only by acting and watching.
"""

from __future__ import annotations

import re
from collections.abc import Hashable
from dataclasses import dataclass
from typing import Any, Protocol

PLAYING = "playing"
LEVEL_CLEARED = "level-cleared"
WON = "won"
STATUSES = (PLAYING, LEVEL_CLEARED, WON)

CHANGE_RE = re.compile(r"^\s*(\d+)\s*,\s*(\d+)\s*=\s*([!-~])\s*$")


@dataclass(frozen=True)
class Frame:
    """What a player can see: the grid, where it is in the game, and what it may do."""

    grid: tuple[str, ...]
    level: int
    levels: int
    actions: tuple[str, ...]
    status: str

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown frame status {self.status!r}")
        if not 1 <= self.level <= self.levels:
            raise ValueError(f"level {self.level} is outside 1..{self.levels}")
        if self.grid and any(len(row) != len(self.grid[0]) for row in self.grid):
            raise ValueError("grid rows must all have the same width")


class Game(Protocol):
    """A deterministic, level-based grid game.

    `start(level, seed)` builds a level's initial state; `step` never mutates
    and returns the state unchanged for an illegal action or a solved level.
    """

    @property
    def name(self) -> str: ...

    @property
    def levels(self) -> int: ...

    def start(self, level: int, seed: int) -> Any: ...

    def step(self, state: Any, action: str) -> Any: ...

    def frame(self, state: Any) -> Frame: ...

    def solved(self, state: Any) -> bool: ...

    def key(self, state: Any) -> Hashable: ...


def status_of(level: int, levels: int, solved: bool) -> str:
    if not solved:
        return PLAYING
    return WON if level == levels else LEVEL_CLEARED


def frame_text(frame: Frame) -> str:
    """Render a frame with 0-based row numbers on the left and column numbers on top."""
    rows = len(frame.grid)
    cols = len(frame.grid[0]) if rows else 0
    row_width = len(str(max(rows - 1, 0)))
    col_width = len(str(max(cols - 1, 0)))
    header = " " * (row_width + 1) + " ".join(str(c).rjust(col_width) for c in range(cols))
    lines = [f"Level {frame.level} of {frame.levels} ({frame.status})", header]
    for r, row in enumerate(frame.grid):
        cells = " ".join(glyph.rjust(col_width) for glyph in row)
        lines.append(f"{str(r).rjust(row_width)} {cells}")
    actions = ", ".join(frame.actions) if frame.actions else "(none)"
    lines.append(f"Legal actions: {actions}")
    return "\n".join(lines)


def cell_changes(before: Frame, after: Frame) -> list[str]:
    """Cells of `after` whose glyph differs from `before`, as sorted `"r,c=g"` items."""
    changed: list[tuple[int, int, str]] = []
    for r, row in enumerate(after.grid):
        old = before.grid[r] if r < len(before.grid) else ""
        for c, glyph in enumerate(row):
            if c >= len(old) or old[c] != glyph:
                changed.append((r, c, glyph))
    return [f"{r},{c}={g}" for r, c, g in sorted(changed)]


def parse_change(item: str) -> tuple[int, int, str]:
    """Parse one `"r,c=g"` item; raises ValueError if it is not in that form."""
    match = CHANGE_RE.match(item)
    if match is None:
        raise ValueError(f"not a cell change: {item!r}")
    return int(match.group(1)), int(match.group(2)), match.group(3)


def format_change(row: int, col: int, glyph: str) -> str:
    return f"{row},{col}={glyph}"
