"""Run commands with a timeout and captured output; failures are values, not exceptions."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KILL_GRACE_S = 5.0


@dataclass(frozen=True)
class CommandResult:
    """What a command did. `exit_code` is None if it never started or timed out."""

    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def to_json(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_s": round(self.duration_s, 3),
            "timed_out": self.timed_out,
        }


@dataclass(frozen=True)
class RawResult:
    """A command result with undecoded output, for callers that need exact bytes."""

    exit_code: int | None
    stdout: bytes
    stderr: bytes
    duration_s: float
    timed_out: bool

    def decoded(self) -> CommandResult:
        return CommandResult(
            exit_code=self.exit_code,
            stdout=self.stdout.decode("utf-8", errors="replace"),
            stderr=self.stderr.decode("utf-8", errors="replace"),
            duration_s=self.duration_s,
            timed_out=self.timed_out,
        )


def run_command(
    command: str | Sequence[str],
    *,
    cwd: Path,
    timeout_s: float = 600.0,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Run `command` in `cwd` and return what happened; never raises for a failed command.

    A string runs through the platform shell; a sequence runs directly. `env` is
    layered over the current environment.
    """
    return run_raw(command, cwd=cwd, timeout_s=timeout_s, env=env).decoded()


def run_raw(
    command: str | Sequence[str],
    *,
    cwd: Path,
    timeout_s: float = 600.0,
    env: Mapping[str, str] | None = None,
    replace_env: bool = False,
    stdin: bytes | None = None,
) -> RawResult:
    """Like `run_command` but keeps output as bytes.

    With `replace_env`, `env` is the complete environment instead of an overlay.
    """
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    shell = isinstance(command, str)
    args: str | list[str]
    if isinstance(command, str):
        args = command
    else:
        args = [os.fspath(part) for part in command]
        if not args:
            return RawResult(None, b"", b"flowproof: empty command\n", 0.0, False)
    if replace_env:
        full_env = dict(env or {})
    else:
        full_env = dict(os.environ)
        full_env.update(env or {})
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            env=full_env,
            shell=shell,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=sys.platform != "win32",
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        message = f"flowproof: could not start {_describe(args)}: {exc}\n"
        return RawResult(None, b"", message.encode("utf-8"), time.monotonic() - started, False)
    try:
        out, err = proc.communicate(input=stdin, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        out, err = _drain(proc)
        note = f"\nflowproof: timed out after {timeout_s:g}s; the process tree was killed\n"
        return RawResult(None, out, err + note.encode("utf-8"), time.monotonic() - started, True)
    except BaseException:
        _kill_tree(proc)
        raise
    return RawResult(proc.returncode, out or b"", err or b"", time.monotonic() - started, False)


def _describe(args: str | list[str]) -> str:
    if isinstance(args, str):
        return repr(args)
    return repr(args[0])


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    # A shell (or any launcher) may have children holding the output pipes open;
    # killing only the direct child would leave communicate() waiting on them.
    if sys.platform == "win32":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=KILL_GRACE_S,
                check=False,
            )
    else:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()


def _drain(proc: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
    try:
        out, err = proc.communicate(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired as exc:
        return _as_bytes(exc.stdout), _as_bytes(exc.stderr)
    return out or b"", err or b""


def _as_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace")
    return b""
