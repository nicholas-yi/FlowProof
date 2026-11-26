"""Helpers shared by the walled grid games (`glide` and `twins`)."""

from __future__ import annotations

import random
from collections.abc import Iterable, Sequence

Pos = tuple[int, int]

MOVES: tuple[str, ...] = ("up", "down", "left", "right")
DELTAS: dict[str, Pos] = {"up": (-1, 0), "down": (1, 0), "left": (0, -1), "right": (0, 1)}
WALL = "#"
FLOOR = "."


def walled_grid(rng: random.Random, size: int, density: float) -> list[list[str]]:
    """A square grid with a wall border and randomly placed interior walls."""
    grid = [[WALL] * size for _ in range(size)]
    for r in range(1, size - 1):
        for c in range(1, size - 1):
            grid[r][c] = WALL if rng.random() < density else FLOOR
    return grid


def free_cells(grid: Sequence[Sequence[str]]) -> list[Pos]:
    return [(r, c) for r, row in enumerate(grid) for c, g in enumerate(row) if g == FLOOR]


def blocked(rows: Sequence[str], pos: Pos) -> bool:
    r, c = pos
    return not (0 <= r < len(rows) and 0 <= c < len(rows[r])) or rows[r][c] == WALL


def overlay(rows: Sequence[str], marks: Iterable[tuple[Pos, str]]) -> tuple[str, ...]:
    """Copy `rows` with glyphs drawn at the given positions, later marks on top."""
    grid = [list(row) for row in rows]
    for (r, c), glyph in marks:
        grid[r][c] = glyph
    return tuple("".join(row) for row in grid)


def take_markers(
    rows: Sequence[str], markers: str, allowed: str
) -> tuple[tuple[str, ...], dict[str, Pos]]:
    """Split hand-drawn rows into base rows (markers replaced by floor) and marker positions.

    Every marker must appear exactly once; every other glyph must be in `allowed`.
    """
    if not rows or any(len(row) != len(rows[0]) for row in rows):
        raise ValueError("rows must be a non-empty rectangle")
    found: dict[str, Pos] = {}
    base: list[str] = []
    for r, row in enumerate(rows):
        cells: list[str] = []
        for c, glyph in enumerate(row):
            if glyph in markers:
                if glyph in found:
                    raise ValueError(f"marker {glyph!r} appears more than once")
                found[glyph] = (r, c)
                cells.append(FLOOR)
            elif glyph in allowed:
                cells.append(glyph)
            else:
                raise ValueError(f"glyph {glyph!r} is not allowed here")
        base.append("".join(cells))
    missing = [m for m in markers if m not in found]
    if missing:
        raise ValueError(f"missing markers: {''.join(missing)}")
    return tuple(base), found


def check_level(level: int, levels: int) -> None:
    if not 1 <= level <= levels:
        raise ValueError(f"level must be in 1..{levels}, got {level}")
