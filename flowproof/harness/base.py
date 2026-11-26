"""The interface every agent adapter implements."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..accounting import Usage
from ..outcomes import Outcome

MODES = ("answer", "edit")


@dataclass(frozen=True)
class AgentRequest:
    role: str
    prompt: str
    mode: str  # "answer": may read the workspace, must not change it; "edit": may change it
    workspace: Path | None
    model: str | None = None
    max_credits: float | None = None
    timeout_s: float = 900.0
    label: str = ""
    shell: bool = False  # the role may run commands itself during the call

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, not {self.mode!r}")
        if self.mode == "edit" and self.workspace is None:
            raise ValueError("an edit request needs a workspace")


@dataclass(frozen=True)
class AgentResponse:
    text: str
    usage: Usage
    outcome: Outcome
    raw: Mapping[str, Any] = field(default_factory=dict)


class Harness(Protocol):
    name: str
    reports: frozenset[str]  # units it can measure
    capabilities: frozenset[str]  # "answer", "edit", "shell"

    def invoke(self, request: AgentRequest) -> AgentResponse: ...
