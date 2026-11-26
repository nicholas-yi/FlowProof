from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flowproof.lang import SyntaxProblem, format_workflow, parse
from flowproof.lang.lexer import KEYWORDS
from flowproof.lang.tree import (
    Ask,
    BinOp,
    Bool,
    Choose,
    Do,
    Each,
    Edit,
    Exec,
    Field,
    Fork,
    Give,
    If,
    Len,
    Let,
    Limit,
    Name,
    Not,
    Note,
    Num,
    Param,
    Policy,
    Ref,
    Repeat,
    Require,
    Role,
    Set,
    Str,
    Template,
    Var,
    Workflow,
)
from flowproof.lang.tree import Test as RunTests

EXAMPLES = sorted((Path(__file__).parents[2] / "examples").glob("*.fp"))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_examples_round_trip(path: Path):
    source = path.read_text(encoding="utf-8")
    wf = parse(source, str(path))
    printed = format_workflow(wf)
    assert parse(printed) == wf
    assert format_workflow(parse(printed)) == printed


def test_triple_quoted_strings_dedent_and_interpolate():
    wf = parse('workflow w\n  role a can answer\n  let x = ask a: """\n    one {y.z}\n'
               '      two\n    """\nend\n')
    let = wf.body[0]
    assert isinstance(let, Let) and isinstance(let.value, Ask)
    assert let.value.prompt.parts == ("one ", Ref(("y", "z")), "\n  two")


def test_escapes_and_literal_braces():
    wf = parse('workflow w\n  note "a\\"b {{c}} \\\\ \\n"\nend\n')
    note = wf.body[0]
    assert isinstance(note, Note)
    assert note.message.parts == ('a"b {c} \\ \n',)


def test_crlf_source_is_accepted():
    source = "workflow w\r\n  note \"hi\"\r\nend\r\n"
    assert parse(source) == parse(source.replace("\r\n", "\n"))


@pytest.mark.parametrize(
    ("source", "code", "fragment", "line"),
    [
        ("workflow w\n  note \"x\"\n", "E100", "missing 'end'", 3),
        ("workflow w\n  let end = 1\nend\n", "E100", "keyword", 2),
        ("workflow w\n  fork 2 into xs\n    note \"x\"\n  end\nend\n", "E215", "keep", 4),
        ("workflow w\n  fork 2 into xs\n    keep 1\n    note \"x\"\n  end\nend\n", "E215",
         "last line", 4),
        ("workflow w\n  keep 1\nend\n", "E215", "only allowed", 2),
        ("workflow w\n  give 1 < 2 < 3\nend\n", "E100", "chained", 2),
        ("workflow w\n  note \"open\nend\n", "E100", "unterminated", 2),
        ("workflow w\n  note \"x\"\n  role a can answer\nend\n", "E100", "before the first", 3),
        ("workflow w\n  note \"\\q\"\nend\n", "E100", "escape", 2),
        ("workflow w\n  note \"{a b}\"\nend\n", "E100", "interpolate", 2),
        ("workflow w\n  note \"}\"\nend\n", "E100", "unmatched", 2),
        ("workflow w\n  fork 1.5 into xs\n    keep 1\n  end\nend\n", "E100", "whole number", 2),
        ("workflow w\n  test xs with \"t\"\nend\n", "E100", "let", 2),
        ("workflow w\nend\nworkflow v\nend\n", "E100", "after the workflow", 3),
        ("workflow w\n  give 3x\nend\n", "E100", "malformed number", 2),
        ("workflow w\n  give @\nend\n", "E100", "unexpected character", 2),
    ],
)
def test_syntax_errors_are_located(source: str, code: str, fragment: str, line: int):
    with pytest.raises(SyntaxProblem) as info:
        parse(source)
    assert info.value.code == code
    assert fragment in info.value.message
    assert info.value.pos.line == line


def test_newlines_inside_parentheses_continue_the_line():
    wf = parse("workflow w\n  give (true and\n    false)\nend\n")
    give = wf.body[0]
    assert isinstance(give, Give) and isinstance(give.value, BinOp)


def test_formatting_preserves_operator_grouping():
    wf = parse("workflow w\n  give not (a or b) and (c == d) == e\nend\n")
    printed = format_workflow(wf)
    assert "give not (a or b) and (c == d) == e" in printed
    assert parse(printed) == wf


COMMENTED = """# before the workflow
workflow w  # on the workflow line
  # before a header
  role a can answer  # after a header
  budget at most 5 calls

  # before a statement
  let x = ask a: \"\"\"
    multi # not a comment: inside a string
    \"\"\"  # after a closing quote
  if x == "y"  # after an if
    note "yes"
    # before else
  else  # after else
    note "no"
    # before end
  end  # after end

  fork 2 into xs
    let y = ask a: "{x}"
    # before keep
    keep y  # after keep
  end
  give xs
  # before the final end
end  # after the final end
# after everything
"""


def test_comments_survive_formatting():
    wf = parse(COMMENTED)
    printed = format_workflow(wf)
    for line in COMMENTED.splitlines():
        if "#" in line and "not a comment" not in line:
            comment = line[line.index("#"):]
            assert comment in printed, comment
    assert "multi # not a comment: inside a string" in printed
    assert format_workflow(parse(printed)) == printed
    assert parse(printed) == wf


# -- generated round trips ------------------------------------------------------

names = st.sampled_from(["a", "b", "x1", "patch", "_tmp", "Best"]).filter(lambda n: n not in KEYWORDS)
roles = st.sampled_from(["author", "reviewer"])
text_chars = st.characters(
    codec="utf-8", categories=("L", "N", "P", "S", "Zs"), include_characters="\n\t\r\"\\{}"
)
texts = st.text(text_chars, min_size=1, max_size=12)


@st.composite
def templates(draw: st.DrawFn) -> Template:
    raw = draw(st.lists(st.one_of(texts, st.lists(names, min_size=1, max_size=3)), max_size=4))
    parts: list[str | Ref] = []
    for part in raw:
        if isinstance(part, str):
            if parts and isinstance(parts[-1], str):
                parts[-1] = parts[-1] + part
            else:
                parts.append(part)
        else:
            parts.append(Ref(tuple(part)))
    return Template(tuple(parts))


literal_templates = texts.map(lambda t: Template((t,)))
numbers = st.one_of(st.integers(0, 10**6), st.floats(0, 1e6, allow_nan=False))
atoms = st.one_of(
    numbers.map(Num), templates().map(Str), st.booleans().map(Bool), names.map(Name)
)


def _extend(children: st.SearchStrategy) -> st.SearchStrategy:
    ops = st.sampled_from(["and", "or", "==", "!=", "<", "<=", ">", ">="])
    return st.one_of(
        st.builds(Field, children, names),
        st.builds(Len, children),
        st.builds(Not, children),
        st.builds(BinOp, ops, children, children),
    )


exprs = st.recursive(atoms, _extend, max_leaves=6)
policies = st.one_of(
    st.sampled_from(["tests", "behavior", "exact"]).map(Policy),
    roles.map(lambda r: Policy("pairwise", r)),
)
values = st.one_of(
    exprs,
    st.builds(Ask, roles, templates(), st.sampled_from(["text", "number", "bool", "list"])),
    st.builds(Edit, roles, templates()),
    st.builds(Exec, literal_templates),
    st.builds(RunTests, exprs, literal_templates, st.lists(literal_templates, max_size=2).map(tuple)),
    st.builds(Choose, exprs, st.lists(policies, min_size=1, max_size=3).map(tuple),
              st.sampled_from(["abstain", "first"])),
)
actions = st.one_of(
    st.builds(Ask, roles, templates(), st.sampled_from(["text", "list"])),
    st.builds(Edit, roles, templates()),
    st.builds(Exec, literal_templates),
)
simple = st.one_of(
    st.builds(Let, names, values),
    st.builds(Var, names, values),
    st.builds(Set, names, values),
    st.builds(Do, actions),
    st.builds(Require, exprs, templates()),
    st.builds(Note, templates()),
    st.builds(Give, exprs),
)
counts = st.integers(0, 20)


def _blocks(children: st.SearchStrategy) -> st.SearchStrategy:
    body = st.lists(children, max_size=3).map(tuple)
    return st.one_of(
        st.builds(Fork, counts, st.booleans(), names, body, exprs),
        st.builds(Each, names, exprs, counts, names, body, exprs),
        st.builds(Repeat, counts, exprs, body),
        st.builds(If, exprs, body, st.one_of(st.none(), body)),
    )


statements = st.recursive(simple, _blocks, max_leaves=8)
workflows = st.builds(
    Workflow,
    names,
    st.lists(st.builds(Param, names, st.sampled_from(["text", "number", "bool"]),
                       st.one_of(st.none(), atoms.filter(lambda a: not isinstance(a, Name)))),
             max_size=2).map(tuple),
    st.lists(st.builds(Role, roles, st.lists(st.sampled_from(["answer", "edit", "shell"]),
                                             min_size=1, max_size=3).map(tuple)),
             max_size=2).map(tuple),
    st.lists(st.builds(Limit, numbers, st.sampled_from(["credits", "calls", "usd"])),
             max_size=2).map(tuple),
    st.lists(statements, max_size=5).map(tuple),
)


def _no_interpolated_defaults(wf: Workflow) -> bool:
    return all(not isinstance(p.default, Str) or p.default.template.literal for p in wf.params)


@settings(max_examples=300, deadline=None)
@given(workflows.filter(_no_interpolated_defaults))
def test_printing_then_parsing_is_the_identity(wf: Workflow):
    printed = format_workflow(wf)
    assert parse(printed) == wf
    assert format_workflow(parse(printed)) == printed
