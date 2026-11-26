from __future__ import annotations

import re

import pytest

from flowproof.accounting import Ledger
from flowproof.testbed.canary import canary_hits
from flowproof.testbed.clients import OracleClient
from flowproof.testbed.engine import Frame, cell_changes, frame_text, parse_change
from flowproof.testbed.games import CANARIES, GAMES, glide, relay, twins
from flowproof.testbed.runner import ARMS, run_episode
from flowproof.testbed.score import LevelResult, episode_score, level_efficiency
from flowproof.testbed.solve import shortest_path


def _frame(grid: tuple[str, ...], actions: tuple[str, ...] = ("up",)) -> Frame:
    return Frame(grid, 1, 4, actions, "playing")


def test_frame_text_numbers_rows_and_columns_and_lists_actions():
    text = frame_text(_frame(("#.@", "..G"), ("up", "down")))
    assert text.splitlines() == [
        "Level 1 of 4 (playing)",
        "  0 1 2",
        "0 # . @",
        "1 . . G",
        "Legal actions: up, down",
    ]


def test_frame_text_shows_no_actions_on_a_cleared_level():
    frame = Frame(("**",), 2, 4, (), "level-cleared")
    assert frame_text(frame).endswith("Legal actions: (none)")


def test_frames_reject_bad_status_levels_and_ragged_rows():
    with pytest.raises(ValueError):
        Frame(("..",), 1, 4, (), "paused")
    with pytest.raises(ValueError):
        Frame(("..",), 5, 4, (), "playing")
    with pytest.raises(ValueError):
        Frame(("..", "."), 1, 4, (), "playing")


def test_cell_changes_lists_new_glyphs_sorted_by_row_then_column():
    before = _frame(("@..", "...", "..."))
    after = _frame(("...", "...", "..@"))
    assert cell_changes(before, after) == ["0,0=.", "2,2=@"]
    assert cell_changes(before, before) == []


def test_cell_changes_counts_cells_outside_the_old_grid_as_changed():
    assert cell_changes(_frame(("..",)), _frame(("...", "#.."))) == [
        "0,2=.",
        "1,0=#",
        "1,1=.",
        "1,2=.",
    ]


def test_parse_change_accepts_spacing_and_rejects_other_forms():
    assert parse_change(" 3 , 4 = * ") == (3, 4, "*")
    for bad in ("3,4", "a,b=c", "3,4=", "3,4=ab", "-1,2=x"):
        with pytest.raises(ValueError):
            parse_change(bad)


def test_every_game_has_its_own_canary_and_hits_are_reported_by_name():
    modules = {"relay": relay, "glide": glide, "twins": twins}
    assert {name: module.CANARY for name, module in modules.items()} == CANARIES
    assert len(set(CANARIES.values())) == 3
    assert all(re.fullmatch(r"[0-9a-f]{32}", canary) for canary in CANARIES.values())
    assert canary_hits("an honest prompt") == []
    assert canary_hits(f"leaked {twins.CANARY} and {relay.CANARY}") == ["relay", "twins"]
    assert canary_hits([{"response": f"...{glide.CANARY}..."}]) == ["glide"]


@pytest.mark.parametrize("name", sorted(GAMES))
def test_frames_never_contain_a_canary(name: str):
    game = GAMES[name]
    for level in (1, 2, 3, 4):
        for seed in range(10):
            state = game.start(level, seed)
            texts = [frame_text(game.frame(state))]
            for action in shortest_path(game, state) or []:
                state = game.step(state, action)
                texts.append(frame_text(game.frame(state)))
            assert canary_hits("\n".join(texts)) == []


@pytest.mark.parametrize("arm", ARMS)
def test_prompts_and_transcripts_never_contain_a_canary_or_the_game_name(arm: str):
    for name, game in GAMES.items():
        client = OracleClient(game, 0)
        episode = run_episode(game, 0, arm, client, Ledger({"credits": 500}))
        assert canary_hits(client.prompts) == []
        assert canary_hits(episode.to_json()) == []
        assert not any(name in prompt.lower() for prompt in client.prompts)


def test_level_efficiency_is_capped_at_one():
    assert level_efficiency(3, 4) == 0.75
    assert level_efficiency(4, 4) == 1.0
    assert level_efficiency(5, 4) == 1.0
    with pytest.raises(ValueError):
        level_efficiency(-1, 3)


def test_episode_score_weights_levels_by_number():
    results = [
        LevelResult(1, actions=5, optimal=5, cleared=True),
        LevelResult(2, actions=8, optimal=4, cleared=True),
        LevelResult(3, actions=4, optimal=6, cleared=True),
        LevelResult(4, actions=30, optimal=10, cleared=False),
    ]
    assert [r.efficiency for r in results] == [1.0, 0.5, 1.0, 0.0]
    assert episode_score(results, 4) == pytest.approx((1 * 1.0 + 2 * 0.5 + 3 * 1.0) / 10)


def test_missing_levels_score_zero():
    only_last = [LevelResult(4, actions=12, optimal=9, cleared=True)]
    assert episode_score(only_last, 4) == pytest.approx(4 * 0.75 / 10)
    assert episode_score([], 4) == 0.0
    perfect = [LevelResult(level, 3, 3, True) for level in (1, 2, 3, 4)]
    assert episode_score(perfect, 4) == 1.0
    assert LevelResult(2, 8, 4, True).to_json() == {
        "level": 2,
        "actions": 8,
        "optimal": 4,
        "cleared": True,
        "efficiency": 0.5,
    }
