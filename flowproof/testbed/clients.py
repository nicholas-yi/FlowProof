"""Offline model clients for tests and demos.

Each client reports `Usage.of(credits=cost)` per call, or unknown credits when
built with `unknown_usage=True`, and records every prompt and the credit cap it
was offered in `prompts` and `budgets`.
"""

from __future__ import annotations

import json
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from flowproof.accounting import Usage
from flowproof.testbed.engine import Game, cell_changes, frame_text
from flowproof.testbed.runner import (
    PLAN,
    PREDICT,
    SINGLE,
    Completion,
    contract_of,
    legal_actions_of,
)
from flowproof.testbed.solve import shortest_path

# No game draws `?`, so a prediction containing this change is always wrong.
WRONG_CHANGE = "0,0=?"


class _Client:
    def __init__(self, cost: float, unknown_usage: bool) -> None:
        if cost < 0:
            raise ValueError("cost must be non-negative")
        self.cost = cost
        self.unknown_usage = unknown_usage
        self.prompts: list[str] = []
        self.budgets: list[float | None] = []

    def _record(self, prompt: str, max_credits: float | None) -> Usage:
        self.prompts.append(prompt)
        self.budgets.append(max_credits)
        return Usage.unknown("credits") if self.unknown_usage else Usage.of(credits=self.cost)


def _reply(contract: str, actions: Sequence[str], expects: Sequence[Sequence[str]]) -> str:
    if contract == SINGLE:
        return json.dumps({"action": actions[0]})
    if contract == PLAN:
        return json.dumps({"actions": list(actions)})
    steps = [{"action": a, "expect": list(e)} for a, e in zip(actions, expects, strict=True)]
    return json.dumps({"steps": steps})


@dataclass(frozen=True)
class _Planned:
    action: str
    changes: tuple[str, ...]
    after: Any


class OracleClient(_Client):
    """Plays optimally by breadth-first search and predicts every change exactly.

    It knows the game and seed (it is not a model) and tracks the state itself,
    assuming the runner plays its replies as the contracts promise. If the
    prompt's frame stops matching its own, it answers with an error instead.
    """

    def __init__(
        self, game: Game, seed: int, *, cost: float = 1.0, unknown_usage: bool = False
    ) -> None:
        super().__init__(cost, unknown_usage)
        self.game = game
        self.seed = seed
        self._level = 1
        self._state: Any = game.start(1, seed)
        self._path: list[str] = []

    def complete(self, prompt: str, *, max_credits: float | None) -> Completion:
        usage = self._record(prompt, max_credits)
        if frame_text(self.game.frame(self._state)) not in prompt:
            return Completion("", usage, error="oracle-desync")
        contract, limit = contract_of(prompt)
        planned = self._lookahead(limit)
        expects, played = [list(p.changes) for p in planned], len(planned)
        if contract == PREDICT:
            expects, played = self._expectations(planned)
        self._advance(planned[:played])
        return Completion(_reply(contract, [p.action for p in planned], expects), usage)

    def _lookahead(self, limit: int) -> list[_Planned]:
        if not self._path:
            path = shortest_path(self.game, self._state)
            if not path:
                raise RuntimeError("oracle has nothing to play")
            self._path = path
        planned: list[_Planned] = []
        state = self._state
        for action in self._path[:limit]:
            after = self.game.step(state, action)
            changes = cell_changes(self.game.frame(state), self.game.frame(after))
            planned.append(_Planned(action, tuple(changes), after))
            state = after
            if self.game.solved(state):
                break
        return planned

    def _expectations(self, planned: Sequence[_Planned]) -> tuple[list[list[str]], int]:
        return [list(p.changes) for p in planned], len(planned)

    def _advance(self, played: Sequence[_Planned]) -> None:
        self._state = played[-1].after
        self._path = self._path[len(played) :]
        if self.game.solved(self._state) and self._level < self.game.levels:
            self._level += 1
            self._state = self.game.start(self._level, self.seed)
            self._path = []


class NoisyPredictorClient(OracleClient):
    """Plays the oracle's actions, but each prediction is wrong with probability `wrong_rate`.

    `wrong_at` records, per predict-contract call, the 1-based index of the first
    wrong step (None when every prediction was right).
    """

    def __init__(
        self,
        game: Game,
        seed: int,
        *,
        wrong_rate: float = 0.25,
        noise_seed: int = 0,
        cost: float = 1.0,
        unknown_usage: bool = False,
    ) -> None:
        if not 0.0 <= wrong_rate <= 1.0:
            raise ValueError("wrong_rate must be within [0, 1]")
        super().__init__(game, seed, cost=cost, unknown_usage=unknown_usage)
        self.wrong_rate = wrong_rate
        self._rng = random.Random(noise_seed)
        self.wrong_at: list[int | None] = []

    def _expectations(self, planned: Sequence[_Planned]) -> tuple[list[list[str]], int]:
        expects: list[list[str]] = []
        first_wrong: int | None = None
        for index, step in enumerate(planned, start=1):
            if self._rng.random() < self.wrong_rate:
                expects.append([*step.changes, WRONG_CHANGE])
                if first_wrong is None:
                    first_wrong = index
            else:
                expects.append(list(step.changes))
        self.wrong_at.append(first_wrong)
        return expects, first_wrong or len(planned)


class RandomClient(_Client):
    """Picks legal actions uniformly at random and predicts that nothing changes."""

    def __init__(self, seed: int, *, cost: float = 1.0, unknown_usage: bool = False) -> None:
        super().__init__(cost, unknown_usage)
        self._rng = random.Random(seed)

    def complete(self, prompt: str, *, max_credits: float | None) -> Completion:
        usage = self._record(prompt, max_credits)
        contract, limit = contract_of(prompt)
        legal = legal_actions_of(prompt)
        if not legal:
            return Completion("{}", usage)
        count = 1 if contract == SINGLE else self._rng.randint(1, limit)
        actions = [self._rng.choice(legal) for _ in range(count)]
        return Completion(_reply(contract, actions, [[] for _ in actions]), usage)


class ScriptedClient(_Client):
    """Returns the given replies in order, then an error for every further call.

    A reply given as a `Completion` is returned unchanged (with its own usage).
    """

    def __init__(
        self,
        responses: Sequence[str | Completion],
        *,
        cost: float = 1.0,
        unknown_usage: bool = False,
    ) -> None:
        super().__init__(cost, unknown_usage)
        self._responses = list(responses)
        self._next = 0

    def complete(self, prompt: str, *, max_credits: float | None) -> Completion:
        usage = self._record(prompt, max_credits)
        if self._next >= len(self._responses):
            return Completion("", usage, error="script exhausted")
        reply = self._responses[self._next]
        self._next += 1
        return reply if isinstance(reply, Completion) else Completion(reply, usage)
