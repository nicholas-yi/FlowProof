"""Mermaid flowcharts of workflows."""

from __future__ import annotations

from itertools import count

from . import printer
from .tree import (
    Ask,
    Choose,
    Do,
    Each,
    Edit,
    Exec,
    Fork,
    Give,
    If,
    Let,
    Note,
    Repeat,
    Require,
    Set,
    Stmt,
    Test,
    Value,
    Var,
    Workflow,
)


def _label(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return (text.replace("&", "#amp;").replace('"', "#quot;").replace("<", "#lt;")
            .replace(">", "#gt;"))


def _value_label(v: Value) -> str:
    if isinstance(v, Ask):
        return f"ask {v.role}" + ("" if v.kind == "text" else f" as {v.kind}")
    if isinstance(v, Edit):
        return f"edit {v.role}"
    if isinstance(v, Exec):
        return f"exec {v.command.text() if v.command.literal else '...'}"
    if isinstance(v, Test):
        frozen = ", ".join(f.text() for f in v.frozen if f.literal)
        return f"test {printer.expr(v.subject)}" + (f" (frozen {frozen})" if frozen else "")
    if isinstance(v, Choose):
        chain = ", then ".join(
            f"pairwise {p.role}" if p.name == "pairwise" else p.name for p in v.policies
        )
        return f"choose {printer.expr(v.subject)} by {chain}"
    return printer.expr(v)


class _Graph:
    def __init__(self) -> None:
        self.ids = count(1)
        self.lines: list[str] = []
        self.edges: list[str] = []

    def node(self, label: str, depth: int, shape: str = "box") -> str:
        ident = f"n{next(self.ids)}"
        left, right = {"box": ("[", "]"), "round": ("(", ")"), "stadium": ("([", "])"),
                       "diamond": ("{", "}"), "hex": ("{{", "}}")}[shape]
        self.lines.append(f'{"  " * depth}{ident}{left}"{_label(label)}"{right}')
        return ident

    def chain(self, body: tuple[Stmt, ...], depth: int) -> tuple[str, str] | None:
        first: str | None = None
        last: str | None = None
        for stmt in body:
            entry, exit_ = self.statement(stmt, depth)
            if last is not None:
                self.edges.append(f"{last} --> {entry}")
            first = first or entry
            last = exit_
        return None if first is None or last is None else (first, last)

    def group(self, title: str, body: tuple[Stmt, ...], depth: int, tail: str | None) -> str:
        ident = f"g{next(self.ids)}"
        pad = "  " * depth
        self.lines.append(f'{pad}subgraph {ident}["{_label(title)}"]')
        inner = self.chain(body, depth + 1)
        if tail is not None:
            keep = self.node(tail, depth + 1, "round")
            if inner is not None:
                self.edges.append(f"{inner[1]} --> {keep}")
        elif inner is None:
            self.node("(empty)", depth + 1, "round")
        self.lines.append(f"{pad}end")
        return ident

    def statement(self, s: Stmt, depth: int) -> tuple[str, str]:
        if isinstance(s, (Let, Var, Set)):
            shape = "hex" if isinstance(s.value, (Test, Choose)) else "box"
            word = {Let: "", Var: "var ", Set: "set "}[type(s)]
            ident = self.node(f"{word}{s.name} = {_value_label(s.value)}", depth, shape)
            return ident, ident
        if isinstance(s, Do):
            ident = self.node(_value_label(s.action), depth)
            return ident, ident
        if isinstance(s, Fork):
            mode = " isolated" if s.isolated else ""
            ident = self.group(f"fork {s.count}{mode} into {s.into}", s.body, depth,
                               f"keep {printer.expr(s.keep)}")
            return ident, ident
        if isinstance(s, Each):
            title = f"for each {s.var} in {printer.expr(s.source)}, at most {s.limit}"
            ident = self.group(f"{title}, into {s.into}", s.body, depth,
                               f"keep {printer.expr(s.keep)}")
            return ident, ident
        if isinstance(s, Repeat):
            ident = self.group(f"repeat at most {s.limit} until {printer.expr(s.until)}",
                               s.body, depth, None)
            self.edges.append(f"{ident} -. again .-> {ident}")
            return ident, ident
        if isinstance(s, If):
            test = self.node(f"if {printer.expr(s.cond)}", depth, "diamond")
            join = self.node(" ", depth, "round")
            for label, block in (("yes", s.then), ("no", s.orelse or ())):
                inner = self.chain(block, depth)
                if inner is None:
                    self.edges.append(f"{test} -->|{label}| {join}")
                else:
                    self.edges.append(f"{test} -->|{label}| {inner[0]}")
                    self.edges.append(f"{inner[1]} --> {join}")
            return test, join
        if isinstance(s, Require):
            ident = self.node(f"require {printer.expr(s.cond)}", depth, "diamond")
            return ident, ident
        if isinstance(s, Note):
            ident = self.node(f"note {s.message.text() if s.message.literal else '...'}",
                              depth, "round")
            return ident, ident
        assert isinstance(s, Give)
        ident = self.node(f"give {printer.expr(s.value)}", depth, "stadium")
        return ident, ident


def mermaid(workflow: Workflow) -> str:
    """A Mermaid `flowchart TD` of the workflow's statements."""
    graph = _Graph()
    start = graph.node(f"workflow {workflow.name}", 1, "stadium")
    inner = graph.chain(workflow.body, 1)
    if inner is not None:
        graph.edges.append(f"{start} --> {inner[0]}")
    return "\n".join(["flowchart TD", *graph.lines, *("  " + e for e in graph.edges)]) + "\n"
