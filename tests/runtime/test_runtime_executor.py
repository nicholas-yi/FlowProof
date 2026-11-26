from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from flowproof.evidence import verify
from flowproof.harness import MockHarness, Reply
from flowproof.harness.base import AgentRequest
from flowproof.lang import parse
from flowproof.outcomes import Status
from flowproof.runtime import Executor, RunOptions, RunResult
from flowproof.runtime.executor import parse_answer, parse_verdict

EXAMPLES = Path(__file__).parents[2] / "examples"
BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TEST = ("import unittest\nfrom calc import add\n\n\nclass T(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 2), 4)\n")
TAMPERED = ("import unittest\n\n\nclass T(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertTrue(True)\n")
# A workflow string literal: the interpreter path is quoted because it may contain spaces.
UNITTEST = f'"\\"{Path(sys.executable).as_posix()}\\" -m unittest discover -s tests -t ."'


def git(cwd: Path, *args: str) -> str:
    isolated = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                "-c", "core.safecrlf=false"]
    return subprocess.run(["git", *isolated, *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text(BUGGY, encoding="utf-8", newline="\n")
    (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "test_calc.py").write_text(TEST, encoding="utf-8", newline="\n")
    (root / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


def run(source: str, harness: MockHarness, repo: Path, tmp_path: Path, **options) -> RunResult:
    wf = parse(textwrap.dedent(source), "test.fp")
    opts = RunOptions(source=repo, evidence=tmp_path / "evidence", lease_root=tmp_path / "leases",
                      timeout_s=60, command_timeout_s=120, **options)
    inputs = {"issue": "add is wrong"} if any(p.name == "issue" for p in wf.params) else {}
    executor = Executor(wf, {r.name: harness for r in wf.roles}, inputs=inputs, options=opts)
    assert executor.report.ok, [d.render("test.fp") for d in executor.report.errors]
    return executor.run()


FIX_AND_PICK = f"""
workflow pick
  needs issue: text
  role author can edit
  role reviewer can answer
  budget at most 20 credits, 12 calls

  fork 3 isolated into attempts
    let patch = edit author: "Fix: {{issue}} (attempt {{branch}})"
    keep patch
  end
  let graded = test attempts with {UNITTEST} frozen "tests/**"
  let best = choose graded by tests, then pairwise reviewer else first
  require best.passed, "nothing passed"
  give best
end
"""


def events(result: RunResult, kind: str) -> list[dict]:
    assert result.evidence_root is not None
    lines = (result.evidence_root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["kind"] == kind]


def test_tampering_attempt_loses_to_the_honest_fix(repo: Path, tmp_path: Path):
    before = git(repo, "status", "--porcelain")
    harness = MockHarness({
        "author": [
            Reply(files={"tests/test_calc.py": TAMPERED}, credits=1.0),  # rewrites the test
            Reply(files={"calc.py": FIXED}, credits=1.0),  # honest fix
            Reply(credits=1.0),  # does nothing
        ],
        "reviewer": [Reply("A", credits=0.1)] * 6,
    })
    result = run(FIX_AND_PICK, harness, repo, tmp_path)
    assert result.outcome.status is Status.OK, result.outcome
    assert result.result_patch and "return a + b" in result.result_patch
    assert result.value["change"]["files"] == ["calc.py"]
    assert result.calls_by_role == {"author": 3}  # the tests left one candidate: no judging
    assert result.bound_held
    assert result.usage.get("credits").value == pytest.approx(3.0)
    # The caller's repository is untouched.
    assert (repo / "calc.py").read_text(encoding="utf-8") == BUGGY
    assert git(repo, "status", "--porcelain") == before
    assert not git(repo, "for-each-ref", "refs/flowproof")
    # The evidence records the false pass and still verifies after being moved.
    grades = events(result, "grade")
    assert [g["false_pass"] for g in grades] == [True, False, False]
    assert grades[0]["tampered"] is True
    assert result.evidence_root is not None
    moved = tmp_path / "moved"
    shutil.copytree(result.evidence_root, moved)
    assert verify(moved).ok


def test_ties_go_to_a_judge_asked_in_both_orders(repo: Path, tmp_path: Path):
    def script(request: AgentRequest) -> Reply:
        if request.mode == "edit":
            files: dict[str, str | None] = {}
            if "attempt 1" in request.prompt or "attempt 2" in request.prompt:
                files["calc.py"] = FIXED
            if "attempt 2" in request.prompt:
                files["NOTES.md"] = "extra\n"
            return Reply(files=files, credits=1.0)
        shown_first = request.prompt.split("Candidate B:")[0]
        return Reply("B" if "NOTES.md" in shown_first else "A", credits=0.1)

    result = run(FIX_AND_PICK, MockHarness(script), repo, tmp_path)
    assert result.outcome.status is Status.OK
    assert result.calls_by_role == {"author": 3, "reviewer": 2}
    assert result.value["change"]["files"] == ["calc.py"]
    assert result.evidence_root is not None
    selection = next((result.evidence_root / "selections").glob("best-*.json"))
    assert json.loads(selection.read_text(encoding="utf-8"))["basis"] == ["tests", "pairwise"]


def test_a_position_biased_judge_cannot_decide(repo: Path, tmp_path: Path):
    source = FIX_AND_PICK.replace("else first", "else abstain")
    script = {"author": [Reply(files={"calc.py": FIXED})] * 2 + [Reply()],
              "reviewer": [Reply("A")] * 2}
    result = run(source, MockHarness(script), repo, tmp_path)
    assert result.outcome.status is Status.FAILED and result.outcome.reason == "abstained"


def test_call_ceiling_stops_the_run(repo: Path, tmp_path: Path):
    result = run(FIX_AND_PICK, MockHarness(default=Reply(credits=1.0)), repo, tmp_path,
                 max_calls=2)
    assert result.outcome.status is Status.LIMITED
    assert sum(result.calls_by_role.values()) == 2
    assert result.evidence_root is not None and verify(result.evidence_root).ok


def test_unknown_spend_stops_a_credit_capped_run(repo: Path, tmp_path: Path):
    result = run(FIX_AND_PICK, MockHarness(default=Reply(credits=None)), repo, tmp_path)
    assert result.outcome.status is Status.INVALID
    assert result.outcome.reason == "accounting-unknown"
    assert result.calls_by_role == {"author": 1}


def test_a_failed_branch_ends_alone(repo: Path, tmp_path: Path):
    harness = MockHarness({"author": [Reply(error="timeout"), Reply(files={"calc.py": FIXED}),
                                      Reply(error="timeout")]})
    result = run(FIX_AND_PICK, harness, repo, tmp_path)
    assert result.outcome.status is Status.OK
    assert result.value["change"]["files"] == ["calc.py"]
    assert len(events(result, "branch-ended")) == 2


def test_the_repair_loop_feeds_failures_back(repo: Path, tmp_path: Path):
    source = (EXAMPLES / "repair.fp").read_text(encoding="utf-8")
    source = source.replace('"python -m pytest -q"', UNITTEST).replace("task", "issue")
    seen: list[str] = []

    def script(request: AgentRequest) -> Reply:
        seen.append(request.prompt)
        return Reply(files={"calc.py": BUGGY if len(seen) == 1 else FIXED}, credits=1.0)

    result = run(source, MockHarness(script), repo, tmp_path)
    assert result.outcome.status is Status.OK
    assert len(seen) == 2 and "FAIL" in seen[1]  # the second prompt carried the test output


def test_testing_a_stale_change_is_invalid(repo: Path, tmp_path: Path):
    source = f"""
    workflow stale
      role author can edit
      budget at most 5 calls
      let first = edit author: "one"
      let second = edit author: "two"
      let g = test first with {UNITTEST} frozen "tests/**"
      require g.passed, "no"
      give second
    end
    """
    harness = MockHarness({"author": [Reply(files={"calc.py": FIXED}),
                                      Reply(files={"x.txt": "x"})]})
    result = run(source, harness, repo, tmp_path)
    assert result.outcome.status is Status.INVALID
    assert result.outcome.reason == "unknown-condition"


def test_a_test_without_frozen_patterns_still_runs(repo: Path, tmp_path: Path):
    source = f"""
    workflow loose
      role author can edit
      budget at most 1 calls
      let p = edit author: "fix"
      let g = test p with {UNITTEST}
      give g
    end
    """
    result = run(source, MockHarness({"author": [Reply(files={"calc.py": FIXED})]}), repo,
                 tmp_path)
    assert result.outcome.status is Status.OK and result.value["passed"] is True


def test_unparseable_answers_are_invalid_not_guessed(repo: Path, tmp_path: Path):
    source = """
    workflow n
      role a can answer
      budget at most 2 calls
      let n = ask a: "How many?" as number
      give n
    end
    """
    result = run(source, MockHarness({"a": [Reply("about seven")]}), repo, tmp_path)
    assert result.outcome.status is Status.INVALID
    assert result.outcome.reason == "unparseable-answer"


@pytest.mark.parametrize(
    ("kind", "text", "expected"),
    [("number", " 7. ", 7.0), ("number", "```\n3.5\n```", 3.5), ("bool", "Yes.", True),
     ("bool", "false", False), ("list", 'Here: ["a", "b"]', ["a", "b"]),
     ("list", "- one\n- two", ["one", "two"])],
)
def test_answers_parse_strictly(kind: str, text: str, expected: object):
    assert parse_answer(kind, text) == expected


@pytest.mark.parametrize("text", ["seven", "1 or 2"])
def test_ambiguous_numbers_are_rejected(text: str):
    with pytest.raises(ValueError):
        parse_answer("number", text)


@pytest.mark.parametrize(("text", "verdict"),
                         [("A", "A"), ("Candidate B is better.\nB", "B"), ("tie", "TIE"),
                          ("A or B?", None), ("neither", None)])
def test_verdicts(text: str, verdict: str | None):
    assert parse_verdict(text) == verdict
