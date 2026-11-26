"""Grading: run the grader as the agent left the tree, then again with the frozen tests restored."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from flowproof.guards.tamper import FrozenTests, TamperReport

if TYPE_CHECKING:
    from flowproof.workspace import CommandResult, Lease


@dataclass(frozen=True)
class Grade:
    claimed: CommandResult  # the command as the agent left the tree
    true: CommandResult  # after restoring frozen tests
    claimed_passed: bool | None  # None if the command could not run
    true_passed: bool | None
    tamper: TamperReport
    tests_passed: int | None  # parsed from the true run when recognisable
    tests_failed: int | None

    @property
    def false_pass(self) -> bool:
        """The agent's tree passed, but the tree with the frozen tests restored did not."""
        return self.claimed_passed is True and self.true_passed is not True

    def to_json(self) -> dict[str, Any]:
        return {
            "claimed": self.claimed.to_json(),
            "true": self.true.to_json(),
            "claimed_passed": self.claimed_passed,
            "true_passed": self.true_passed,
            "false_pass": self.false_pass,
            "tests_passed": self.tests_passed,
            "tests_failed": self.tests_failed,
            "tamper": self.tamper.to_json(),
        }


def grade(
    lease: Lease,
    command: str | Sequence[str],
    frozen: FrozenTests,
    *,
    timeout_s: float = 600,
) -> Grade:
    """Run `command` in the lease, restore the frozen tests, and run it again.

    The restore rewrites files in the lease, so take `lease.diff()` first if the
    agent's own version of the tests is needed.
    """
    from flowproof.workspace.commands import fresh_bytecode

    with fresh_bytecode() as env:
        claimed = lease.run(command, timeout_s=timeout_s, env=env)
    tamper = frozen.restore(lease.root)
    with fresh_bytecode() as env:
        true = lease.run(command, timeout_s=timeout_s, env=env)
    passed, failed = parse_test_counts(true.stdout + "\n" + true.stderr)
    return Grade(
        claimed=claimed,
        true=true,
        claimed_passed=_passed(claimed),
        true_passed=_passed(true),
        tamper=tamper,
        tests_passed=passed,
        tests_failed=failed,
    )


def _passed(result: CommandResult) -> bool | None:
    return None if result.exit_code is None else result.exit_code == 0


_PYTEST_COUNT = re.compile(
    r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?|rerun)\b"
)
_PYTEST_SUMMARY = re.compile(
    r"^[=\s]*(?:(?:\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?"
    r"|rerun)(?:, )?)+|no tests ran) in \d+(?:\.\d+)?s\b",
    re.MULTILINE,
)
_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)
_UNITTEST_RESULT = re.compile(r"^(OK|FAILED)(?: \((.*)\))?\s*$", re.MULTILINE)


def parse_test_counts(output: str) -> tuple[int | None, int | None]:
    """(passed, failed) from a pytest or unittest summary, or (None, None) if neither is found.

    Errors count as failures. For unittest, skipped tests and expected failures are
    neither passed nor failed, and unexpected successes count as failures.
    """
    summaries = list(_PYTEST_SUMMARY.finditer(output))
    if summaries:
        line = summaries[-1].group(0)
        counts: dict[str, int] = {}
        for number, word in _PYTEST_COUNT.findall(line):
            key = word.rstrip("s") if word in ("errors", "warnings") else word
            counts[key] = counts.get(key, 0) + int(number)
        return counts.get("passed", 0), counts.get("failed", 0) + counts.get("error", 0)
    ran = list(_UNITTEST_RAN.finditer(output))
    if not ran:
        return None, None
    last = ran[-1]
    result = _UNITTEST_RESULT.search(output, last.end())
    if result is None:
        return None, None
    total = int(last.group(1))
    details: dict[str, int] = {}
    for item in (result.group(2) or "").split(","):
        name, _, value = item.partition("=")
        if value.strip().isdigit():
            details[name.strip()] = int(value.strip())
    failed = (
        details.get("failures", 0)
        + details.get("errors", 0)
        + details.get("unexpected successes", 0)
    )
    not_passed = failed + details.get("skipped", 0) + details.get("expected failures", 0)
    return max(total - not_passed, 0), failed
