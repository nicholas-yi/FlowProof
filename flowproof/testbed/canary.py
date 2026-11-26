"""Detect game-source canaries in anything a model saw or wrote."""

from __future__ import annotations

import json

from flowproof.testbed.games import CANARIES


def canary_hits(text: object) -> list[str]:
    """Names of games whose CANARY occurs in `text`, sorted.

    `text` may be a string or any JSON-serialisable value such as a transcript;
    an honest run always yields an empty list.
    """
    blob = text if isinstance(text, str) else json.dumps(text, default=str, sort_keys=True)
    return sorted(name for name, canary in CANARIES.items() if canary in blob)
