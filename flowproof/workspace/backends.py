"""Workspace backends: where an agent works, what it started from, and what it changed."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self

from flowproof.guards.fingerprint import (
    DEFAULT_EXCLUDES,
    Fingerprint,
    GuardViolation,
    ReadOnlyGuard,
    fingerprint,
    is_junction,
)
from flowproof.workspace.changes import Change, listing_change, snapshot_change
from flowproof.workspace.commands import CommandResult, run_command
from flowproof.workspace.errors import WorkspaceError
from flowproof.workspace.gitenv import (
    Git,
    RepoInfo,
    empty_tree,
    pin_state,
    repo_info,
    stage_and_diff,
)

COPY_EXCLUDED_NAMES: tuple[str, ...] = (
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "build",
    "dist",
    ".flowproof",
    ".mypy_cache",
    ".ruff_cache",
    ".hypothesis",
    ".tox",
    ".nox",
)

_REMOVE_ATTEMPTS = 8


class Tier(IntEnum):
    SHARED = 0  # agents work in the caller's directory
    GUARDED = 1  # in place, but read-only and fingerprint-checked
    COPY = 2  # a private copy of the source tree
    WORKTREE = 3  # a git worktree at a pinned commit


@dataclass(frozen=True)
class _Identity:
    id: str
    root: Path
    base: str
    tier: Tier
    label: str
    source: Path


class Lease(ABC):
    """A directory an agent may work in, the base it started from, and how to give it back."""

    def __init__(
        self, *, id: str, root: Path, base: str, tier: Tier, label: str, source: Path
    ) -> None:
        self.id = id
        self.root = root
        self.base = base
        self.tier = tier
        self.label = label
        self.source = source
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def run(
        self,
        command: str | Sequence[str],
        *,
        timeout_s: float = 600,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Run a command in the lease root; a string goes through the platform shell.

        `env` is layered over the current environment. Never raises for a failing
        command: see `CommandResult`.
        """
        self._require_open("run a command in")
        return run_command(command, cwd=self.root, timeout_s=timeout_s, env=env)

    def diff(self) -> Change:
        """Everything that differs from the base, including new and deleted files."""
        self._require_open("diff")
        return self._diff()

    def release(self) -> None:
        """Give the lease back. Idempotent; never deletes the caller's tree."""
        if self._released:
            return
        self._release()
        self._released = True

    @abstractmethod
    def _diff(self) -> Change: ...

    @abstractmethod
    def _release(self) -> None: ...

    def _require_open(self, action: str) -> None:
        if self._released:
            raise WorkspaceError(f"cannot {action} lease {self.id}: it has been released")

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(id={self.id!r}, tier={self.tier.name}, "
            f"root={str(self.root)!r}, base={self.base!r})"
        )


def _init_lease(lease: Lease, identity: _Identity) -> None:
    Lease.__init__(
        lease,
        id=identity.id,
        root=identity.root,
        base=identity.base,
        tier=identity.tier,
        label=identity.label,
        source=identity.source,
    )


class Backend(Protocol):
    name: str
    tier: Tier

    def lease(self, source: Path, *, label: str) -> Lease: ...


class _Tracker(ABC):
    """Computes the change of a live tree against the base it was leased at."""

    @abstractmethod
    def change(self) -> Change: ...

    @abstractmethod
    def close(self) -> None: ...


class _GitTracker(_Tracker):
    def __init__(
        self,
        git: Git,
        *,
        cwd: Path,
        base: str,
        prefix: str,
        empty: str,
        git_dir: Path | None = None,
    ) -> None:
        self.git = git
        self.cwd = cwd
        self.base = base
        self.prefix = prefix
        self.empty = empty
        self.git_dir = git_dir

    def change(self) -> Change:
        return stage_and_diff(
            self.git,
            cwd=self.cwd,
            base=self.base,
            prefix=self.prefix,
            empty=self.empty,
            git_dir=self.git_dir,
        )

    def close(self) -> None:
        self.git.close()


class _SnapshotTracker(_Tracker):
    """Keeps a pristine private copy of the base so a full text diff can be produced."""

    def __init__(self, holder: Path, before: Fingerprint, live: Path) -> None:
        self.holder = holder
        self.before = before
        self.live = live

    @property
    def snapshot(self) -> Path:
        return self.holder / "base"

    @classmethod
    def take(cls, source: Path, label: str, parent: Path | None) -> _SnapshotTracker:
        holder = _make_container(parent, f"base-{label}", source)
        try:
            _copy_tree(source, holder / "base")
            before = fingerprint(holder / "base")
        except BaseException:
            _remove_tree(holder)
            raise
        return cls(holder, before, source)

    def change(self) -> Change:
        return snapshot_change(self.snapshot, self.before, self.live, fingerprint(self.live))

    def close(self) -> None:
        _remove_tree(self.holder)


class InPlaceLease(Lease):
    def __init__(self, identity: _Identity, tracker: _Tracker) -> None:
        _init_lease(self, identity)
        self._tracker = tracker

    def _diff(self) -> Change:
        return self._tracker.change()

    def _release(self) -> None:
        self._tracker.close()


class InPlaceBackend:
    """Tier SHARED: the agent works directly in the caller's directory.

    In a git work tree the base is a commit of the tree as it was at lease time,
    written to the object store without a ref, so changes the caller had already
    made are not attributed to the agent. Outside git the base is a fingerprint
    digest and a private snapshot (in the system temporary directory) backs `diff()`.
    """

    name: str = "inplace"
    tier: Tier = Tier.SHARED

    def lease(self, source: Path, *, label: str) -> Lease:
        root = _existing_dir(source)
        git = Git()
        tracker: _Tracker
        try:
            info = repo_info(git, root)
            if info is not None:
                empty = empty_tree(git, info.top)
                base = pin_state(git, info.top, f"flowproof in-place base: {label}", empty)
                tracker = _GitTracker(git, cwd=info.top, base=base, prefix=info.prefix, empty=empty)
            else:
                git.close()
                snapshot = _SnapshotTracker.take(root, label, None)
                base = snapshot.before.digest
                tracker = snapshot
        except BaseException:
            git.close()
            raise
        identity = _Identity(_new_lease_id(label), root, base, self.tier, label, root)
        return InPlaceLease(identity, tracker)


class GuardedLease(Lease):
    def __init__(self, identity: _Identity, guard: ReadOnlyGuard) -> None:
        _init_lease(self, identity)
        self.guard = guard
        self.violation: GuardViolation | None = None

    def _diff(self) -> Change:
        return listing_change(self.guard.check())

    def _release(self) -> None:
        self._released = True
        try:
            self.guard.__exit__(None, None, None)
        except GuardViolation as violation:
            self.violation = violation
            raise


class GuardedBackend:
    """Tier GUARDED: the caller's directory, fingerprinted and (by default) made read-only.

    `release()` restores permissions and raises `GuardViolation` if any
    fingerprinted file was added, removed or modified. Only hashes are kept, so
    `diff()` names changed files without their content.
    """

    name: str = "guarded"
    tier: Tier = Tier.GUARDED

    def __init__(self, *, protect: bool = True, exclude: Sequence[str] = DEFAULT_EXCLUDES) -> None:
        self.protect = protect
        self.exclude = tuple(exclude)

    def lease(self, source: Path, *, label: str) -> Lease:
        root = _existing_dir(source)
        guard = ReadOnlyGuard(root, exclude=self.exclude, protect=self.protect).__enter__()
        assert guard.before is not None
        identity = _Identity(
            _new_lease_id(label), root, guard.before.digest, self.tier, label, root
        )
        return GuardedLease(identity, guard)


class CopyLease(Lease):
    def __init__(self, identity: _Identity, tracker: _SnapshotTracker, container: Path) -> None:
        _init_lease(self, identity)
        self._tracker = tracker
        self._container = container

    def _diff(self) -> Change:
        return self._tracker.change()

    def _release(self) -> None:
        _remove_tree(self._container)
        self._tracker.close()


class CopyBackend:
    """Tier COPY: a private copy of the tree without VCS data, build output or caches.

    The base is the starting fingerprint digest. A second private copy of the
    starting content, next to the lease, lets `diff()` produce a unified diff.
    """

    name: str = "copy"
    tier: Tier = Tier.COPY

    def __init__(self, root: Path | None = None) -> None:
        self.root = root

    def lease(self, source: Path, *, label: str) -> Lease:
        src = _existing_dir(source)
        container = _make_container(self.root, label, src)
        try:
            tracker = _SnapshotTracker.take(src, label, self.root)
        except BaseException:
            _remove_tree(container)
            raise
        work = container / "tree"
        try:
            _copy_tree(tracker.snapshot, work)
        except BaseException:
            _remove_tree(container)
            tracker.close()
            raise
        tracker.live = work
        identity = _Identity(
            _new_lease_id(label), work, tracker.before.digest, self.tier, label, src
        )
        return CopyLease(identity, tracker, container)


class WorktreeLease(Lease):
    def __init__(
        self,
        identity: _Identity,
        tracker: _GitTracker,
        *,
        repo: RepoInfo,
        worktree: Path,
        git_dir: Path,
        ref: str,
        container: Path,
    ) -> None:
        _init_lease(self, identity)
        self._tracker = tracker
        self.repo = repo
        self.worktree = worktree
        self.git_dir = git_dir
        self.ref = ref
        self._container = container

    def _diff(self) -> Change:
        return self._tracker.change()

    def _release(self) -> None:
        git, top = self._tracker.git, self.repo.top
        if os.path.lexists(self.worktree):
            git.run(
                ["worktree", "remove", "--force", "--force", str(self.worktree)],
                cwd=top,
                check=False,
            )
            # Raises while a lock persists, keeping the ref and admin entry for a retry.
            _remove_tree(self.worktree)
        if self.git_dir.parent.name == "worktrees" and os.path.lexists(self.git_dir):
            _remove_tree(self.git_dir)
        git.run(["update-ref", "-d", self.ref], cwd=top, check=False)
        still = git.run(["rev-parse", "--verify", "--quiet", self.ref], cwd=top, check=False)
        if still.exit_code == 0:
            raise WorkspaceError(f"could not delete private ref {self.ref} in {top}")
        _remove_tree(self._container)
        self._tracker.close()


class WorktreeBackend:
    """Tier WORKTREE: a detached git worktree at a commit pinned from the current work tree.

    The pin includes uncommitted and untracked (non-ignored) files, is built with a
    scratch index so the caller's index is untouched, and is held by a private ref
    under `refs/flowproof/` until release. Leases live under `root` (default: the
    system temporary directory), never inside the source tree.
    """

    name: str = "worktree"
    tier: Tier = Tier.WORKTREE

    def __init__(self, root: Path | None = None) -> None:
        self.root = root

    def lease(self, source: Path, *, label: str) -> Lease:
        src = _existing_dir(source)
        git = Git()
        try:
            return self._lease(git, src, label)
        except BaseException:
            git.close()
            raise

    def _lease(self, git: Git, src: Path, label: str) -> Lease:
        info = repo_info(git, src)
        if info is None:
            raise WorkspaceError(f"{src} is not inside a git work tree")
        lease_id = _new_lease_id(label)
        container = _make_container(self.root, label, info.top)
        ref = f"refs/flowproof/{lease_id}"
        try:
            empty = empty_tree(git, info.top)
            commit = pin_state(git, info.top, f"flowproof base: {label}", empty)
            git.run(["update-ref", ref, commit, ""], cwd=info.top)
        except BaseException:
            _remove_tree(container)
            raise
        worktree = container / lease_id
        try:
            git.run(
                ["worktree", "add", "--detach", "--quiet", str(worktree), commit],
                cwd=info.top,
                env={"GIT_ATTR_SOURCE": empty},
            )
            git_dir = Path(git.text(["rev-parse", "--absolute-git-dir"], cwd=worktree)).resolve()
        except BaseException:
            remove = ["worktree", "remove", "--force", "--force", str(worktree)]
            git.run(remove, cwd=info.top, check=False)
            git.run(["update-ref", "-d", ref], cwd=info.top, check=False)
            _remove_tree(container)
            raise
        root = worktree.joinpath(*info.prefix.strip("/").split("/")) if info.prefix else worktree
        root.mkdir(parents=True, exist_ok=True)
        tracker = _GitTracker(
            git, cwd=worktree, base=commit, prefix=info.prefix, empty=empty, git_dir=git_dir
        )
        identity = _Identity(lease_id, root, commit, self.tier, label, src)
        return WorktreeLease(
            identity,
            tracker,
            repo=info,
            worktree=worktree,
            git_dir=git_dir,
            ref=ref,
            container=container,
        )


_BACKENDS: Mapping[str, Callable[[Path | None], Backend]] = {
    "inplace": lambda _root: InPlaceBackend(),
    "guarded": lambda _root: GuardedBackend(),
    "copy": lambda root: CopyBackend(root),
    "worktree": lambda root: WorktreeBackend(root),
}


def backend_for(name: str, *, lease_root: Path | None = None) -> Backend:
    """The backend for a binding name: `inplace`, `guarded`, `copy` or `worktree`.

    `lease_root` sets where copy and worktree leases are created.
    """
    factory = _BACKENDS.get(name)
    if factory is None:
        known = ", ".join(sorted(_BACKENDS))
        raise ValueError(f"unknown workspace backend {name!r}; expected one of: {known}")
    return factory(lease_root)


_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def _slug(label: str, limit: int = 24) -> str:
    text = _UNSAFE.sub("-", label).strip("-_")[:limit].strip("-_")
    return text or "lease"


def _new_lease_id(label: str) -> str:
    return f"{_slug(label)}-{secrets.token_hex(4)}"


def _existing_dir(source: Path) -> Path:
    path = Path(source)
    if not path.is_dir():
        raise WorkspaceError(f"source is not a directory: {path}")
    return path.resolve()


def _make_container(root: Path | None, label: str, source: Path) -> Path:
    parent = (Path(root) if root is not None else Path(tempfile.gettempdir())).resolve()
    tree = source.resolve()
    if parent == tree or tree in parent.parents:
        raise WorkspaceError(f"lease root {parent} must be outside the source tree {tree}")
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"flowproof-{_slug(label, 16)}-", dir=parent)).resolve()


def _copy_tree(source: Path, target: Path) -> None:
    excluded = frozenset(COPY_EXCLUDED_NAMES)

    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in excluded}

    try:
        shutil.copytree(source, target, symlinks=True, ignore=ignore)
    except (shutil.Error, OSError) as exc:
        raise WorkspaceError(f"could not copy {source} to {target}: {exc}") from exc


_REMOVERS = (os.unlink, os.rmdir, os.remove)


def _make_writable(func: Callable[..., object], target: str, error: object) -> None:
    parent = os.path.dirname(target)
    for path in (parent,) if os.path.islink(target) else (target, parent):
        try:
            mode = os.lstat(path).st_mode
            os.chmod(path, mode | stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
        except OSError:
            pass
    if func in _REMOVERS:
        func(target)
        return
    # rmtree could not scan or open the path; the outer retry loop tries again.
    if isinstance(error, BaseException):
        raise error
    if isinstance(error, tuple) and len(error) == 3 and isinstance(error[1], BaseException):
        raise error[1]
    raise OSError(f"cannot remove {target}")


def _rmtree(path: Path) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_make_writable)
    else:
        shutil.rmtree(path, onerror=_make_writable)


def _remove_tree(path: Path) -> None:
    """Remove a lease-owned path, retrying briefly while Windows holds file locks."""
    delay = 0.05
    error: OSError | None = None
    for attempt in range(_REMOVE_ATTEMPTS):
        if not os.path.lexists(path):
            return
        try:
            if is_junction(path):
                os.rmdir(path)
            elif path.is_symlink() or not path.is_dir():
                _make_writable(os.unlink, str(path), None)
            else:
                _rmtree(path)
        except OSError as exc:
            error = exc
            if attempt < _REMOVE_ATTEMPTS - 1:
                time.sleep(delay)
                delay = min(delay * 2, 1.0)
    if os.path.lexists(path):
        raise WorkspaceError(f"could not remove {path}: {error}")
