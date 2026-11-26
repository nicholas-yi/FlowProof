"""Role bindings: `role=harness[:model]`."""

from __future__ import annotations

import re
from dataclasses import dataclass

_BINDING = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=([a-z][a-z0-9-]*)(?::(\S+))?$")


@dataclass(frozen=True)
class Binding:
    role: str
    harness: str
    model: str | None = None

    def __str__(self) -> str:
        return f"{self.role}={self.harness}" + (f":{self.model}" if self.model else "")


def parse_binding(text: str) -> Binding:
    match = _BINDING.match(text.strip())
    if not match:
        raise ValueError(f"bad binding {text!r}; expected ROLE=HARNESS[:MODEL], "
                         "e.g. author=copilot:gpt-5.6-sol")
    return Binding(match.group(1), match.group(2), match.group(3))


def parse_bindings(texts: list[str]) -> dict[str, Binding]:
    out: dict[str, Binding] = {}
    for text in texts:
        binding = parse_binding(text)
        if binding.role in out:
            raise ValueError(f"role {binding.role!r} is bound twice")
        out[binding.role] = binding
    return out
