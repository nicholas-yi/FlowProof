"""Agent harnesses: the adapters between a workflow role and an agent product."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import AgentRequest, AgentResponse, Harness
from .copilot import CopilotHarness, parse_usage
from .mock import MockHarness, Reply

HARNESSES = ("mock", "copilot")


def load_script(path: Path) -> dict[str, list[Reply]]:
    """A mock script: a JSON object mapping each role to a list of replies."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(isinstance(v, list) for v in data.values()):
        raise ValueError(f"{path}: a mock script maps role names to lists of replies")
    return {str(role): [Reply.from_json(r) for r in replies] for role, replies in data.items()}


def make_harness(name: str, **options: Any) -> Harness:
    if name == "mock":
        script = options.get("script")
        return MockHarness(load_script(Path(script)) if script else None)
    if name == "copilot":
        return CopilotHarness(reasoning_effort=options.get("reasoning_effort"))
    raise ValueError(f"unknown harness {name!r}; expected one of: {', '.join(HARNESSES)}")


__all__ = [
    "HARNESSES",
    "AgentRequest",
    "AgentResponse",
    "CopilotHarness",
    "Harness",
    "MockHarness",
    "Reply",
    "load_script",
    "make_harness",
    "parse_usage",
]
