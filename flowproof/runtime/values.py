"""Run-time values of a workflow and how they render into prompts and reports."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..outcomes import Outcome

TAIL = 4000


class _Unknown:
    """A value that was not measured: neither true nor false, never zero."""

    _instance: _Unknown | None = None

    def __new__(cls) -> _Unknown:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNKNOWN"


UNKNOWN = _Unknown()


@dataclass(frozen=True)
class Unavailable:
    """The value of a branch or iteration that ended early, with the reason."""

    outcome: Outcome


@dataclass(frozen=True, eq=False)
class ChangeValue:
    """A change, the workspace it lives in, who made it and what they were asked."""

    change: Any  # flowproof.workspace.Change
    lease: Any  # flowproof.workspace.Lease
    author: str
    prompt: str

    def field(self, name: str) -> Any:
        if name == "patch":
            return self.change.patch
        if name == "files":
            return list(self.change.files)
        if name == "empty":
            return self.change.empty
        raise KeyError(name)


@dataclass(frozen=True)
class ResultValue:
    ok: bool | _Unknown
    exit: int | _Unknown
    out: str

    def field(self, name: str) -> Any:
        return {"ok": self.ok, "exit": self.exit, "out": self.out}[name]


@dataclass(frozen=True, eq=False)
class GradedValue:
    change: ChangeValue | Unavailable
    grade: Any | None  # flowproof.guards.Grade
    outcome: Outcome = field(default_factory=Outcome.ok)

    def field(self, name: str) -> Any:
        if name == "change":
            return self.change
        if name == "patch":
            return self.change.field("patch") if isinstance(self.change, ChangeValue) else UNKNOWN
        g = self.grade
        if g is None or not self.outcome.is_ok:
            return UNKNOWN
        if name == "passed":
            return _known(g.true_passed)
        if name == "claimed":
            return _known(g.claimed_passed)
        if name == "tampered":
            return g.tamper.tampered
        if name == "false_pass":
            return g.false_pass
        if name == "tests_passed":
            return _known(g.tests_passed)
        if name == "tests_failed":
            return _known(g.tests_failed)
        if name == "out":
            return tail(g.true.stdout + g.true.stderr)
        raise KeyError(name)


def _known(value: Any) -> Any:
    return UNKNOWN if value is None else value


def tail(text: str, limit: int = TAIL) -> str:
    return text if len(text) <= limit else "...\n" + text[-limit:]


def field_of(value: Any, name: str) -> Any:
    if isinstance(value, (Unavailable, _Unknown)):
        return value
    return value.field(name)


def number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def render(value: Any) -> str:
    """How a value appears when interpolated into a prompt or message."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return number(value)
    if isinstance(value, str):
        return value
    if value is UNKNOWN:
        return "unknown"
    if isinstance(value, Unavailable):
        return f"(unavailable: {value.outcome.reason or value.outcome.status.value})"
    if isinstance(value, ChangeValue):
        return value.change.patch or "(no changes)"
    if isinstance(value, ResultValue):
        return value.out
    if isinstance(value, GradedValue):
        passed = render(value.field("passed"))
        body = render(value.change)
        return f"frozen tests passed: {passed}\n{body}"
    if isinstance(value, Sequence):
        lines = []
        for item in value:
            text = render(item).replace("\n", "\n  ")
            lines.append(f"- {text}")
        return "\n".join(lines) if lines else "(none)"
    raise TypeError(f"cannot render {type(value).__name__}")


def to_json(value: Any) -> Any:
    """A JSON-safe description of a value for reports."""
    if value is UNKNOWN:
        return {"unknown": True}
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    if isinstance(value, Unavailable):
        return {"unavailable": value.outcome.to_json()}
    if isinstance(value, ChangeValue):
        return {"author": value.author, "files": list(value.change.files),
                "added": list(value.change.added), "deleted": list(value.change.deleted),
                "patch_bytes": len(value.change.patch.encode("utf-8"))}
    if isinstance(value, ResultValue):
        return {"ok": to_json(value.ok), "exit": to_json(value.exit), "out": tail(value.out, 800)}
    if isinstance(value, GradedValue):
        names = ("passed", "claimed", "tampered", "false_pass", "tests_passed", "tests_failed")
        return {"outcome": value.outcome.to_json(), "change": to_json(value.change),
                **{n: to_json(value.field(n)) for n in names}}
    if isinstance(value, Sequence):
        return [to_json(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): to_json(v) for k, v in value.items()}
    return repr(value)
