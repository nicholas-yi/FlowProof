"""The registered testbed games, by name."""

from __future__ import annotations

from flowproof.testbed.engine import Game
from flowproof.testbed.games import glide, relay, twins

GAMES: dict[str, Game] = {
    "relay": relay.Relay(),
    "glide": glide.Glide(),
    "twins": twins.Twins(),
}

CANARIES: dict[str, str] = {
    "relay": relay.CANARY,
    "glide": glide.CANARY,
    "twins": twins.CANARY,
}

__all__ = ["CANARIES", "GAMES", "glide", "relay", "twins"]
