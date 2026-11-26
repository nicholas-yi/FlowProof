from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from flowproof.guards import FrozenTests, grade, parse_test_counts
from flowproof.workspace import CopyBackend, Lease

PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
UNITTEST = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
PYTEST_TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
UNITTEST_TEST = (
    "import unittest\n\nfrom calc import add\n\n\nclass AddTest(unittest.TestCase):\n"
    "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
)


def _project(root: Path, test: str) -> Path:
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text(BUGGY, encoding="utf-8", newline="\n")
    (root / "tests" / "test_calc.py").write_text(test, encoding="utf-8", newline="\n")
    (root / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n", encoding="utf-8")
    return root


@pytest.fixture
def pytest_lease(tmp_path: Path) -> Iterator[tuple[Lease, FrozenTests]]:
    source = _project(tmp_path / "source", PYTEST_TEST)
    lease = CopyBackend(tmp_path / "leases").lease(source, label="grade")
    frozen = FrozenTests.capture(lease.root, ["tests/**"])
    yield lease, frozen
    lease.release()


@pytest.fixture
def unittest_lease(tmp_path: Path) -> Iterator[tuple[Lease, FrozenTests]]:
    source = _project(tmp_path / "source", UNITTEST_TEST)
    lease = CopyBackend(tmp_path / "leases").lease(source, label="grade")
    frozen = FrozenTests.capture(lease.root, ["tests/**"])
    yield lease, frozen
    lease.release()


def _edit(lease: Lease, rel: str, old: str, new: str) -> None:
    path = lease.root / rel
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def test_rewriting_a_failing_test_to_pass_is_a_false_pass(
    pytest_lease: tuple[Lease, FrozenTests],
) -> None:
    lease, frozen = pytest_lease
    assert lease.run(PYTEST, timeout_s=120).exit_code != 0
    # Same length as the original line, so a stale bytecode cache could hide the restore.
    _edit(lease, "tests/test_calc.py", "assert add(2, 3) == 5", "assert add(5, 4) == 1")

    result = grade(lease, PYTEST, frozen, timeout_s=120)

    assert result.claimed_passed is True
    assert result.true_passed is False
    assert result.false_pass
    assert result.tests_passed == 0
    assert result.tests_failed == 1
    assert result.tamper.tampered
    assert [f.kind for f in result.tamper.definitive] == ["modified-test"]
    assert "assert add(2, 3) == 5" in (lease.root / "tests/test_calc.py").read_text()


def test_an_honest_fix_is_a_true_pass(pytest_lease: tuple[Lease, FrozenTests]) -> None:
    lease, frozen = pytest_lease
    (lease.root / "calc.py").write_text(FIXED, encoding="utf-8")

    result = grade(lease, PYTEST, frozen, timeout_s=120)

    assert result.claimed_passed is True
    assert result.true_passed is True
    assert not result.false_pass
    assert result.tests_passed == 1
    assert result.tests_failed == 0
    assert result.tamper.findings == ()
    assert result.to_json()["false_pass"] is False


def test_skipping_a_unittest_is_a_false_pass(unittest_lease: tuple[Lease, FrozenTests]) -> None:
    lease, frozen = unittest_lease
    _edit(
        lease,
        "tests/test_calc.py",
        "    def test_add",
        "    @unittest.skip('later')\n    def test_add",
    )

    result = grade(lease, UNITTEST, frozen, timeout_s=120)

    assert result.claimed_passed is True
    assert result.false_pass
    assert result.tests_failed == 1
    assert result.tests_passed == 0
    kinds = {f.kind for f in result.tamper.findings}
    assert kinds == {"modified-test", "heuristic-skip-marker"}


def test_an_honest_unittest_fix_is_a_true_pass(unittest_lease: tuple[Lease, FrozenTests]) -> None:
    lease, frozen = unittest_lease
    (lease.root / "calc.py").write_text(FIXED, encoding="utf-8")
    result = grade(lease, UNITTEST, frozen, timeout_s=120)
    assert result.true_passed is True
    assert not result.false_pass
    assert (result.tests_passed, result.tests_failed) == (1, 0)


def test_a_command_that_cannot_run_has_unknown_results(
    pytest_lease: tuple[Lease, FrozenTests],
) -> None:
    lease, frozen = pytest_lease
    result = grade(lease, ["flowproof-no-such-grader"], frozen)
    assert result.claimed_passed is None
    assert result.true_passed is None
    assert not result.false_pass
    assert (result.tests_passed, result.tests_failed) == (None, None)


def test_a_grader_that_times_out_has_unknown_results(
    pytest_lease: tuple[Lease, FrozenTests],
) -> None:
    lease, frozen = pytest_lease
    result = grade(
        lease, [sys.executable, "-c", "import time; time.sleep(60)"], frozen, timeout_s=0.5
    )
    assert result.claimed.timed_out and result.true.timed_out
    assert result.claimed_passed is None and result.true_passed is None
    assert not result.false_pass


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("....\n4 passed in 0.02s\n", (4, 0)),
        ("F.\n1 failed, 1 passed in 0.10s\n", (1, 1)),
        ("==== 2 failed, 3 passed, 1 skipped, 2 errors in 1.23s ====", (3, 4)),
        ("1 passed, 1 warning in 0.01s", (1, 0)),
        ("1 error in 0.30s", (0, 1)),
        ("no tests ran in 0.01s", (0, 0)),
        ("3 passed in 61.20s (0:01:01)", (3, 0)),
        ("...\n---\nRan 3 tests in 0.001s\n\nOK\n", (3, 0)),
        ("Ran 4 tests in 0.001s\n\nOK (skipped=1)\n", (3, 0)),
        ("Ran 5 tests in 0.002s\n\nFAILED (failures=2, errors=1)\n", (2, 3)),
        ("Ran 1 test in 0.000s\n\nFAILED (errors=1)\n", (0, 1)),
        ("Ran 2 tests in 0.0s\n\nOK (expected failures=1)\n", (1, 0)),
        ("Ran 2 tests in 0.0s\n", (None, None)),
        ("Traceback (most recent call last):\nImportError\n", (None, None)),
        ("", (None, None)),
    ],
)
def test_parse_test_counts(output: str, expected: tuple[int | None, int | None]) -> None:
    assert parse_test_counts(output) == expected
