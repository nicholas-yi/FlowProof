"""A scripted harness for tests, demos and dry runs. It never contacts a model."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..accounting import Usage
from ..outcomes import Outcome
from .base import AgentRequest, AgentResponse


@dataclass(frozen=True)
class Reply:
    """One scripted response.

    `files` are written into the workspace for edit requests (a value of None
    deletes the file). `credits=None` reports the call's credits as unknown.
    `error` makes the call invalid with that reason.
    """

    text: str = ""
    credits: float | None = 0.0
    files: Mapping[str, str | None] = field(default_factory=dict)
    error: str | None = None

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> Reply:
        allowed = {"text", "credits", "files", "error"}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown reply field(s): {', '.join(sorted(unknown))}")
        credits = data.get("credits", 0.0)
        return Reply(
            text=str(data.get("text", "")),
            credits=None if credits is None else float(credits),
            files=dict(data.get("files", {})),
            error=data.get("error"),
        )


Script = Mapping[str, Sequence[Reply]] | Callable[[AgentRequest], Reply]


def _safe_target(root: Path, rel: str) -> Path:
    path = (root / rel).resolve()
    if rel.startswith(("/", "\\")) or ".." in Path(rel).parts or not path.is_relative_to(
        root.resolve()
    ):
        raise ValueError(f"scripted file {rel!r} escapes the workspace")
    return path


class MockHarness:
    """Replies from a per-role script (consumed in order) or a function of the request."""

    name = "mock"
    capabilities = frozenset({"answer", "edit", "shell"})

    def __init__(self, script: Script | None = None, *, reports: Sequence[str] = ("credits",),
                 default: Reply | None = None):
        self.script = script
        self.reports = frozenset(reports)
        self.default = default
        self.requests: list[AgentRequest] = []
        self._cursor: dict[str, int] = {}
        self._lock = threading.Lock()

    def _next(self, request: AgentRequest) -> Reply:
        if callable(self.script):
            return self.script(request)
        replies = (self.script or {}).get(request.role, ())
        with self._lock:
            index = self._cursor.get(request.role, 0)
            self._cursor[request.role] = index + 1
        if index < len(replies):
            return replies[index]
        if self.default is not None:
            return self.default
        return Reply(error="script-exhausted")

    def invoke(self, request: AgentRequest) -> AgentResponse:
        with self._lock:
            self.requests.append(request)
        reply = self._next(request)
        usage = (Usage.unknown(*self.reports) if reply.credits is None
                 else Usage.of(credits=reply.credits))
        if reply.error is not None:
            return AgentResponse(reply.text, usage, Outcome.invalid(reply.error))
        if reply.files:
            if request.mode != "edit" or request.workspace is None:
                return AgentResponse(reply.text, usage, Outcome.invalid("write-in-answer-mode"))
            for rel, content in reply.files.items():
                target = _safe_target(request.workspace, rel)
                if content is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content.encode("utf-8"))
        return AgentResponse(reply.text, usage, Outcome.ok(), {"scripted": True})
