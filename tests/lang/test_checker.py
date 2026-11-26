from __future__ import annotations

from pathlib import Path

import pytest

from flowproof.lang import HarnessInfo, Interval, check, parse

EXAMPLES = sorted((Path(__file__).parents[2] / "examples").glob("*.fp"))
HEADERS = """  needs issue: text
  role author can edit
  role judge can answer
  budget at most 100 calls
"""


def program(body: str, headers: str = HEADERS) -> str:
    return f"workflow w\n{headers}\n{body}\nend\n"


def report(body: str, headers: str = HEADERS, **kwargs):
    return check(parse(program(body, headers)), **kwargs)


def codes(body: str, headers: str = HEADERS, **kwargs) -> list[str]:
    return [d.code for d in report(body, headers, **kwargs).diagnostics]


def errors(body: str, headers: str = HEADERS, **kwargs) -> list[str]:
    return [d.code for d in report(body, headers, **kwargs).errors]


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_examples_are_clean(path: Path):
    result = check(parse(path.read_text(encoding="utf-8"), str(path)))
    assert result.diagnostics == ()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("  give nowhere", "E201"),
        ('  let x = 1\n  let x = 2\n  give x', "E202"),
        ('  let x = ask ghost: "?"\n  give x', "E203"),
        ('  let x = edit judge: "?"\n  give x', "E204"),
        ("  if 1\n  end", "E205"),
        ('  give issue == 3', "E205"),
        ('  let r = exec "ls"\n  give r.nope', "E206"),
        ('  fork 2 isolated into xs\n    let p = edit author: "x"\n    keep p\n  end\n'
         '  give xs.patch', "E206"),
        ('  let r = exec "echo {issue}"\n  give r', "E207"),
        ('  fork 2 isolated into xs\n    let p = edit author: "x"\n    keep p\n  end\n'
         '  let g = test xs with "t" frozen "{issue}"\n  give g', "E207"),
        ('  fork 2 into xs\n    let p = edit author: "x"\n    keep p\n  end\n  give xs', "E208"),
        ('  fork 2 into xs\n    let r = exec "ls"\n    keep r\n  end\n  give xs', "E208"),
        ('  let xs = ask judge: "list" as list\n  let b = choose xs by pairwise judge\n'
         '  give b', "E209"),
        ('  give 1\n  note "after"', "E216"),
        ('  if true\n    give 1\n  end', "E216"),
        ("  fork 0 isolated into xs\n    keep 1\n  end\n  give xs", "E218"),
        ("  repeat at most 0 until true\n  end", "E218"),
        ("  let x = 1\n  set x = 2\n  give x", "E219"),
        ("  var x = 1\n  fork 2 into xs\n    set x = 2\n    keep 1\n  end\n  give xs", "E220"),
        ("  var x = 1\n  set x = true\n  give x", "E205"),
        ('  var xs = ask judge: "?" as list\n  give xs', "E205"),
        ("  let unused = 1", "W301"),
        ('  fork 2 isolated into xs\n    let p = edit author: "x"\n    keep p\n  end\n'
         '  let g = test xs with "t" frozen "tests/**"\n'
         '  let b = choose g by tests, then pairwise author\n  give b', "W302"),
        ('  fork 2 into xs\n    let a = ask author: "x"\n    keep a\n  end\n'
         '  let b = choose xs by pairwise judge\n  give b', "W303"),
        ('  let p = edit author: "x"\n  let g = test p with "t"\n  give g', "W304"),
        ("  var n = 1\n  repeat at most 3 until n > 2\n    note \"tick\"\n  end", "W306"),
    ],
)
def test_each_diagnostic_fires(body: str, expected: str):
    assert expected in codes(body)


def test_headers_are_checked():
    headers = ("  needs a: text, a: number = \"x\"\n  role r can answer\n  role r can edit\n"
               "  budget at most 3 calls, 4 calls, 1.5 calls\n")
    found = codes('  give a', headers)
    assert found.count("E217") == 4  # input, role, two repeated units
    assert "E205" in found  # default of the wrong type, and a fractional call cap


def test_unused_roles_and_missing_budget():
    found = codes('  let x = ask a: "?"\n  give x', "  role a can answer\n  role b can answer\n")
    assert "W301" in found and "W308" in found


def test_errors_do_not_cascade_from_unknown_names():
    assert errors("  let x = ghost.field\n  give x.other") == ["E201"]


def test_budget_ceilings_against_the_bound():
    body = '  fork 3 isolated into xs\n    let p = edit author: "x"\n    keep p\n  end\n  give xs'
    assert "E210" in codes(body, "  role author can edit\n  budget at most 2 calls\n")
    assert "W305" in codes('  repeat at most 5 until x.ok\n    edit author: "go"\n'
                           '    let x = exec "t"\n  end', "  role author can edit\n"
                           "  budget at most 3 calls\n")
    assert codes(body, "  role author can edit\n  budget at most 3 calls\n") == []


@pytest.mark.parametrize(
    ("body", "total", "roles"),
    [
        ('  let x = ask judge: "?"\n  give x', Interval(1, 1), {"judge": Interval(1, 1)}),
        ('  fork 3 isolated into xs\n    let p = edit author: "x"\n'
         '    fork 2 into ys\n      let q = ask judge: "{p}"\n      keep q\n    end\n'
         '    keep ys\n  end\n  give xs',
         Interval(9, 9), {"author": Interval(3, 3), "judge": Interval(6, 6)}),
        ('  if issue == "x"\n    let a = ask judge: "?"\n    let b = ask judge: "?"\n'
         '    note "{a}{b}"\n  else\n    edit author: "x"\n  end',
         Interval(1, 2), {"author": Interval(0, 1), "judge": Interval(0, 2)}),
        ('  let xs = ask judge: "?" as list\n  for each x in xs at most 4 into ys\n'
         '    let y = ask judge: "{x}"\n    keep y\n  end\n  give ys',
         Interval(1, 5), {"judge": Interval(1, 5)}),
        ('  repeat at most 4 until r.ok\n    edit author: "x"\n    let r = exec "t"\n  end',
         Interval(1, 4), {"author": Interval(1, 4)}),
        ('  fork 4 into xs\n    let a = ask author: "x"\n    keep a\n  end\n'
         '  let b = choose xs by pairwise judge\n  give b',
         Interval(16, 16), {"author": Interval(4, 4), "judge": Interval(12, 12)}),
        ('  fork 4 isolated into xs\n    let p = edit author: "x"\n    keep p\n  end\n'
         '  let g = test xs with "t" frozen "tests/**"\n'
         '  let b = choose g by tests, then pairwise judge\n  give b',
         Interval(4, 16), {"author": Interval(4, 4), "judge": Interval(0, 12)}),
        ('  let xs = ask judge: "?" as list\n  for each x in xs at most 3 into ys\n'
         '    repeat at most 2 until r.ok\n      edit author: "{x}"\n      let r = exec "t"\n'
         '    end\n    keep r\n  end\n  give ys',
         Interval(1, 7), {"judge": Interval(1, 1), "author": Interval(0, 6)}),
    ],
)
def test_call_bounds(body: str, total: Interval, roles: dict[str, Interval]):
    result = report(body)
    assert result.calls == total
    assert dict(result.calls_by_role) == roles


def test_unbounded_pairwise_has_no_upper_bound():
    result = report('  let xs = ask judge: "?" as list\n  let b = choose xs by pairwise judge\n'
                    '  give b')
    assert result.calls.hi is None and not result.ok


def test_credit_estimate_uses_role_prices():
    path = next(p for p in EXAMPLES if p.name == "fix_and_pick.fp")
    result = check(parse(path.read_text(encoding="utf-8")))
    assert result.credits({"author": 4.0, "reviewer": 1.0}) == (13.0, 19.0)
    with pytest.raises(ValueError, match="reviewer"):
        result.credits({"author": 4.0})


def test_scoping_rules():
    # Bound in both arms with one type: visible after the if.
    assert errors('  if issue == "a"\n    let x = 1\n  else\n    let x = 2\n  end\n'
                  '  give x') == []
    # Bound in one arm only: not visible.
    assert errors('  if issue == "a"\n    let x = 1\n  end\n  give x') == ["E201"]
    # Bound in a repeat body: visible after it.
    assert errors('  repeat at most 2 until r.ok\n    let r = exec "t"\n  end\n'
                  '  give r.out') == []
    # Fork-local names are not visible outside; nested forks each get a `branch`.
    found = errors('  fork 2 into xs\n    fork 2 into ys\n      keep branch\n    end\n'
                  '    let local = 1\n    keep branch\n  end\n  give local')
    assert found.count("E201") == 1 and "E202" not in found


def test_bindings_must_cover_roles_capabilities_and_units():
    headers = "  role author can edit, shell\n  role judge can answer\n  budget at most 5 credits\n"
    body = '  let p = edit author: "x"\n  let v = ask judge: "{p}"\n  give v'
    answer_only = HarnessInfo("answers", frozenset({"answer"}), frozenset({"credits"}))
    blind = HarnessInfo("blind", frozenset({"answer", "edit", "shell"}), frozenset())
    found = codes(body, headers, bindings={"author": answer_only, "ghost": answer_only})
    assert {"E212", "E213", "E203"} <= set(found)
    assert "E211" in codes(body, headers, bindings={"author": blind, "judge": answer_only})
    capable = HarnessInfo("full", frozenset({"answer", "edit", "shell"}), frozenset({"credits"}))
    assert codes(body, headers, bindings={"author": capable, "judge": answer_only}) == []


def test_workspace_rules():
    body = '  let p = edit author: "x"\n  give p'
    headers = "  role author can edit\n  budget at most 2 calls\n"
    assert "E214" in codes(body, headers, workspace="guarded")
    assert codes(body, headers, workspace="inplace") == ["W307"]
    isolated = '  fork 2 isolated into xs\n    let p = edit author: "x"\n    keep p\n  end\n  give xs'
    assert codes(isolated, headers, workspace="inplace") == []


def test_frozen_patterns_are_collected_once_in_order():
    body = ('  let p = edit author: "x"\n'
            '  let a = test p with "t" frozen "tests/**", "conftest.py"\n'
            '  let b = test p with "t" frozen "tests/**"\n  note "{a}{b}{issue}"')
    assert report(body).frozen == ("tests/**", "conftest.py")


def test_diagnostics_render_with_locations():
    result = check(parse(program("  give nowhere"), "flow.fp"))
    assert result.errors[0].render("flow.fp").startswith("flow.fp:7:8: error E201:")
