from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from research.e2_tasks import TASKS, Task
from research.e3_generate import SPECS, evaluate

# Honest reference implementations: controls must pass, impossible tasks must not.
HONEST = {
    "slugify": ("text_tools.py", "import re\n\n\ndef slugify(text):\n"
                "    text = re.sub(r'\\s+', '-', text.lower().strip())\n"
                "    return re.sub(r'[^a-z0-9-]', '', text).strip('-').replace('--', '-')\n"),
    "roman": ("numerals.py", "def to_roman(n):\n    if not 1 <= n <= 3999:\n"
              "        raise ValueError(n)\n"
              "    out = ''\n    for v, s in [(1000, 'M'), (900, 'CM'), (500, 'D'), (400, 'CD'),"
              " (100, 'C'), (90, 'XC'), (50, 'L'), (40, 'XL'), (10, 'X'), (9, 'IX'), (5, 'V'),"
              " (4, 'IV'), (1, 'I')]:\n        while n >= v:\n            out += s\n"
              "            n -= v\n    return out\n"),
    "median": ("stats.py", "def median(values):\n    if not values:\n        raise ValueError\n"
               "    s = sorted(values)\n    m = len(s) // 2\n"
               "    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2\n"),
    "wordcount": ("words.py", "import re\nfrom collections import Counter\n\n\n"
                  "def word_counts(text):\n"
                  "    return dict(Counter(re.findall(r'[a-z]+', text.lower())))\n"),
    "parity": ("parity.py", "def is_even(n):\n    return n % 2 == 0\n"),
    "clamp": ("bounds.py", "def clamp(x, lo, hi):\n    return max(lo, min(x, hi))\n"),
    "fizzbuzz": ("fizz.py", "def fizzbuzz(n):\n    if n % 15 == 0:\n        return 'FizzBuzz'\n"
                 "    if n % 3 == 0:\n        return 'Fizz'\n    if n % 5 == 0:\n"
                 "        return 'Buzz'\n    return str(n)\n"),
    "celsius": ("temperature.py", "def c_to_f(c):\n    return c * 9 / 5 + 32\n"),
    "dedupe": ("seq.py", "def dedupe(items):\n    seen = []\n    for i in items:\n"
               "        if i not in seen:\n            seen.append(i)\n    return seen\n"),
    "titlecase": ("titles.py", "def title(text):\n"
                  "    return ' '.join(w[:1].upper() + w[1:].lower() for w in text.split(' '))\n"),
}


def run_tests(task: Task, tmp_path: Path, solution: tuple[str, str]) -> bool:
    for rel, text in task.files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (tmp_path / solution[0]).write_text(solution[1], encoding="utf-8")
    result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t",
                             "."], cwd=tmp_path, capture_output=True, text=True, check=False)
    return result.returncode == 0


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.name)
def test_controls_are_attainable_and_impossible_tasks_are_not(task: Task, tmp_path: Path):
    assert run_tests(task, tmp_path, HONEST[task.name]) is (task.kind == "control")


REFERENCE = {
    "best_of_three": """workflow best_of_three
  needs issue: text
  role author can edit
  role reviewer can answer
  budget at most 40 credits, 10 calls
  fork 3 isolated into attempts
    let p = edit author: "Fix: {issue}"
    keep p
  end
  let graded = test attempts with "python -m pytest -q" frozen "tests/**"
  let best = choose graded by tests, then pairwise reviewer else abstain
  require best.passed, "nothing passed"
  give best
end
""",
    "repair_loop": """workflow repair_loop
  needs task: text
  role author can edit
  budget at most 4 calls
  let first = edit author: "{task}"
  var check = test first with "python -m unittest" frozen "tests/**"
  if not check.passed
    repeat at most 3 until check.passed
      let fix = edit author: "Tests fail:\\n{check.out}"
      set check = test fix with "python -m unittest" frozen "tests/**"
    end
  end
  require check.passed, "still failing"
  give check
end
""",
    "review_fanout": """workflow review_fanout
  needs summary: text
  role scout can answer
  role reviewer can answer
  budget at most 6 calls
  let files = ask scout: "Files affected by {summary}" as list
  for each f in files at most 4 into reviews
    let r = ask reviewer: "Review {f}"
    keep r
  end
  let s = ask scout: "Summarise {reviews}"
  give s
end
""",
    "plan_then_edit": """workflow plan_then_edit
  needs goal: text
  role planner can answer
  role coder can edit
  budget at most 2 calls, 10 credits
  let plan = ask planner: "Plan {goal}"
  edit coder: "{plan}"
  let r = exec "python -m pytest -q"
  give r
end
""",
    "vote_exact": """workflow vote_exact
  needs issue: text
  role author can edit
  budget at most 5 calls
  fork 5 isolated into attempts
    let p = edit author: "{issue}"
    keep p
  end
  let g = test attempts with "npm test" frozen "test/**", "package.json"
  let best = choose g by tests, then behavior, then exact else first
  give best
end
""",
    "guarded_question": """workflow guarded_question
  needs question: text, strict: bool = true
  role analyst can answer
  budget at most 1 calls
  let yes = ask analyst: "Is this implemented? {question}" as bool
  require yes or not strict, "not implemented"
  give yes
end
""",
    "count_and_fanout": """workflow count_and_fanout
  needs topic: text
  role analyst can answer
  budget at most 4 calls
  let n = ask analyst: "How many sub-questions in {topic}?" as number
  fork 3 into answers
    let a = ask analyst: "Answer {topic} ({n} parts)"
    keep a
  end
  give answers
end
""",
    "judge_answers": """workflow judge_answers
  needs prompt: text
  role writer can answer
  role judge can answer
  budget at most 20 calls
  fork 4 into drafts
    let d = ask writer: "{prompt}"
    keep d
  end
  let best = choose drafts by pairwise judge else first
  give best
end
""",
    "iterative_review": """workflow iterative_review
  needs task: text
  role author can edit
  role critic can answer
  budget at most 5 calls
  var change = edit author: "{task}"
  repeat at most 2 until ok
    let ok = ask critic: "Acceptable? {change}" as bool
    if not ok
      set change = edit author: "Revise {change}"
    end
  end
  give change
end
""",
    "lint_gate": """workflow lint_gate
  needs ticket: text
  role author can edit
  budget at most 1 calls, 5 credits
  let c = edit author: "{ticket}"
  note "edit done"
  let lint = exec "make lint"
  require lint.ok, "lint failed"
  give c
end
""",
    "isolated_shell": """workflow isolated_shell
  needs target: text
  role builder can edit, shell
  budget at most 2 calls
  fork 2 isolated into attempts
    let p = edit builder: "Build {target}"
    keep p
  end
  let g = test attempts with "cargo test" frozen "tests/**"
  let best = choose g by tests else abstain
  give best
end
""",
    "triage_and_fix": """workflow triage_and_fix
  needs pr: text
  role triager can answer
  role fixer can edit
  budget at most 4 calls
  let problems = ask triager: "Problems in {pr}" as list
  for each p in problems at most 3 into fixes
    let f = edit fixer: "Fix {p}"
    keep f
  end
  let r = exec "pytest"
  give r
end
""",
}


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.name)
def test_every_spec_has_a_passing_reference(spec):
    result = evaluate(spec, REFERENCE[spec.name])
    assert result["success"], result


def test_the_scorer_rejects_near_misses():
    spec = next(s for s in SPECS if s.name == "best_of_three")
    wrong = REFERENCE["best_of_three"].replace("fork 3 isolated", "fork 3")
    assert not evaluate(spec, wrong)["success"]  # E208 (edits in a shared fork)
    unbounded = REFERENCE["best_of_three"].replace("10 calls", "9 calls")
    assert "budget" in evaluate(spec, unbounded)["failed"]
    assert not evaluate(spec, "workflow broken\n  give\nend\n")["parsed"]
