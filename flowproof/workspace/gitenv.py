"""Git with an isolated configuration, so a user's settings cannot change what Flowproof sees."""

from __future__ import annotations

import contextlib
import itertools
import os
import secrets
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from flowproof.workspace.changes import Change
from flowproof.workspace.commands import RawResult, run_raw
from flowproof.workspace.errors import GitError

GIT_TIMEOUT_S = 600.0


@dataclass(frozen=True)
class RepoInfo:
    """Where a directory sits inside a git work tree."""

    top: Path  # the work tree's top level
    prefix: str  # the directory relative to `top`, "" or ending in "/"


class Git:
    """Runs git with a private, empty global configuration.

    Owns a temporary directory holding the empty config file, an empty hooks
    directory and scratch index files. Call `close()` (or use it as a context
    manager) to delete it.
    """

    def __init__(self, executable: str = "git") -> None:
        self.executable = executable
        self._home = Path(tempfile.mkdtemp(prefix="flowproof-git-")).resolve()
        self.config_file = self._home / "gitconfig"
        self.config_file.write_bytes(b"")
        self._empty_file = self._home / "empty"
        self._empty_file.write_bytes(b"")
        self._hooks = self._home / "hooks"
        self._hooks.mkdir()
        self._counter = itertools.count()

    @property
    def scratch(self) -> Path:
        return self._home

    def temp_path(self, stem: str) -> Path:
        return self._home / f"{stem}-{next(self._counter)}-{secrets.token_hex(3)}"

    def flags(self) -> list[str]:
        settings = {
            "core.autocrlf": "false",
            "core.safecrlf": "false",
            "user.name": "flowproof",
            "user.email": "flowproof@localhost",
            "commit.gpgsign": "false",
            "core.hooksPath": self._hooks.as_posix(),
            "core.fsmonitor": "false",
            "core.untrackedCache": "false",
            "core.splitIndex": "false",
            "core.attributesFile": self._empty_file.as_posix(),
            "core.excludesFile": self._empty_file.as_posix(),
            "core.quotePath": "false",
            "core.longpaths": "true",
            "diff.noprefix": "false",
            "diff.mnemonicPrefix": "false",
            "gc.auto": "0",
            "maintenance.auto": "false",
            "submodule.recurse": "false",
            "protocol.allow": "never",
        }
        flags: list[str] = []
        for key, value in settings.items():
            flags += ["-c", f"{key}={value}"]
        return flags

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        # Inherited GIT_* variables (for example GIT_DIR or GIT_INDEX_FILE set by a
        # hook that launched us) would silently redirect every command.
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        env.update(
            GIT_CONFIG_GLOBAL=str(self.config_file),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
            GIT_OPTIONAL_LOCKS="0",
            GIT_ATTR_NOSYSTEM="1",
        )
        if extra:
            env.update(extra)
        return env

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
        timeout_s: float = GIT_TIMEOUT_S,
        check: bool = True,
    ) -> RawResult:
        command = [self.executable, *self.flags(), *args]
        result = run_raw(
            command,
            cwd=cwd,
            timeout_s=timeout_s,
            env=self.environment(env),
            replace_env=True,
            stdin=stdin,
        )
        if check and result.exit_code != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise GitError(f"git {' '.join(args)} failed ({result.exit_code}) in {cwd}: {stderr}")
        return result

    def text(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str] | None = None) -> str:
        output = self.run(args, cwd=cwd, env=env).stdout.decode("utf-8", errors="surrogateescape")
        return output.rstrip("\r\n")

    def close(self) -> None:
        shutil.rmtree(self._home, ignore_errors=True)

    def __enter__(self) -> Git:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def repo_info(git: Git, path: Path) -> RepoInfo | None:
    """Locate `path` in a git work tree, or None if it is not inside one."""
    result = git.run(["rev-parse", "--show-toplevel", "--show-prefix"], cwd=path, check=False)
    if result.exit_code is None:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(f"git could not be started: {stderr}")
    if result.exit_code != 0:
        return None
    lines = result.stdout.decode("utf-8", errors="surrogateescape").splitlines()
    if not lines or not lines[0]:
        return None
    prefix = lines[1].strip() if len(lines) > 1 else ""
    return RepoInfo(top=Path(lines[0]).resolve(), prefix=prefix)


def empty_tree(git: Git, top: Path) -> str:
    """The empty tree's id in this repository's hash format (SHA-1 or SHA-256)."""
    result = git.run(["hash-object", "-t", "tree", "--stdin"], cwd=top, stdin=b"")
    return result.stdout.decode("ascii").strip()


def exact_bytes_env(index: Path, empty: str) -> dict[str, str]:
    # Attributes are read from the empty tree, so `.gitattributes` eol, text and
    # filter rules cannot rewrite bytes on their way into or out of the index.
    return {"GIT_INDEX_FILE": str(index), "GIT_ATTR_SOURCE": empty}


def pin_state(git: Git, top: Path, message: str, empty: str) -> str:
    """Commit the work tree exactly as it is now, without touching the real index.

    Includes uncommitted changes and untracked files that are not ignored. The
    commit's parent is HEAD when HEAD exists. Returns the commit id.
    """
    index = git.temp_path("pin.index")
    env = exact_bytes_env(index, empty)
    try:
        head = git.run(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], cwd=top, check=False)
        parent = (
            head.stdout.decode("ascii", errors="replace").strip() if head.exit_code == 0 else ""
        )
        git.run(["read-tree", parent] if parent else ["read-tree", "--empty"], cwd=top, env=env)
        git.run(["add", "-A"], cwd=top, env=env)
        tree = git.text(["write-tree"], cwd=top, env=env)
        args = ["commit-tree", tree, "-m", message]
        if parent:
            args += ["-p", parent]
        return git.text(args, cwd=top, env=env)
    finally:
        _unlink_quietly(index)
        _unlink_quietly(Path(f"{index}.lock"))


def stage_and_diff(
    git: Git,
    *,
    cwd: Path,
    base: str,
    prefix: str,
    empty: str,
    git_dir: Path | None = None,
) -> Change:
    """Diff the whole work tree at `cwd` against commit `base` using a scratch index.

    New, deleted and binary files appear; paths are relative to `prefix`.
    """
    location = ["--git-dir", str(git_dir), "--work-tree", str(cwd)] if git_dir else []
    index = git.temp_path("diff.index")
    env = exact_bytes_env(index, empty)
    try:
        git.run([*location, "read-tree", base], cwd=cwd, env=env)
        git.run([*location, "add", "-A"], cwd=cwd, env=env)
        common = [
            *location,
            "diff",
            "--cached",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
        ]
        if prefix:
            common.append(f"--relative={prefix}")
        names = git.run([*common, "--name-status", "-z", base, "--"], cwd=cwd, env=env).stdout
        patch = git.run(
            [*common, "--binary", "--src-prefix=a/", "--dst-prefix=b/", base, "--"],
            cwd=cwd,
            env=env,
        ).stdout
    finally:
        _unlink_quietly(index)
        _unlink_quietly(Path(f"{index}.lock"))
    files: list[str] = []
    added: list[str] = []
    deleted: list[str] = []
    tokens = names.split(b"\0")
    for status, raw in zip(tokens[0::2], tokens[1::2], strict=False):
        if not status:
            continue
        path = raw.decode("utf-8", errors="replace")
        files.append(path)
        if status.startswith(b"A"):
            added.append(path)
        elif status.startswith(b"D"):
            deleted.append(path)
    return Change(
        patch=patch.decode("utf-8", errors="replace"),
        files=tuple(sorted(files)),
        added=tuple(sorted(added)),
        deleted=tuple(sorted(deleted)),
    )


def _unlink_quietly(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)
