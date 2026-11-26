from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from flowproof.workspace import (
    CopyBackend,
    GuardedBackend,
    InPlaceBackend,
    Tier,
    WorktreeBackend,
    backend_for,
    run_command,
)


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_output_and_exit_code_are_captured_without_raising(tmp_path: Path) -> None:
    code = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    result = run_command(_python(code), cwd=tmp_path, timeout_s=60)
    assert result.exit_code == 3
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"
    assert not result.timed_out
    assert not result.ok
    assert result.duration_s >= 0


def test_commands_run_in_the_given_directory_with_an_environment_overlay(tmp_path: Path) -> None:
    code = "import os; print(os.getcwd()); print(os.environ['FLOWPROOF_PROBE'])"
    result = run_command(_python(code), cwd=tmp_path, env={"FLOWPROOF_PROBE": "seen"})
    cwd, probe = result.stdout.splitlines()
    assert Path(cwd).resolve() == tmp_path.resolve()
    assert probe == "seen"


def test_a_string_command_runs_through_the_shell(tmp_path: Path) -> None:
    result = run_command("echo flowproof-shell", cwd=tmp_path, timeout_s=60)
    assert result.exit_code == 0
    assert "flowproof-shell" in result.stdout


def test_output_is_decoded_as_utf8_with_replacement(tmp_path: Path) -> None:
    code = "import sys; sys.stdout.buffer.write('caf\\u00e9 '.encode() + b'\\xff!')"
    result = run_command(_python(code), cwd=tmp_path)
    assert result.stdout == "café \ufffd!"


def test_a_timeout_kills_the_command_and_reports_no_exit_code(tmp_path: Path) -> None:
    started = time.monotonic()
    result = run_command(_python("import time; time.sleep(60)"), cwd=tmp_path, timeout_s=0.5)
    assert result.timed_out
    assert result.exit_code is None
    assert "timed out" in result.stderr
    assert time.monotonic() - started < 30


def test_a_timeout_kills_the_children_of_a_shell_command(tmp_path: Path) -> None:
    # The grandchild keeps the output pipes open; only killing the whole tree returns early.
    started = time.monotonic()
    command = f'"{sys.executable}" -c "import time; time.sleep(60)"'
    result = run_command(command, cwd=tmp_path, timeout_s=1)
    assert result.timed_out
    assert result.exit_code is None
    assert time.monotonic() - started < 30


def test_a_missing_executable_is_reported_not_raised(tmp_path: Path) -> None:
    result = run_command(["flowproof-no-such-program-xyz"], cwd=tmp_path)
    assert result.exit_code is None
    assert not result.timed_out
    assert "could not start" in result.stderr


def test_a_missing_working_directory_is_reported_not_raised(tmp_path: Path) -> None:
    result = run_command(_python("pass"), cwd=tmp_path / "missing")
    assert result.exit_code is None
    assert "could not start" in result.stderr


def test_an_empty_command_is_reported(tmp_path: Path) -> None:
    result = run_command([], cwd=tmp_path)
    assert result.exit_code is None
    assert "empty command" in result.stderr


def test_a_non_positive_timeout_is_a_programming_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run_command(_python("pass"), cwd=tmp_path, timeout_s=0)


@pytest.mark.parametrize(
    ("name", "kind", "tier"),
    [
        ("inplace", InPlaceBackend, Tier.SHARED),
        ("guarded", GuardedBackend, Tier.GUARDED),
        ("copy", CopyBackend, Tier.COPY),
        ("worktree", WorktreeBackend, Tier.WORKTREE),
    ],
)
def test_backend_for_resolves_binding_names(name: str, kind: type, tier: Tier) -> None:
    backend = backend_for(name)
    assert isinstance(backend, kind)
    assert backend.name == name
    assert backend.tier is tier


def test_backend_for_passes_the_lease_root(tmp_path: Path) -> None:
    backend = backend_for("copy", lease_root=tmp_path)
    assert isinstance(backend, CopyBackend)
    assert backend.root == tmp_path


def test_backend_for_rejects_unknown_names() -> None:
    with pytest.raises(ValueError, match="unknown workspace backend"):
        backend_for("docker")
