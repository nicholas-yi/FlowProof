"""What an agent changed: a unified diff plus sorted file lists."""

from __future__ import annotations

import difflib
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from flowproof.guards.fingerprint import Fingerprint, FingerprintDiff, compare


@dataclass(frozen=True)
class Change:
    """A unified diff (git `--binary` compatible where content is retained) and its files.

    Paths are relative to the lease root, use `/` and are sorted.
    """

    patch: str
    files: tuple[str, ...]
    added: tuple[str, ...]
    deleted: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not self.files

    @property
    def modified(self) -> tuple[str, ...]:
        gone = set(self.added) | set(self.deleted)
        return tuple(path for path in self.files if path not in gone)


@dataclass(frozen=True)
class _Entry:
    data: bytes
    mode: str


def snapshot_change(
    base_root: Path, before: Fingerprint, live_root: Path, after: Fingerprint
) -> Change:
    """Diff two directory states; `base_root` must still hold the `before` content."""
    diff = compare(before, after)
    sections: list[str] = []
    for path in diff.paths:
        old = None if path in diff.added else _read_entry(base_root, path)
        new = None if path in diff.removed else _read_entry(live_root, path)
        sections.append(_file_patch(path, old, new))
    return Change(patch="".join(sections), files=diff.paths, added=diff.added, deleted=diff.removed)


def listing_change(diff: FingerprintDiff) -> Change:
    """A change for a tier that keeps hashes but not content: files are marked as differing."""
    sections: list[str] = []
    for path in diff.paths:
        header = f"diff --git a/{path} b/{path}\n"
        if path in diff.added:
            header += f"new file mode 100644\nBinary files /dev/null and b/{path} differ\n"
        elif path in diff.removed:
            header += f"deleted file mode 100644\nBinary files a/{path} and /dev/null differ\n"
        else:
            header += f"Binary files a/{path} and b/{path} differ\n"
        sections.append(header)
    return Change(patch="".join(sections), files=diff.paths, added=diff.added, deleted=diff.removed)


def _read_entry(root: Path, rel: str) -> _Entry | None:
    full = root.joinpath(*rel.split("/"))
    try:
        if full.is_symlink():
            target = os.readlink(full)
            return _Entry(target.encode("utf-8", errors="surrogateescape"), "120000")
        data = full.read_bytes()
        mode = full.stat().st_mode
    except OSError:
        return None
    executable = sys.platform != "win32" and bool(mode & (stat.S_IXUSR | stat.S_IXGRP))
    return _Entry(data, "100755" if executable else "100644")


def _as_text(entry: _Entry | None) -> str | None:
    if entry is None:
        return ""
    if b"\0" in entry.data[:8192]:
        return None
    try:
        return entry.data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _split_lines(text: str) -> list[str]:
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _file_patch(path: str, old: _Entry | None, new: _Entry | None) -> str:
    a_name = f"a/{path}" if old is not None else "/dev/null"
    b_name = f"b/{path}" if new is not None else "/dev/null"
    out = [f"diff --git a/{path} b/{path}\n"]
    if old is None and new is not None:
        out.append(f"new file mode {new.mode}\n")
    elif new is None and old is not None:
        out.append(f"deleted file mode {old.mode}\n")
    elif old is not None and new is not None and old.mode != new.mode:
        out.append(f"old mode {old.mode}\nnew mode {new.mode}\n")
    old_text, new_text = _as_text(old), _as_text(new)
    if old_text is None or new_text is None:
        out.append(f"Binary files {a_name} and {b_name} differ\n")
        return "".join(out)
    old_lines, new_lines = _split_lines(old_text), _split_lines(new_text)
    for line in difflib.unified_diff(
        old_lines, new_lines, fromfile=a_name, tofile=b_name, lineterm="\n"
    ):
        if line.endswith("\n"):
            out.append(line)
        else:
            out.append(line + "\n\\ No newline at end of file\n")
    return "".join(out)
