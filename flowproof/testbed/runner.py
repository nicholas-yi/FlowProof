"""Harness arms: the runner that lets a model play a testbed game.

The model sees only rendered frames, the cell changes its own actions caused,
and feedback on its replies. It never sees game source, so rules cannot leak
through the filesystem. All arms share one prompt preamble and observation
format; they differ only in the response contract and in how the runner plays
a reply:

- `single`: one action per call.
- `plan`: up to K actions per call, played until the level changes.
- `predict`: up to K (action, expected changes) steps, played until the first
  wrong prediction or a level change.
- `adaptive`: `predict` at the start of every level; after M consecutive
  verified predictions it switches to `plan` for the rest of the level, and a
  later wrong prediction or a new level switches it back.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from flowproof.accounting import AccountingUnknown, Ledger, LimitReached, Usage
from flowproof.testbed.engine import (
    Game,
    cell_changes,
    format_change,
    frame_text,
    parse_change,
)
from flowproof.testbed.score import LevelResult, episode_score
from flowproof.testbed.solve import optimal_actions

SINGLE = "single"
PLAN = "plan"
PREDICT = "predict"
ADAPTIVE = "adaptive"
ARMS = (SINGLE, PLAN, PREDICT, ADAPTIVE)
STOP_REASONS = ("won", "limited", "accounting-unknown", "call-cap", "invalid-streak")

PREAMBLE = (
    "You are playing a turn-based game on a grid of single-character cells. Nothing about "
    "its rules is given to you: work out how the world behaves by acting and watching what "
    "changes, then clear every level using as few actions as you can. Fewer actions score "
    "higher, later levels are worth more, and the rules may change from one level to the "
    "next, so keep checking your understanding.\n"
    "Rows are numbered on the left and columns along the top, both from 0. A cell change is "
    'written "r,c=g": the cell at row r, column c now shows glyph g.\n'
    "A reply that is not JSON in the required shape, or that names an action missing from "
    "the legal actions, is rejected and nothing is played."
)

_CONTRACT_RE = re.compile(
    r"^Response contract: (single|plan|predict)(?: \(at most (\d+) (?:actions|steps)\))?$",
    re.MULTILINE,
)
_LEGAL_RE = re.compile(r"^Legal actions: (.*)$", re.MULTILINE)
_FENCE_RE = re.compile(r"```[A-Za-z]*[ \t]*\n?(.*?)```", re.DOTALL)
_MAX_OBJECT_STARTS = 200  # bounds the `{` scan so a huge reply cannot stall the runner


@dataclass(frozen=True)
class Completion:
    text: str
    usage: Usage
    error: str | None = None


class ModelClient(Protocol):
    def complete(self, prompt: str, *, max_credits: float | None) -> Completion: ...


@dataclass(frozen=True)
class Step:
    """One action of a parsed reply; `expect` is None when the contract has no predictions."""

    action: str
    expect: tuple[str, ...] | None = None

    def to_json(self) -> dict[str, Any]:
        if self.expect is None:
            return {"action": self.action}
        return {"action": self.action, "expect": list(self.expect)}


class InvalidResponse(ValueError):
    """A reply that cannot be played; `reason` is a short machine-readable code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def contract_text(contract: str, limit: int) -> str:
    """The arm-specific end of a prompt; its first line names the contract."""
    if contract == SINGLE:
        return (
            "Response contract: single\n"
            'Reply with one JSON object and nothing else: {"action": "<one legal action>"}'
        )
    if contract == PLAN:
        return (
            f"Response contract: plan (at most {limit} actions)\n"
            "Reply with one JSON object and nothing else: "
            '{"actions": ["<legal action>", ...]}\n'
            f"List 1 to {limit} legal actions. They are played in order; play stops early if "
            "the level changes."
        )
    if contract == PREDICT:
        return (
            f"Response contract: predict (at most {limit} steps)\n"
            "Reply with one JSON object and nothing else:\n"
            '{"steps": [{"action": "<legal action>", "expect": ["r,c=g", ...]}, ...]}\n'
            f'List 1 to {limit} steps. For each step, "expect" lists every cell you expect '
            "that action to change, with its new glyph; use [] if you expect nothing to "
            "change. Steps are played in order until a prediction is wrong or the level "
            "changes, and you are then shown what actually changed."
        )
    raise ValueError(f"unknown contract {contract!r}")


def contract_of(prompt: str) -> tuple[str, int]:
    """The contract a prompt asks for and its step limit (1 for `single`)."""
    match = _CONTRACT_RE.search(prompt)
    if match is None:
        raise ValueError("prompt names no response contract")
    return match.group(1), int(match.group(2) or 1)


def legal_actions_of(prompt: str) -> tuple[str, ...]:
    """The legal actions listed in a prompt's current frame."""
    match = _LEGAL_RE.search(prompt)
    if match is None or match.group(1) == "(none)":
        return ()
    return tuple(match.group(1).split(", "))


def extract_json(text: str) -> dict[str, Any] | None:
    """The first JSON object in `text`: whole text, then fenced blocks, then any `{...}`."""
    candidates = [text.strip(), *(m.group(1).strip() for m in _FENCE_RE.finditer(text))]
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    decoder = json.JSONDecoder()
    starts = (index for index, char in enumerate(text) if char == "{")
    for _, index in zip(range(_MAX_OBJECT_STARTS), starts, strict=False):
        try:
            value, _ = decoder.raw_decode(text, index)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _clip(value: object, width: int = 60) -> str:
    text = repr(value)
    return text if len(text) <= width else text[: width - 3] + "..."


def _change_order(item: str) -> tuple[int, int, str]:
    return parse_change(item)


def _action(value: object, legal: frozenset[str]) -> str:
    if not isinstance(value, str):
        raise InvalidResponse("bad-action", f"actions are strings, got {_clip(value)}")
    action = " ".join(value.split()).lower()
    if action not in legal:
        raise InvalidResponse("illegal-action", _clip(value))
    return action


def _items(data: dict[str, Any], key: str, limit: int) -> list[Any]:
    items = data.get(key)
    if not isinstance(items, list):
        raise InvalidResponse(f"missing-{key}", f'expected a "{key}" list')
    if not items:
        raise InvalidResponse("empty", f'"{key}" is empty')
    if len(items) > limit:
        raise InvalidResponse("too-long", f"{len(items)} {key}, at most {limit} allowed")
    return items


def _step(item: object, legal: frozenset[str]) -> Step:
    if not isinstance(item, dict):
        raise InvalidResponse("bad-step", f"each step is an object, got {_clip(item)}")
    action = _action(item.get("action"), legal)
    expect = item.get("expect")
    if not isinstance(expect, list):
        raise InvalidResponse("missing-expect", f'step "{action}" has no "expect" list')
    changes: set[str] = set()
    for entry in expect:
        if not isinstance(entry, str):
            raise InvalidResponse("bad-expect", _clip(entry))
        try:
            changes.add(format_change(*parse_change(entry)))
        except ValueError:
            raise InvalidResponse("bad-expect", _clip(entry)) from None
    return Step(action, tuple(sorted(changes, key=_change_order)))


def parse_response(text: str, contract: str, legal: Sequence[str], limit: int) -> tuple[Step, ...]:
    """Leniently find the reply's JSON object, then strictly validate it against `contract`.

    Raises `InvalidResponse` for anything that cannot be played as a whole.
    """
    data = extract_json(text)
    if data is None:
        raise InvalidResponse("no-json", "no JSON object found")
    allowed = frozenset(legal)
    if contract == SINGLE:
        return (Step(_action(data.get("action"), allowed)),)
    if contract == PLAN:
        return tuple(Step(_action(item, allowed)) for item in _items(data, "actions", limit))
    if contract == PREDICT:
        return tuple(_step(item, allowed) for item in _items(data, "steps", limit))
    raise ValueError(f"unknown contract {contract!r}")


def _changes_text(changes: Sequence[str]) -> str:
    return ", ".join(changes) if changes else "nothing changed"


@dataclass(frozen=True)
class Episode:
    game: str
    seed: int
    arm: str
    levels_cleared: int
    levels_total: int
    per_level: list[LevelResult]
    calls: int
    invalid_calls: int
    usage: Usage
    stop_reason: str
    score: float
    mode_switches: list[dict[str, Any]]
    predictions_checked: int
    predictions_correct: int
    transcript: list[dict[str, Any]]
    settings: dict[str, Any]

    @property
    def actions(self) -> int:
        return sum(result.actions for result in self.per_level)

    def to_json(self) -> dict[str, Any]:
        return {
            "game": self.game,
            "seed": self.seed,
            "arm": self.arm,
            "levels_cleared": self.levels_cleared,
            "levels_total": self.levels_total,
            "per_level": [result.to_json() for result in self.per_level],
            "calls": self.calls,
            "invalid_calls": self.invalid_calls,
            "usage": self.usage.to_json(),
            "stop_reason": self.stop_reason,
            "score": self.score,
            "mode_switches": [dict(record) for record in self.mode_switches],
            "predictions": {
                "checked": self.predictions_checked,
                "correct": self.predictions_correct,
            },
            "transcript": [dict(entry) for entry in self.transcript],
            "settings": dict(self.settings),
        }


class _Run:
    """Mutable bookkeeping for one episode."""

    def __init__(
        self, game: Game, seed: int, arm: str, limit: int, streak_to_plan: int, window: int
    ) -> None:
        self.game = game
        self.seed = seed
        self.arm = arm
        self.limit = limit
        self.streak_to_plan = streak_to_plan
        self.window = window
        self.level = 1
        self.state: Any = game.start(1, seed)
        self.level_actions = 0
        self.cleared: list[LevelResult] = []
        self.history: deque[tuple[int, str, list[str]]] = deque(maxlen=window)
        self.feedback: list[str] = []
        self.mode = PREDICT if arm == ADAPTIVE else arm
        self.streak = 0
        self.calls = 0
        self.invalid_calls = 0
        self.invalid_run = 0
        self.usage = Usage.none()
        self.checked = 0
        self.correct = 0
        self.switches: list[dict[str, Any]] = []
        self.transcript: list[dict[str, Any]] = []
        self.won = False

    def prompt(self) -> str:
        sections = [
            PREAMBLE,
            "Current frame:\n"
            + frame_text(self.game.frame(self.state))
            + f"\nActions taken on this level so far: {self.level_actions}",
        ]
        earlier = [f"  Level {r.level}: cleared in {r.actions} actions." for r in self.cleared]
        sections.append("Earlier levels:\n" + ("\n".join(earlier) or "  none"))
        recent = [
            f'  level {level}, "{action}": {_changes_text(changes)}'
            for level, action, changes in self.history
        ]
        sections.append(
            f"Recent history (oldest first, at most {self.window} actions):\n"
            + ("\n".join(recent) or "  none")
        )
        if self.feedback:
            sections.append("Feedback on your last reply:\n" + "\n".join(self.feedback))
        sections.append(contract_text(self.mode, 1 if self.mode == SINGLE else self.limit))
        return "\n\n".join(sections) + "\n"

    def handle(self, prompt: str, contract: str, completion: Completion) -> None:
        self.calls += 1
        self.usage = self.usage + Usage(completion.usage.amounts, 1)
        entry: dict[str, Any] = {
            "call": self.calls,
            "level": self.level,
            "mode": contract,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "prompt_length": len(prompt),
            "response": completion.text,
            "error": completion.error,
            "usage": completion.usage.to_json(),
            "parsed": None,
            "played": [],
            "changes": [],
            "predictions": [],
            "mode_switches": [],
        }
        self.feedback = []
        legal = self.game.frame(self.state).actions
        try:
            if completion.error is not None:
                raise InvalidResponse("client-error", completion.error)
            steps = parse_response(completion.text, contract, legal, self._limit(contract))
        except InvalidResponse as exc:
            self.invalid_calls += 1
            self.invalid_run += 1
            entry["outcome"] = "invalid"
            entry["invalid_reason"] = str(exc)
            self.feedback.append(f"Your last reply was rejected ({exc}); nothing was played.")
        else:
            self.invalid_run = 0
            entry["parsed"] = [step.to_json() for step in steps]
            entry["outcome"] = self._play(steps, entry)
        self.transcript.append(entry)

    def _limit(self, contract: str) -> int:
        return 1 if contract == SINGLE else self.limit

    def _play(self, steps: Sequence[Step], entry: dict[str, Any]) -> str:
        for index, step in enumerate(steps, start=1):
            before = self.game.frame(self.state)
            if step.action not in before.actions:
                return "stopped"
            self.state = self.game.step(self.state, step.action)
            changes = cell_changes(before, self.game.frame(self.state))
            self.level_actions += 1
            entry["played"].append(step.action)
            entry["changes"].append(changes)
            self.history.append((self.level, step.action, changes))
            matched = step.expect is None or self._check(index, step, changes, entry)
            if self.game.solved(self.state):
                return self._clear_level(index, entry)
            if not matched:
                return "mismatch"
        return "played"

    def _check(self, index: int, step: Step, changes: list[str], entry: dict[str, Any]) -> bool:
        expected = list(step.expect or ())
        matched = expected == changes
        self.checked += 1
        entry["predictions"].append(
            {"step": index, "expected": expected, "actual": changes, "match": matched}
        )
        if matched:
            self.correct += 1
            self.streak += 1
            if self.arm == ADAPTIVE and self.mode == PREDICT and self.streak >= self.streak_to_plan:
                self._switch(PLAN, "verified-streak", index, entry)
            return True
        self.streak = 0
        self.feedback += [
            f'Your prediction for step {index} ("{step.action}") was wrong; play stopped there.',
            f"  expected: {_changes_text(expected)}",
            f"  actual:   {_changes_text(changes)}",
        ]
        if self.arm == ADAPTIVE and self.mode == PLAN:
            self._switch(PREDICT, "mismatch", index, entry)
        return False

    def _clear_level(self, index: int, entry: dict[str, Any]) -> str:
        optimal = optimal_actions(self.game, self.level, self.seed)
        self.cleared.append(LevelResult(self.level, self.level_actions, optimal, True))
        self.streak = 0
        if self.level == self.game.levels:
            self.won = True
            return "won"
        self.level += 1
        self.state = self.game.start(self.level, self.seed)
        self.level_actions = 0
        if self.arm == ADAPTIVE and self.mode == PLAN:
            self._switch(PREDICT, "new-level", index, entry)
        return "level-cleared"

    def _switch(self, to: str, reason: str, index: int, entry: dict[str, Any]) -> None:
        record = {
            "call": self.calls,
            "level": self.level,
            "after_step": index,
            "from": self.mode,
            "to": to,
            "reason": reason,
        }
        self.mode = to
        self.switches.append(record)
        entry["mode_switches"].append(record)

    def episode(self, stop_reason: str, settings: dict[str, Any]) -> Episode:
        per_level = list(self.cleared)
        if not self.won:
            for level in range(self.level, self.game.levels + 1):
                actions = self.level_actions if level == self.level else 0
                optimal = optimal_actions(self.game, level, self.seed)
                per_level.append(LevelResult(level, actions, optimal, False))
        return Episode(
            game=self.game.name,
            seed=self.seed,
            arm=self.arm,
            levels_cleared=len(self.cleared),
            levels_total=self.game.levels,
            per_level=per_level,
            calls=self.calls,
            invalid_calls=self.invalid_calls,
            usage=self.usage,
            stop_reason=stop_reason,
            score=episode_score(per_level, self.game.levels),
            mode_switches=self.switches,
            predictions_checked=self.checked,
            predictions_correct=self.correct,
            transcript=self.transcript,
            settings=settings,
        )


def _complete(client: ModelClient, prompt: str, max_credits: float | None) -> Completion:
    try:
        return client.complete(prompt, max_credits=max_credits)
    except Exception as exc:  # a broken client is an invalid call, never a crash mid-ledger
        return Completion("", Usage({}, 1), error=f"client raised {type(exc).__name__}: {exc}")


def run_episode(
    game: Game,
    seed: int,
    arm: str,
    client: ModelClient,
    ledger: Ledger,
    *,
    K: int = 12,
    M: int = 4,
    window: int = 6,
    max_calls: int | None = None,
    invalid_streak: int = 3,
) -> Episode:
    """Play `game` from level 1 until it is won, the ledger refuses, or a cap stops it.

    `K` bounds actions per call (`plan`, `predict`, `adaptive`), `M` is the
    verified-prediction streak after which `adaptive` switches to `plan`,
    `window` is how many recent (action, changes) records each prompt shows,
    `max_calls` caps this episode's calls, and `invalid_streak` consecutive
    invalid replies end it. Every call is admitted by `ledger` first and
    settled afterwards; the client is told the credits left.
    """
    if arm not in ARMS:
        raise ValueError(f"arm must be one of {', '.join(ARMS)}, got {arm!r}")
    if K < 1 or M < 1 or window < 0 or invalid_streak < 1:
        raise ValueError("K, M and invalid_streak must be positive and window non-negative")
    if max_calls is not None and max_calls < 0:
        raise ValueError("max_calls must be non-negative")
    if max_calls is None and ledger.max_calls is None and not ledger.ceilings:
        raise ValueError("an episode needs a bound: max_calls, a ledger call cap or a ceiling")
    settings: dict[str, Any] = {
        "K": K,
        "M": M,
        "window": window,
        "max_calls": max_calls,
        "invalid_streak": invalid_streak,
    }
    run = _Run(game, seed, arm, K, M, window)
    while True:
        if run.won:
            stop = "won"
            break
        if max_calls is not None and run.calls >= max_calls:
            stop = "call-cap"
            break
        try:
            ledger.admit()
        except LimitReached:
            stop = "limited"
            break
        except AccountingUnknown:
            stop = "accounting-unknown"
            break
        contract = run.mode
        prompt = run.prompt()
        completion = _complete(client, prompt, ledger.remaining("credits"))
        ledger.settle(Usage(completion.usage.amounts, 1))
        run.handle(prompt, contract, completion)
        if run.invalid_run >= invalid_streak:
            stop = "invalid-streak"
            break
    return run.episode(stop, settings)
