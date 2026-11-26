from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any

import pytest

from flowproof.testbed.engine import Frame, Game, cell_changes, parse_change
from flowproof.testbed.games import GAMES, glide, relay, twins
from flowproof.testbed.solve import optimal_actions, shortest_path

SEEDS = range(10)
LEVELS = (1, 2, 3, 4)
CASES = [(name, level) for name in GAMES for level in LEVELS]


def _changed(game: Game, state: Any, action: str) -> tuple[Any, set[tuple[int, int]]]:
    after = game.step(state, action)
    cells = {parse_change(item)[:2] for item in cell_changes(game.frame(state), game.frame(after))}
    return after, cells


def _find(frame: Frame, glyph: str) -> tuple[int, int]:
    hits = [(r, c) for r, row in enumerate(frame.grid) for c, g in enumerate(row) if g == glyph]
    assert len(hits) == 1, f"{glyph!r} appears {len(hits)} times"
    return hits[0]


def test_the_registry_holds_three_four_level_games():
    assert sorted(GAMES) == ["glide", "relay", "twins"]
    for name, game in GAMES.items():
        assert game.name == name
        assert game.levels == 4


@pytest.mark.parametrize(("name", "level"), CASES)
def test_every_level_is_solvable_quickly_for_seeds_0_to_9(name: str, level: int):
    game = GAMES[name]
    for seed in SEEDS:
        state = game.start(level, seed)
        began = time.perf_counter()
        path = shortest_path(game, state)
        elapsed = time.perf_counter() - began
        assert path, f"{name} level {level} seed {seed} has no solution or starts solved"
        assert elapsed < 1.0, f"{name} level {level} seed {seed}: BFS took {elapsed:.2f}s"
        for action in path:
            assert not game.solved(state)
            state = game.step(state, action)
        assert game.solved(state)
        assert optimal_actions(game, level, seed) == len(path)


@pytest.mark.parametrize(("name", "level"), CASES)
def test_layouts_are_small_ascii_seeded_and_distinct(name: str, level: int):
    game = GAMES[name]
    grids = set()
    for seed in SEEDS:
        frame = game.frame(game.start(level, seed))
        assert frame == game.frame(game.start(level, seed))
        assert (frame.level, frame.levels, frame.status) == (level, 4, "playing")
        assert 1 <= len(frame.grid) <= 9
        assert 1 <= len(frame.grid[0]) <= 9
        assert all("!" <= glyph <= "~" for row in frame.grid for glyph in row)
        assert frame.actions
        grids.add(frame.grid)
    assert len(grids) == len(SEEDS)


def test_layouts_do_not_depend_on_the_hash_seed():
    script = (
        "from flowproof.testbed.games import GAMES\n"
        "for g in GAMES.values():\n"
        "    for level in (1, 2, 3, 4):\n"
        "        print(g.frame(g.start(level, 5)).grid)\n"
    )
    outputs = set()
    for hash_seed in ("1", "2"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        done = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, env=env, check=True
        )
        outputs.add(done.stdout)
    assert len(outputs) == 1


@pytest.mark.parametrize("name", sorted(GAMES))
def test_illegal_actions_change_nothing_and_solved_levels_accept_nothing(name: str):
    game = GAMES[name]
    state = game.start(1, 0)
    for action in ("jump", "", "press 9 9", "UP", " up"):
        assert game.key(game.step(state, action)) == game.key(state)
    path = shortest_path(game, state)
    assert path
    for action in path:
        state = game.step(state, action)
    frame = game.frame(state)
    assert frame.status == "level-cleared"
    assert frame.actions == ()
    assert game.step(state, path[0]) == state
    last = game.start(4, 0)
    for action in shortest_path(game, last) or []:
        last = game.step(last, action)
    assert game.frame(last).status == "won"


class TestRelay:
    game = relay.Relay()

    def test_level_1_toggles_a_plus(self):
        state = relay.state_from_rows(1, ["ooo", "ooo", "ooo"])
        _, centre = _changed(self.game, state, "press 1 1")
        assert centre == {(0, 1), (1, 0), (1, 1), (1, 2), (2, 1)}
        _, corner = _changed(self.game, state, "press 0 0")
        assert corner == {(0, 0), (0, 1), (1, 0)}

    def test_level_2_toggles_the_diagonals_and_itself(self):
        state = relay.state_from_rows(2, ["oooo"] * 4)
        _, cells = _changed(self.game, state, "press 1 1")
        assert cells == {(0, 0), (0, 2), (2, 0), (2, 2), (1, 1)}
        _, corner = _changed(self.game, state, "press 0 0")
        assert corner == {(0, 0), (1, 1)}

    def test_level_3_toggles_knight_moves_and_itself(self):
        state = relay.state_from_rows(3, ["oooo"] * 4)
        _, corner = _changed(self.game, state, "press 0 0")
        assert corner == {(0, 0), (1, 2), (2, 1)}
        _, inner = _changed(self.game, state, "press 1 1")
        assert inner == {(1, 1), (0, 3), (2, 3), (3, 0), (3, 2)}

    def test_level_4_toggles_the_rest_of_the_row(self):
        state = relay.state_from_rows(4, ["oooo"] * 4)
        _, cells = _changed(self.game, state, "press 1 1")
        assert cells == {(1, 0), (1, 2), (1, 3)}

    def test_presses_flip_lamps_undo_themselves_and_all_on_clears(self):
        state = relay.state_from_rows(1, ["*o*", "ooo", "*o*"])
        pressed = self.game.step(state, "press 1 1")
        assert self.game.frame(pressed).grid == ("***", "***", "***")
        assert self.game.solved(pressed)
        assert self.game.frame(pressed).status == "level-cleared"
        assert not self.game.solved(state)
        other = self.game.step(state, "press 0 1")
        assert self.game.step(other, "press 0 1") == state

    def test_the_hidden_pattern_differs_on_every_level(self):
        patterns = {relay.pattern(level, 4, 4, 1, 1) for level in LEVELS}
        assert len(patterns) == 4


class TestGlide:
    game = glide.Glide()

    def _slide(self, level: int, rows: list[str], *actions: str) -> glide.GlideState:
        state = glide.state_from_rows(level, rows)
        for action in actions:
            state = self.game.step(state, action)
        return state

    def test_level_1_slides_until_a_wall_and_must_stop_on_the_goal(self):
        passing = self._slide(1, ["#######", "#@.G..#", "#######"], "right")
        assert passing.pos == (1, 5)
        assert not self.game.solved(passing)
        assert self.game.frame(passing).grid[1] == "#..G.@#"
        resting = self._slide(1, ["#######", "#@...G#", "#######"], "right")
        assert self.game.solved(resting)

    def test_level_2_plus_tiles_stop_the_slide(self):
        rows = ["#######", "#@.+..#", "#G....#", "#######"]
        stopped = self._slide(2, rows, "right")
        assert stopped.pos == (1, 3)
        assert self.game.frame(stopped).grid[1] == "#..@..#"
        onward = self.game.step(stopped, "right")
        assert onward.pos == (1, 5)
        assert self.game.frame(onward).grid[1] == "#..+.@#"

    def test_level_3_arrows_turn_the_slide(self):
        rows = ["#######", "#@..v.#", "#.....#", "#G....#", "#######"]
        assert self._slide(3, rows, "right").pos == (3, 4)
        into_wall = ["#######", "#@..^.#", "#G....#", "#######"]
        assert self._slide(3, into_wall, "right").pos == (1, 4)

    def test_level_4_tildes_reverse_the_slide_once(self):
        rows = ["#######", "#.@.~.#", "#G....#", "#######"]
        assert self._slide(4, rows, "right").pos == (1, 1)
        twice = ["#######", "#.~.@~#", "#G....#", "#######"]
        state = self._slide(4, twice, "right")
        assert state.pos == (1, 1)
        assert self.game.frame(state).grid[1] == "#@~..~#"

    def test_tiles_from_other_levels_are_rejected_when_drawn(self):
        with pytest.raises(ValueError):
            glide.state_from_rows(1, ["#####", "#@+G#", "#####"])

    def test_a_slide_that_would_cycle_goes_nowhere(self):
        rows = ("#######", "#.>.<.#", "#######")
        assert glide.slide(rows, (1, 1), "right", 3) == (1, 1)


class TestTwins:
    game = twins.Twins()
    rows = ("#######", "#A....#", "#.....#", "#..B..#", "#....a#", "#b....#", "#######")

    def _positions(self, state: twins.TwinsState) -> tuple[tuple[int, int], tuple[int, int]]:
        frame = self.game.frame(state)
        return _find(frame, "A"), _find(frame, "B")

    def _play(self, level: int, *actions: str, rows: tuple[str, ...] | None = None):
        state = twins.state_from_rows(level, rows or self.rows)
        for action in actions:
            state = self.game.step(state, action)
        return self._positions(state)

    def test_level_1_b_copies_a(self):
        assert self._play(1, "right") == ((1, 2), (3, 4))
        assert self._play(1, "down") == ((2, 1), (4, 3))

    def test_level_2_b_mirrors_left_and_right(self):
        assert self._play(2, "right") == ((1, 2), (3, 2))
        assert self._play(2, "down") == ((2, 1), (4, 3))

    def test_level_3_b_mirrors_up_and_down(self):
        assert self._play(3, "down") == ((2, 1), (2, 3))
        assert self._play(3, "right") == ((1, 2), (3, 4))

    def test_level_4_b_moves_on_every_second_action(self):
        assert self._play(4, "right") == ((1, 2), (3, 3))
        assert self._play(4, "right", "right") == ((1, 3), (3, 4))
        assert self._play(4, "right", "right", "down") == ((2, 3), (3, 4))
        assert self._play(4, "right", "right", "down", "down") == ((3, 3), (4, 4))

    def test_walls_block_each_avatar_on_its_own(self):
        assert self._play(1, "up") == ((1, 1), (2, 3))
        assert self._play(1, "left") == ((1, 1), (3, 2))

    def test_avatars_never_share_a_cell_or_swap(self):
        side_by_side = ("######", "#AB..#", "#a..b#", "######")
        assert self._play(2, "right", rows=side_by_side) == ((1, 1), (1, 2))
        assert self._play(1, "left", rows=side_by_side) == ((1, 1), (1, 2))
        assert self._play(1, "right", rows=side_by_side) == ((1, 2), (1, 3))

    def test_both_goals_at_once_clear_the_level(self):
        rows = ("######", "#Aa..#", "#Bb..#", "######")
        state = twins.state_from_rows(1, rows)
        assert self.game.solved(self.game.step(state, "right"))
        assert not self.game.solved(twins.state_from_rows(2, rows))
        assert not self.game.solved(self.game.step(twins.state_from_rows(4, rows), "right"))

    def test_the_level_4_beat_is_hidden_state_that_search_keys_on(self):
        state = twins.state_from_rows(4, self.rows)
        later = twins.state_from_rows(4, self.rows, moves=1)
        assert self.game.frame(state) == self.game.frame(later)
        assert self.game.key(state) != self.game.key(later)
        plain = twins.state_from_rows(1, self.rows)
        assert self.game.key(plain) == self.game.key(twins.state_from_rows(1, self.rows, moves=1))
