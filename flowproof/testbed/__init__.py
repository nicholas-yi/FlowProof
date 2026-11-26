"""Rule-learning grid games and the harness arms that let a model play them.

See docs/TESTBED.md. Games live in `flowproof.testbed.games`; the runner in
`flowproof.testbed.runner` is the only path between a game and a model.
"""

from __future__ import annotations

from flowproof.testbed.canary import canary_hits
from flowproof.testbed.engine import Frame, Game, cell_changes, frame_text
from flowproof.testbed.games import GAMES
from flowproof.testbed.runner import ARMS, Completion, Episode, ModelClient, run_episode
from flowproof.testbed.score import LevelResult, episode_score, level_efficiency
from flowproof.testbed.solve import optimal_actions, shortest_path

__all__ = [
    "ARMS",
    "GAMES",
    "Completion",
    "Episode",
    "Frame",
    "Game",
    "LevelResult",
    "ModelClient",
    "canary_hits",
    "cell_changes",
    "episode_score",
    "frame_text",
    "level_efficiency",
    "optimal_actions",
    "run_episode",
    "shortest_path",
]
