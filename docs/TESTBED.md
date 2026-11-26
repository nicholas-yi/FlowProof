# Testbed

`flowproof/testbed/` measures how well an agent learns hidden rules from
observation alone, and how much the way it is allowed to act (the harness
*arm*) changes cost and efficiency. Each game has four levels, and its rules
change from one level to the next, so an agent has to keep checking what it
thinks it knows.

## What the model can and cannot see

The runner is the only path between a game and a model. Each prompt contains:

- one shared preamble: the task is to work out how the world behaves and clear
  every level in as few actions as possible;
- the current frame: level, status, the grid with 0-based row and column
  numbers, and the legal actions;
- how many actions have been taken on the current level;
- a summary of earlier levels (level number and actions used);
- the last `window` actions with the cells each one changed (`"r,c=g"`);
- feedback on the previous reply (why it was rejected, or a wrong prediction
  next to what actually changed);
- the response contract for the arm.

No prompt names the game or the seed, states a rule or the goal, or includes a
legend. The model never gets file access or game source. Each game module
defines a random hex `CANARY` that never appears in frames or prompts;
`canary_hits(text_or_transcript)` returns the names of games whose canary
appears. A hit in a model's output means the model saw game source.

## Games

Every level is at most 9x9, deterministic for a given `(level, seed)`, and
checked solvable by breadth-first search. Glyphs are single printable ASCII
characters.

| Game | Board | Level 1 | Level 2 | Level 3 | Level 4 |
|---|---|---|---|---|---|
| `relay` | lamps `o` off, `*` on; action `press R C`; goal: every lamp on | press toggles a plus shape | the four diagonals and the pressed lamp | knight-move cells and the pressed lamp | the rest of the pressed lamp's row |
| `glide` | avatar `@` on ice slides until a wall `#`; goal: come to rest on `G` | plain sliding | `+` stops a slide that enters it | `>` `<` `^` `v` turn the slide that way | `~` reverses the slide, at most once per slide |
| `twins` | avatars `A`, `B`; goal: `A` on `a` while `B` is on `b` | B copies A | B mirrors left/right | B mirrors up/down | B copies A only on every second action of the level |

How seeds choose a layout:

- **Relay** boards are 3x3 on level 1 and 4x4 after that. A seed picks a board
  from a seeded shuffle of every board whose optimal solution needs 2 to 3
  presses (level 1) or 3 to 4 presses (levels 2 to 4). Every board is reached
  by pressing lamps from the all-on board, so it is always solvable, and seeds
  0 to N-1 never repeat a board.
- **Glide** (7x7 to 9x9) and **twins** (7x7 to 9x9) draw walls, tiles and
  positions from a random stream seeded by `"<game>:<level>:<seed>"`. A layout
  is redrawn until it is solvable and needs at least 3 moves (glide) or 4 to
  24 moves (twins). On levels 2 to 4, the level 1 rule's optimal solution must
  also fail to solve the layout. Glide level 3 layouts also may not contain a
  slide that loops forever.
- **Twins** avatars block each other: a move that would put both avatars in
  one cell, or swap them, leaves both where they were.

Per-module helpers `state_from_rows(level, rows)` build hand-drawn states for
tests and demos.

## Arms and response contracts

Replies must be JSON. The runner finds the object leniently (a bare object, a
fenced block, or an object inside prose) but validates it strictly: an unknown
or illegal action, a wrong shape, an empty list, more than `K` items or a
malformed `expect` item makes the whole call invalid, and nothing is played.
Action words are compared after collapsing whitespace and lower-casing.

| Arm | Reply | Runner behaviour |
|---|---|---|
| `single` | `{"action": "up"}` | plays one action |
| `plan` | `{"actions": ["up", "left"]}`, at most `K` | plays them all; a level change ends the call |
| `predict` | `{"steps": [{"action": "up", "expect": ["2,3=@", "4,3=."]}]}`, at most `K` | plays steps until the first prediction that differs from the observed `cell_changes` (that step is still played), or a level change. `[]` means "nothing changes". The next prompt shows the expected and actual changes. |
| `adaptive` | `predict` replies, then `plan` replies | starts each level in `predict`. After `M` consecutive verified predictions on a level it switches to `plan` for the rest of the level. A later wrong prediction (possible only among the remaining steps of the call that triggered the switch) or a new level switches it back. Each switch is recorded with its call, level, step and reason (`verified-streak`, `mismatch`, `new-level`). |

## Running an episode

```python
from flowproof.accounting import Ledger
from flowproof.testbed.games import GAMES
from flowproof.testbed.runner import run_episode

episode = run_episode(GAMES["glide"], 0, "predict", client, Ledger({"credits": 50}),
                      K=12, M=4, window=6, max_calls=None, invalid_streak=3)
```

`client` implements `ModelClient.complete(prompt, *, max_credits) -> Completion`.
Before each call the runner calls `ledger.admit()`. `LimitReached` stops the
episode with `"limited"` and `AccountingUnknown` stops it with
`"accounting-unknown"`. The client is passed
`max_credits=ledger.remaining("credits")`. After the call the runner always
calls `ledger.settle`, counting exactly one call. A client that raises, or that
returns a `Completion` with an `error`, is recorded as an invalid call. Its
spend is unknown, so a credits-capped episode then stops.

The other stop reasons are `"won"`, `"call-cap"` (the episode reached
`max_calls`) and `"invalid-streak"` (`invalid_streak` consecutive invalid
calls). `run_episode` refuses to start an unbounded episode, one with no
`max_calls`, no ledger call cap and no ledger ceiling.

`Episode` records the game, seed, arm, levels cleared and total, `per_level`
(`LevelResult`: level, actions, optimal, cleared, efficiency), calls, invalid
calls, usage, stop reason, score, mode switches, predictions checked and
correct, the settings used, and a transcript. Each transcript entry holds the
prompt's SHA-256 and length, the response text, the parsed reply, the actions
played, the observed changes, the prediction checks, any mode switches and the
outcome (`played`, `mismatch`, `level-cleared`, `won` or `invalid` with a
reason). `Episode.to_json()` returns plain JSON data.

## Scoring

- Efficiency of a cleared level is `min(1, optimal / actions)`. `optimal`
  comes from `optimal_actions(game, level, seed)`, a cached breadth-first
  search over `game.key(state)`.
- The episode score is the level-weighted mean, where level `l` has weight `l`.
  Levels that were not cleared score 0. For four levels the score is
  `sum(l * eff_l) / 10`.

## Offline clients and demo

`flowproof.testbed.clients` provides the following clients. Each one charges
`cost` credits per call, or unknown usage with `unknown_usage=True`, and
records `prompts` and `budgets`.

- `OracleClient(game, seed)`: uses BFS to play optimally and predicts every
  change exactly.
- `NoisyPredictorClient(game, seed, wrong_rate=..., noise_seed=...)`: plays the
  oracle's actions, but each prediction is wrong with probability `wrong_rate`.
- `RandomClient(seed)`: plays random legal actions.
- `ScriptedClient(responses)`: returns fixed replies.

The oracle knows the game and seed. It is a reference for the runner and the
scoring code, not a model.

```powershell
.venv\Scripts\python.exe -m flowproof.testbed --game twins --arm adaptive --client noisy --seed 3
.venv\Scripts\python.exe -m flowproof.testbed --game relay --arm predict --show-prompt
.venv\Scripts\python.exe -m flowproof.testbed --game glide --arm plan --client random --json
```

The demo runs offline with a ledger credit ceiling (`--credits`, default 200).

## Validation

```powershell
.venv\Scripts\python.exe -m pytest tests/testbed
.venv\Scripts\ruff.exe check flowproof/testbed tests/testbed
.venv\Scripts\pyright.exe flowproof/testbed
```

These are local, offline checks of the games, solver, runner and scoring. They
do not measure any real model.
