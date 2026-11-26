"""Use any harness as a testbed `ModelClient` (answer mode, no workspace, no tools)."""

from __future__ import annotations

from ..testbed.runner import Completion
from .base import AgentRequest, Harness


class HarnessClient:
    def __init__(self, harness: Harness, *, model: str | None = None, role: str = "player",
                 timeout_s: float = 300.0, label: str = "testbed"):
        self.harness = harness
        self.model = model
        self.role = role
        self.timeout_s = timeout_s
        self.label = label
        self.responses: list[dict[str, object]] = []

    def complete(self, prompt: str, *, max_credits: float | None) -> Completion:
        request = AgentRequest(role=self.role, prompt=prompt, mode="answer", workspace=None,
                               model=self.model, max_credits=max_credits,
                               timeout_s=self.timeout_s, label=self.label)
        response = self.harness.invoke(request)
        self.responses.append({"outcome": response.outcome.to_json(),
                               "raw": {k: v for k, v in response.raw.items()
                                       if k != "usage_file"}})
        error = None if response.outcome.is_ok else response.outcome.reason or "invalid"
        return Completion(response.text, response.usage, error)
