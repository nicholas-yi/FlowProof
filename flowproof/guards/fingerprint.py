"""Exact-byte fingerprints of directory trees, and a guard that proves a tree was not changed."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Any

DEFAULT_EXCLUDES: tuple[str, ...] = (
    "**/.git",
    "**/.git/**",
    "**/.hg/**",
    "**/.svn/**",
    "**/.venv/**",
    "**/node_modules/**",
    "**/__pycache__/**",
    "**/.pytest_cache/**",
    "**/.mypy_cache/**",
    "**/.ruff_cache/**",
    "**/.hypothesis/**",
    "**/.tox/**",
    "**/.nox/**",
    "**/build/**",
    "**/dist/**",
    "**/*.egg-info/**",
    "**/.flowproof/**",
)

_CHUNK = 1 << 20
_LINK_TAG = b"flowproof-symlink\0"


def normalize_pattern(pattern: str) -> str:
    """Use `/` separators, drop a leading `./`, and read a trailing `/` as `/**`."""
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError(f"empty glob pattern: {pattern!r}")
    text = pattern.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if text.endswith("/"):
        text += "**"
    return text


def translate_glob(pattern: str) -> str:
    """Translate a path glob to a regular expression for whole relative paths.

    `*` and `?` stay inside one path segment, `**` as a whole segment matches any
    number of segments (including none before a further segment), and `[...]` is a
    character class. A pattern without `/` matches only at the top level.
    """
    parts = normalize_pattern(pattern).split("/")
    out: list[str] = []
    for index, segment in enumerate(parts):
        last = index == len(parts) - 1
        if segment == "**":
            out.append(".*" if last else "(?:[^/]+/)*")
        else:
            out.append(_segment(segment))
            if not last:
                out.append("/")
    return "".join(out)


def _segment(segment: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(segment):
        char = segment[i]
        if char == "*":
            while i + 1 < len(segment) and segment[i + 1] == "*":
                i += 1
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = segment.find("]", i + 2)
            if end == -1:
                out.append(re.escape(char))
            else:
                body = segment[i + 1 : end].replace("\\", "\\\\")
                if body.startswith("!"):
                    out.append(f"[^{body[1:]}/]")
                else:
                    out.append(f"[{body}]")
                i = end
        else:
            out.append(re.escape(char))
        i += 1
    return "".join(out)


def glob_match(path: str, pattern: str) -> bool:
    """Whether relative `/` path `path` matches `pattern` (see `translate_glob`)."""
    return re.fullmatch(translate_glob(pattern), path) is not None


class PathFilter:
    """Include/exclude globs compiled once, with directory pruning for `dir/**` excludes."""

    def __init__(self, include: Iterable[str], exclude: Iterable[str]) -> None:
        if isinstance(include, str) or isinstance(exclude, str):
            raise TypeError("include and exclude must be sequences of globs, not strings")
        exclude = tuple(normalize_pattern(p) for p in exclude)
        self._include = [re.compile(translate_glob(p)) for p in include]
        self._exclude = [re.compile(translate_glob(p)) for p in exclude]
        self._prune = [
            re.compile(translate_glob(p[:-3])) for p in exclude if p.endswith("/**") and len(p) > 3
        ]

    def wants(self, path: str) -> bool:
        if not any(rx.fullmatch(path) for rx in self._include):
            return False
        return not any(rx.fullmatch(path) for rx in self._exclude)

    def matches_any(self, path: str) -> bool:
        return any(rx.fullmatch(path) for rx in self._include)

    def prunes(self, directory: str) -> bool:
        return any(rx.fullmatch(directory) for rx in self._prune)


@dataclass(frozen=True)
class Fingerprint:
    """Relative `/` path -> sha256 of the file's exact bytes, in sorted path order.

    A symbolic link is hashed by its target text (tagged), never followed.
    """

    files: Mapping[str, str]

    def __post_init__(self) -> None:
        ordered = dict(sorted(dict(self.files).items()))
        object.__setattr__(self, "files", MappingProxyType(ordered))

    @property
    def digest(self) -> str:
        """One sha256 over every (path, hash) pair; equal trees have equal digests."""
        hasher = hashlib.sha256()
        for path, digest in self.files.items():
            hasher.update(path.encode("utf-8", errors="surrogateescape"))
            hasher.update(b"\0")
            hasher.update(digest.encode("ascii"))
            hasher.update(b"\n")
        return hasher.hexdigest()

    def to_json(self) -> dict[str, Any]:
        return {"digest": self.digest, "files": dict(self.files)}


def fingerprint(
    root: Path,
    *,
    include: Sequence[str] = ("**",),
    exclude: Sequence[str] = DEFAULT_EXCLUDES,
) -> Fingerprint:
    """Hash every file under `root` selected by the globs. Symlinked directories are not entered."""
    base = Path(root)
    if not base.is_dir():
        raise NotADirectoryError(f"not a directory: {base}")
    selector = PathFilter(include, exclude)
    files: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        rel_dir = Path(dirpath).relative_to(base).as_posix()
        prefix = "" if rel_dir == "." else rel_dir + "/"
        keep: list[str] = []
        for name in sorted(dirnames):
            rel = prefix + name
            full = os.path.join(dirpath, name)
            if os.path.islink(full) or is_junction(full):
                if selector.wants(rel):
                    files[rel] = _link_digest(full)
                continue
            if not selector.prunes(rel):
                keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            rel = prefix + name
            if not selector.wants(rel):
                continue
            full = os.path.join(dirpath, name)
            files[rel] = _link_digest(full) if os.path.islink(full) else file_digest(full)
    return Fingerprint(files)


def file_digest(path: str | Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            hasher.update(chunk)
    return hasher.hexdigest()


def _link_digest(path: str) -> str:
    try:
        target = os.readlink(path)
    except OSError:
        target = ""
    return hashlib.sha256(_LINK_TAG + target.encode("utf-8", errors="surrogateescape")).hexdigest()


def is_junction(path: str | Path) -> bool:
    if sys.version_info >= (3, 12):
        return os.path.isjunction(path)
    return False


@dataclass(frozen=True)
class FingerprintDiff:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    modified: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not (self.added or self.removed or self.modified)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted({*self.added, *self.removed, *self.modified}))

    def summary(self, limit: int = 10) -> str:
        parts: list[str] = []
        for label, paths in (
            ("added", self.added),
            ("removed", self.removed),
            ("modified", self.modified),
        ):
            if paths:
                shown = ", ".join(paths[:limit])
                more = f" (+{len(paths) - limit} more)" if len(paths) > limit else ""
                parts.append(f"{label}: {shown}{more}")
        return "; ".join(parts) or "no changes"

    def to_json(self) -> dict[str, list[str]]:
        return {
            "added": list(self.added),
            "removed": list(self.removed),
            "modified": list(self.modified),
        }


def compare(before: Fingerprint, after: Fingerprint) -> FingerprintDiff:
    old, new = before.files, after.files
    return FingerprintDiff(
        added=tuple(sorted(p for p in new if p not in old)),
        removed=tuple(sorted(p for p in old if p not in new)),
        modified=tuple(sorted(p for p in new if p in old and new[p] != old[p])),
    )


class GuardViolation(Exception):
    """A tree that was supposed to stay unchanged was changed."""

    def __init__(
        self, message: str, *, diff: FingerprintDiff | None = None, root: Path | None = None
    ) -> None:
        super().__init__(message)
        self.diff = diff
        self.root = root


_WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


class ReadOnlyGuard:
    """Fingerprint a directory on entry and raise `GuardViolation` on exit if it changed.

    With `protect` (the default) the fingerprinted files also lose their write
    permission for the duration and get their original modes back on exit. That
    stops accidental writes; it is not a security boundary, because any process
    running as the same user can restore the permission, and new files can still
    be created. The fingerprint comparison is what decides a violation.
    """

    def __init__(
        self,
        root: Path,
        *,
        include: Sequence[str] = ("**",),
        exclude: Sequence[str] = DEFAULT_EXCLUDES,
        protect: bool = True,
    ) -> None:
        self.root = Path(root)
        self.include = tuple(include)
        self.exclude = tuple(exclude)
        self.protect = protect
        self.before: Fingerprint | None = None
        self.diff: FingerprintDiff | None = None
        self._modes: dict[str, int] = {}

    def __enter__(self) -> ReadOnlyGuard:
        if self.before is not None:
            raise RuntimeError("a ReadOnlyGuard can be entered only once")
        self.before = fingerprint(self.root, include=self.include, exclude=self.exclude)
        if self.protect:
            try:
                self._make_read_only(self.before)
            except BaseException:
                self._restore_modes()
                raise
        return self

    def check(self) -> FingerprintDiff:
        """Compare the tree now with the fingerprint taken on entry, without closing."""
        if self.before is None:
            raise RuntimeError("the guard has not been entered")
        now = fingerprint(self.root, include=self.include, exclude=self.exclude)
        return compare(self.before, now)

    def close(self) -> FingerprintDiff:
        """Restore permissions and return the final comparison. Repeated calls return it again."""
        if self.diff is None:
            self._restore_modes()
            self.diff = self.check()
        return self.diff

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        diff = self.close()
        if not diff.clean:
            raise GuardViolation(
                f"guarded directory {self.root} changed: {diff.summary()}",
                diff=diff,
                root=self.root,
            )

    def _make_read_only(self, before: Fingerprint) -> None:
        for rel in before.files:
            full = self.root.joinpath(*rel.split("/"))
            try:
                if full.is_symlink():
                    continue
                mode = stat.S_IMODE(full.stat().st_mode)
                if mode & _WRITE_BITS:
                    os.chmod(full, mode & ~_WRITE_BITS)
                    self._modes[rel] = mode
            except OSError:
                continue

    def _restore_modes(self) -> None:
        modes, self._modes = self._modes, {}
        for rel, mode in modes.items():
            full = self.root.joinpath(*rel.split("/"))
            try:
                if full.exists() and not full.is_symlink():
                    os.chmod(full, mode)
            except OSError:
                continue
