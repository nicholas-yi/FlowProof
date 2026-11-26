"""Typed outcomes: a wrong answer is never confused with a broken run."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    """How a stage, a call or a whole run ended."""

    OK = "ok"
    FAILED = "failed"
    INVALID = "invalid"
    LIMITED = "limited"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Outcome:
    """A status plus a short machine-readable reason and optional detail.

    `FAILED` is a real negative: the work ran and did not achieve the task.
    `INVALID` means the result cannot be trusted either way (a timeout, a
    malformed response, an unknown cost under a cap, a guard violation), and
    must be reported separately rather than counted as a failure.
    """

    status: Status
    reason: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, reason: str = "", **detail: Any) -> Outcome:
        return cls(Status.OK, reason, detail)

    @classmethod
    def failed(cls, reason: str, **detail: Any) -> Outcome:
        return cls(Status.FAILED, reason, detail)

    @classmethod
    def invalid(cls, reason: str, **detail: Any) -> Outcome:
        return cls(Status.INVALID, reason, detail)

    @classmethod
    def limited(cls, reason: str, **detail: Any) -> Outcome:
        return cls(Status.LIMITED, reason, detail)

    @classmethod
    def cancelled(cls, reason: str = "cancelled", **detail: Any) -> Outcome:
        return cls(Status.CANCELLED, reason, detail)

    @property
    def is_ok(self) -> bool:
        return self.status is Status.OK

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status.value, "reason": self.reason, "detail": dict(self.detail)}
