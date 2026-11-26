"""Play one episode with an offline client: `python -m flowproof.testbed --help`."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from flowproof.accounting import Ledger
from flowproof.testbed.clients import NoisyPredictorClient, OracleClient, RandomClient
from flowproof.testbed.games import GAMES
from flowproof.testbed.runner import ARMS, ModelClient, run_episode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m flowproof.testbed",
        description="Play one testbed episode with an offline client (no model, no network).",
    )
    parser.add_argument("--game", choices=sorted(GAMES), default="relay")
    parser.add_argument("--arm", choices=ARMS, default="predict")
    parser.add_argument("--client", choices=("oracle", "noisy", "random"), default="oracle")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--credits", type=float, default=200.0, help="ledger credit ceiling")
    parser.add_argument("--show-prompt", action="store_true", help="print the first prompt")
    parser.add_argument("--json", action="store_true", help="print the whole episode as JSON")
    args = parser.parse_args(argv)

    game = GAMES[args.game]
    client: OracleClient | RandomClient
    if args.client == "oracle":
        client = OracleClient(game, args.seed)
    elif args.client == "noisy":
        client = NoisyPredictorClient(game, args.seed, wrong_rate=0.3, noise_seed=args.seed)
    else:
        client = RandomClient(args.seed)
    model: ModelClient = client
    episode = run_episode(game, args.seed, args.arm, model, Ledger({"credits": args.credits}))

    if args.show_prompt and client.prompts:
        print(client.prompts[0])
    if args.json:
        print(json.dumps(episode.to_json(), indent=2, sort_keys=True))
        return 0
    cleared = f"{episode.levels_cleared}/{episode.levels_total}"
    predictions = f"{episode.predictions_correct}/{episode.predictions_checked}"
    print(f"game {episode.game}, seed {episode.seed}, arm {episode.arm}, client {args.client}")
    print(f"stop: {episode.stop_reason}; levels cleared {cleared}; score {episode.score:.3f}")
    print(
        f"calls {episode.calls} (invalid {episode.invalid_calls}); "
        f"usage {episode.usage.get('credits')}; predictions correct {predictions}; "
        f"mode switches {len(episode.mode_switches)}"
    )
    for result in episode.per_level:
        print(
            f"  level {result.level}: actions {result.actions}, optimal {result.optimal}, "
            f"cleared {result.cleared}, efficiency {result.efficiency:.3f}"
        )
    return 0
