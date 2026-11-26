from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from flowproof.accounting import Ledger, Usage
from flowproof.testbed.clients import (
    WRONG_CHANGE,
    NoisyPredictorClient,
    OracleClient,
    RandomClient,
    ScriptedClient,
)
from flowproof.testbed.demo import main as demo_main
from flowproof.testbed.engine import Game, cell_changes
from flowproof.testbed.games import GAMES
from flowproof.testbed.runner import (
    ARMS,
    PREAMBLE,
    Completion,
    contract_of,
    extract_json,
    parse_response,
    run_episode,
)
from flowproof.testbed.solve import optimal_actions, shortest_path

GENEROUS = 1_000.0


def _ledger(credits: float = GENEROUS, max_calls: int | None = None) -> Ledger:
    return Ledger({"credits": credits}, max_calls=max_calls)


def _oracle_steps(game: Game, seed: int, count: int) -> list[tuple[str, list[str]]]:
    """The first `count` optimal (action, changes) pairs of level 1."""
    state = game.start(1, seed)
    path = shortest_path(game, state) or []
    steps: list[tuple[str, list[str]]] = []
    for action in path[:count]:
        after = game.step(state, action)
        steps.append((action, cell_changes(game.frame(state), game.frame(after))))
        state = after
    return steps


def _predict(steps: list[tuple[str, list[str]]]) -> str:
    return json.dumps({"steps": [{"action": a, "expect": e} for a, e in steps]})


@pytest.mark.parametrize("arm", ARMS)
@pytest.mark.parametrize("name", sorted(GAMES))
def test_the_oracle_wins_every_game_under_every_arm_with_perfect_efficiency(name: str, arm: str):
    game = GAMES[name]
    for seed in range(10):
        episode = run_episode(game, seed, arm, OracleClient(game, seed), _ledger())
        assert episode.stop_reason == "won"
        assert episode.levels_cleared == episode.levels_total == 4
        assert episode.invalid_calls == 0
        assert episode.score == 1.0
        assert [r.efficiency for r in episode.per_level] == [1.0] * 4
        assert [r.optimal for r in episode.per_level] == [
            optimal_actions(game, level, seed) for level in (1, 2, 3, 4)
        ]
        if arm in ("predict", "adaptive"):
            assert episode.predictions_checked == episode.predictions_correct > 0
        else:
            assert episode.predictions_checked == 0


@pytest.mark.parametrize("name", sorted(GAMES))
def test_single_needs_at_least_as_many_calls_as_plan(name: str):
    game = GAMES[name]
    for seed in range(5):
        single = run_episode(game, seed, "single", OracleClient(game, seed), _ledger())
        plan = run_episode(game, seed, "plan", OracleClient(game, seed), _ledger())
        assert single.calls == single.actions == plan.actions
        assert single.calls >= plan.calls
        assert plan.calls < single.calls


def test_plan_plays_every_action_but_stops_at_a_level_change():
    game = GAMES["glide"]
    path = shortest_path(game, game.start(1, 0)) or []
    extra = [*path, "up", "up"]
    client = ScriptedClient([json.dumps({"actions": extra})])
    episode = run_episode(game, 0, "plan", client, _ledger(), max_calls=1)
    assert episode.transcript[0]["played"] == path
    assert episode.transcript[0]["outcome"] == "level-cleared"
    assert episode.per_level[1].actions == 0


@pytest.mark.parametrize("name", sorted(GAMES))
def test_predict_stops_at_the_first_wrong_prediction(name: str):
    game = GAMES[name]
    client = NoisyPredictorClient(game, 1, wrong_rate=0.4, noise_seed=11)
    episode = run_episode(game, 1, "predict", client, _ledger())
    assert episode.stop_reason == "won"
    assert episode.score == 1.0
    assert len(client.wrong_at) == episode.calls
    mismatches = 0
    for index, (entry, wrong) in enumerate(zip(episode.transcript, client.wrong_at, strict=True)):
        matches = [p["match"] for p in entry["predictions"]]
        if wrong is None:
            assert len(entry["played"]) == len(entry["parsed"])
            assert all(matches)
            continue
        mismatches += 1
        assert len(entry["played"]) == wrong
        assert matches == [True] * (wrong - 1) + [False]
        last = entry["predictions"][-1]
        assert WRONG_CHANGE in last["expected"]
        if index + 1 < len(client.prompts):
            follow_up = client.prompts[index + 1]
            assert f"Your prediction for step {wrong}" in follow_up
            actual = ", ".join(last["actual"]) or "nothing changed"
            assert f"actual:   {actual}" in follow_up
    assert mismatches > 0
    assert episode.predictions_checked - episode.predictions_correct == mismatches


def test_a_wrong_prediction_plays_nothing_after_it():
    game = GAMES["twins"]
    steps = _oracle_steps(game, 0, 3)
    wrong = [steps[0], (steps[1][0], [*steps[1][1], WRONG_CHANGE]), steps[2]]
    client = ScriptedClient([_predict(wrong)])
    episode = run_episode(game, 0, "predict", client, _ledger(), max_calls=1)
    entry = episode.transcript[0]
    assert entry["played"] == [steps[0][0], steps[1][0]]
    assert entry["outcome"] == "mismatch"
    assert episode.per_level[0].actions == 2
    assert (episode.predictions_checked, episode.predictions_correct) == (2, 1)


def test_an_empty_expectation_means_nothing_changes():
    game = GAMES["glide"]
    [(action, changes)] = _oracle_steps(game, 0, 1)
    again = [(action, changes), (action, [])]  # a slide that just hit a wall goes nowhere
    episode = run_episode(
        game, 0, "predict", ScriptedClient([_predict(again)]), _ledger(), max_calls=1
    )
    assert [p["match"] for p in episode.transcript[0]["predictions"]] == [True, True]
    assert episode.transcript[0]["changes"][1] == []


def test_adaptive_switches_to_plan_after_m_verified_predictions_and_back_each_level():
    game = GAMES["twins"]
    episode = run_episode(game, 0, "adaptive", OracleClient(game, 0), _ledger(), K=1, M=2)
    assert episode.stop_reason == "won"
    for level in (1, 2, 3, 4):
        modes = [e["mode"] for e in episode.transcript if e["level"] == level]
        n = optimal_actions(game, level, 0)
        assert modes == ["predict"] * min(n, 2) + ["plan"] * max(0, n - 2)
    reasons = [(s["level"], s["from"], s["to"], s["reason"]) for s in episode.mode_switches]
    assert reasons == [
        (1, "predict", "plan", "verified-streak"),
        (2, "plan", "predict", "new-level"),
        (2, "predict", "plan", "verified-streak"),
        (3, "plan", "predict", "new-level"),
        (3, "predict", "plan", "verified-streak"),
        (4, "plan", "predict", "new-level"),
        (4, "predict", "plan", "verified-streak"),
    ]
    switch_calls = [e["call"] for e in episode.transcript if e["mode_switches"]]
    assert switch_calls == sorted({s["call"] for s in episode.mode_switches})


def test_adaptive_switches_back_on_a_later_mismatch():
    game = GAMES["twins"]
    steps = _oracle_steps(game, 0, 4)
    first = [steps[0], steps[1], (steps[2][0], [WRONG_CHANGE])]
    client = ScriptedClient([_predict(first), _predict([steps[3]])])
    episode = run_episode(game, 0, "adaptive", client, _ledger(), K=4, M=2, max_calls=2)
    switches = [(s["call"], s["after_step"], s["to"], s["reason"]) for s in episode.mode_switches]
    assert switches == [(1, 2, "plan", "verified-streak"), (1, 3, "predict", "mismatch")]
    assert [e["mode"] for e in episode.transcript] == ["predict", "predict"]
    assert contract_of(client.prompts[1]) == ("predict", 4)
    assert episode.transcript[1]["played"] == [steps[3][0]]


def test_adaptive_never_switches_while_predictions_keep_failing():
    game = GAMES["glide"]
    client = NoisyPredictorClient(game, 0, wrong_rate=1.0)
    episode = run_episode(game, 0, "adaptive", client, _ledger(), M=1)
    assert episode.stop_reason == "won"
    assert episode.mode_switches == []
    assert {e["mode"] for e in episode.transcript} == {"predict"}
    assert all(len(e["played"]) == 1 for e in episode.transcript)


def test_a_credit_ceiling_stops_the_episode_as_limited():
    game = GAMES["twins"]
    client = OracleClient(game, 0, cost=1.0)
    ledger = _ledger(3.0)
    episode = run_episode(game, 0, "single", client, ledger)
    assert episode.stop_reason == "limited"
    assert episode.calls == 3
    assert client.budgets == [3.0, 2.0, 1.0]
    assert episode.usage.get("credits").value == 3.0
    assert ledger.spent.calls == 3


def test_a_call_may_overshoot_the_ceiling_and_the_next_one_is_refused():
    game = GAMES["twins"]
    episode = run_episode(game, 0, "single", OracleClient(game, 0, cost=2.5), _ledger(4.0))
    assert (episode.stop_reason, episode.calls) == ("limited", 2)
    assert episode.usage.get("credits").value == 5.0


def test_a_ledger_call_cap_is_limited_and_a_runner_cap_is_call_cap():
    game = GAMES["relay"]
    capped = run_episode(game, 0, "single", OracleClient(game, 0), Ledger(max_calls=2))
    assert (capped.stop_reason, capped.calls) == ("limited", 2)
    runner = run_episode(game, 0, "single", OracleClient(game, 0), _ledger(), max_calls=2)
    assert (runner.stop_reason, runner.calls) == ("call-cap", 2)
    nothing = run_episode(game, 0, "single", OracleClient(game, 0), _ledger(), max_calls=0)
    assert (nothing.stop_reason, nothing.calls, nothing.score) == ("call-cap", 0, 0.0)


def test_unknown_usage_stops_a_credit_capped_episode():
    game = GAMES["glide"]
    client = OracleClient(game, 0, unknown_usage=True)
    episode = run_episode(game, 0, "single", client, _ledger(100.0))
    assert (episode.stop_reason, episode.calls) == ("accounting-unknown", 1)
    assert episode.usage.get("credits").value is None


def test_unknown_usage_does_not_stop_an_episode_capped_only_by_calls():
    game = GAMES["glide"]
    client = OracleClient(game, 0, unknown_usage=True)
    episode = run_episode(game, 0, "plan", client, Ledger(max_calls=50))
    assert episode.stop_reason == "won"
    assert client.budgets == [None] * episode.calls


def test_a_partial_episode_scores_only_what_it_cleared():
    game = GAMES["relay"]
    episode = run_episode(game, 0, "plan", OracleClient(game, 0), _ledger(1.0))
    assert (episode.stop_reason, episode.levels_cleared) == ("limited", 1)
    assert [r.cleared for r in episode.per_level] == [True, False, False, False]
    assert episode.score == pytest.approx(0.1)


@pytest.mark.parametrize(
    ("arm", "reply"),
    [
        ("single", "I would like to move up."),
        ("single", '{"action": "jump"}'),
        ("single", '{"actions": ["up"]}'),
        ("single", '{"action": 3}'),
        ("plan", '{"actions": ["up", "fly"]}'),
        ("plan", '{"actions": []}'),
        ("plan", json.dumps({"actions": ["up"] * 13})),
        ("plan", '{"action": "up"}'),
        ("predict", '{"steps": [{"action": "up", "expect": ["one,two"]}]}'),
        ("predict", '{"steps": [{"action": "up"}]}'),
        ("predict", json.dumps({"steps": [{"action": a, "expect": []} for a in ("up", "dive")]})),
        ("predict", '{"steps": ["up"]}'),
        ("adaptive", '{"actions": ["up"]}'),
        ("plan", '{"actions": ["up"'),
    ],
)
def test_malformed_or_illegal_replies_play_nothing_and_count_as_invalid(arm: str, reply: str):
    game = GAMES["glide"]
    client = ScriptedClient([reply] * 3)
    episode = run_episode(game, 0, arm, client, _ledger())
    assert episode.stop_reason == "invalid-streak"
    assert (episode.calls, episode.invalid_calls) == (3, 3)
    assert episode.actions == 0
    for entry in episode.transcript:
        assert entry["outcome"] == "invalid"
        assert entry["played"] == []
        assert entry["parsed"] is None
    assert "rejected" in client.prompts[1]
    assert "nothing was played" in client.prompts[1]


def test_a_valid_reply_resets_the_invalid_streak():
    game = GAMES["glide"]
    replies = ["nope", "nope", '{"action": "up"}', "nope", "nope"]
    episode = run_episode(game, 0, "single", ScriptedClient(replies), _ledger(), invalid_streak=3)
    assert episode.stop_reason == "invalid-streak"
    assert (episode.calls, episode.invalid_calls) == (6, 5)
    assert episode.transcript[-1]["invalid_reason"] == "client-error: script exhausted"


def test_replies_are_found_inside_fences_and_prose_and_normalised():
    legal = ("up", "down", "press 1 2")
    fenced = 'Thinking...\n```json\n{"action": "UP"}\n```\nDone.'
    prose = 'My move is {"action": "press  1   2"} because of the lamps {x}.'
    assert parse_response(fenced, "single", legal, 1)[0].action == "up"
    assert parse_response(prose, "single", legal, 1)[0].action == "press 1 2"
    steps = parse_response(
        '{"steps": [{"action": "down", "expect": [" 2,1 = @", "0,0=.", "0,0=."]}]}',
        "predict",
        legal,
        12,
    )
    assert steps[0].expect == ("0,0=.", "2,1=@")
    assert extract_json("no braces here") is None
    assert extract_json("[1, 2]") is None


def test_client_errors_and_exceptions_are_invalid_calls_and_still_settled():
    class Exploding:
        def complete(self, prompt: str, *, max_credits: float | None) -> Completion:
            raise RuntimeError("boom")

    game = GAMES["relay"]
    ledger = _ledger(10.0)
    episode = run_episode(game, 0, "single", Exploding(), ledger)
    assert (episode.stop_reason, episode.calls, episode.invalid_calls) == (
        "accounting-unknown",
        1,
        1,
    )
    assert "client raised RuntimeError: boom" in episode.transcript[0]["invalid_reason"]
    assert ledger.spent.calls == 1
    assert ledger.spent.get("credits").value is None

    errored = ScriptedClient(
        [Completion('{"action": "press 0 0"}', Usage.of(credits=1), "timeout")]
    )
    episode = run_episode(game, 0, "single", errored, _ledger(), invalid_streak=1)
    assert episode.transcript[0]["outcome"] == "invalid"
    assert episode.actions == 0


def test_every_arm_shares_one_preamble_and_observation_format():
    game = GAMES["relay"]
    heads = set()
    for arm in ARMS:
        client = ScriptedClient([])
        run_episode(game, 0, arm, client, _ledger(), max_calls=1)
        prompt = client.prompts[0]
        assert prompt.startswith(PREAMBLE)
        heads.add(prompt.split("Response contract:")[0])
    assert len(heads) == 1


def test_prompts_show_a_bounded_history_and_a_summary_of_earlier_levels():
    game = GAMES["twins"]
    client = OracleClient(game, 0)
    episode = run_episode(game, 0, "single", client, _ledger(), window=2)
    first_level = optimal_actions(game, 1, 0)
    prompt = client.prompts[first_level + 3]
    history = prompt.split("Recent history")[1].split("\n\n")[0].splitlines()[1:]
    assert len(history) == 2
    assert f"Level 1: cleared in {first_level} actions." in prompt
    assert "Actions taken on this level so far: 3" in prompt
    assert episode.stop_reason == "won"


def test_the_transcript_and_json_record_every_call():
    game = GAMES["glide"]
    client = OracleClient(game, 2)
    episode = run_episode(game, 2, "adaptive", client, _ledger(), K=3, M=2)
    assert len(episode.transcript) == episode.calls == len(client.prompts)
    for entry, prompt in zip(episode.transcript, client.prompts, strict=True):
        assert entry["prompt_sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
        assert entry["prompt_length"] == len(prompt)
        assert set(entry) >= {"response", "parsed", "played", "changes", "outcome", "mode"}
        assert len(entry["changes"]) == len(entry["played"])
    data: dict[str, Any] = json.loads(json.dumps(episode.to_json()))
    assert data["stop_reason"] == "won"
    assert data["usage"] == {"calls": episode.calls, "amounts": {"credits": float(episode.calls)}}
    assert data["predictions"]["checked"] == episode.predictions_checked
    assert len(data["per_level"]) == 4
    assert data["settings"]["K"] == 3


def test_random_play_is_bounded_and_honest():
    game = GAMES["twins"]
    episode = run_episode(game, 0, "predict", RandomClient(4), _ledger(25.0))
    assert episode.stop_reason in ("limited", "won")
    assert episode.calls <= 25
    assert episode.invalid_calls == 0


def test_bad_parameters_and_unbounded_episodes_are_refused():
    game = GAMES["relay"]
    with pytest.raises(ValueError):
        run_episode(game, 0, "greedy", ScriptedClient([]), _ledger())
    with pytest.raises(ValueError):
        run_episode(game, 0, "plan", ScriptedClient([]), _ledger(), K=0)
    with pytest.raises(ValueError):
        run_episode(game, 0, "plan", ScriptedClient([]), Ledger())


def test_the_demo_runs_offline(capsys: pytest.CaptureFixture[str]):
    assert demo_main(["--game", "glide", "--arm", "adaptive", "--client", "noisy"]) == 0
    out = capsys.readouterr().out
    assert "stop: won" in out
    assert "level 4:" in out
