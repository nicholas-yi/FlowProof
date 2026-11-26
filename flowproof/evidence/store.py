"""Sealed, hash-chained run records that can be verified from any directory on any OS."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FORMAT = "flowproof-evidence"
VERSION = 1
MANIFEST = "manifest.json"
EVENTS = "events.jsonl"
ZERO_HASH = "0" * 64
RESERVED_EVENT_FIELDS = frozenset({"seq", "kind", "at", "prev"})

RUN_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f\x7f]')
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


class EvidenceError(Exception):
    """An evidence write was refused or a run could not be created or sealed."""


@dataclass(frozen=True)
class VerifyReport:
    ok: bool
    problems: tuple[str, ...]


def canonical_json(value: Any) -> str:
    """Sorted keys, no whitespace, UTF-8 text, no NaN or infinities."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def pretty_json(value: Any) -> str:
    """Sorted keys, indent 2, trailing newline."""
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)


def check_relative_path(rel: str) -> str:
    """Return `rel` if it is a safe, portable relative `/` path; raise `EvidenceError` if not."""
    if not isinstance(rel, str) or not rel:
        raise EvidenceError("evidence path must be a non-empty string")
    if "\\" in rel:
        raise EvidenceError(f"evidence path must use '/', not backslashes: {rel!r}")
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise EvidenceError(f"evidence path must be relative, not absolute: {rel!r}")
    for part in rel.split("/"):
        if part == "..":
            raise EvidenceError(f"evidence path must not contain '..': {rel!r}")
        if part in ("", "."):
            raise EvidenceError(f"evidence path has an empty or '.' segment: {rel!r}")
        if _BAD_CHARS.search(part):
            raise EvidenceError(f"evidence path has a character that is not portable: {rel!r}")
        if part.endswith((" ", ".")):
            raise EvidenceError(f"evidence path segment ends with a space or dot: {rel!r}")
        if part.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
            raise EvidenceError(f"evidence path uses a reserved device name: {rel!r}")
    return rel


def _list_files(root: Path) -> tuple[set[str], set[str]]:
    """(regular files, other entries such as links) under `root`, as relative `/` paths."""
    files: set[str] = set()
    others: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        prefix = "" if rel_dir == "." else rel_dir + "/"
        for name in list(dirnames):
            if os.path.islink(os.path.join(dirpath, name)):
                others.add(prefix + name)
                dirnames.remove(name)
        for name in filenames:
            full = os.path.join(dirpath, name)
            (others if os.path.islink(full) else files).add(prefix + name)
    return files, others


def _scan_chain(data: bytes) -> tuple[list[str], str, int]:
    """Check an events.jsonl body. Returns (problems, head hash, line count)."""
    problems: list[str] = []
    if data and not data.endswith(b"\n"):
        problems.append(f"{EVENTS} does not end with a newline")
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    prev = ZERO_HASH
    for number, line in enumerate(lines, 1):
        expected_seq = number
        try:
            event = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            problems.append(f"{EVENTS} line {number} is not valid UTF-8 JSON")
            prev = _sha256(line)
            continue
        if not isinstance(event, dict):
            problems.append(f"{EVENTS} line {number} is not a JSON object")
        else:
            try:
                canonical = canonical_json(event).encode("utf-8") == line
            except ValueError:
                canonical = False
            if not canonical:
                problems.append(f"{EVENTS} line {number} is not canonical JSON")
            seq = event.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq != expected_seq:
                problems.append(f"{EVENTS} line {number} has seq {seq!r}, expected {expected_seq}")
            if event.get("prev") != prev:
                problems.append(
                    f"{EVENTS} line {number} does not link to the previous line (prev mismatch)"
                )
            if not isinstance(event.get("kind"), str) or not isinstance(event.get("at"), str):
                problems.append(f"{EVENTS} line {number} lacks a string kind or at")
        prev = _sha256(line)
    return problems, prev, len(lines)


class RunRecord:
    """One run's evidence directory. Files are write-once; events are hash-chained.

    Created by `EvidenceStore.create_run`, which records a first `run-created` event.
    """

    def __init__(self, id: str, root: Path, label: str) -> None:
        self.id = id
        self.root = Path(root)
        self.label = label
        self._files: dict[str, str] = {}
        self._folded: set[str] = set()
        self._dirs: set[str] = set()
        self._seq = 0
        self._head = ZERO_HASH
        self._sealed = False
        self._lock = threading.Lock()

    @property
    def sealed(self) -> bool:
        return self._sealed

    @property
    def head(self) -> str:
        """sha256 of the last event line, or 64 zeros before the first event."""
        return self._head

    def write_bytes(self, rel: str, data: bytes) -> str:
        """Write a new file and return its sha256. Never overwrites."""
        payload = bytes(data)
        with self._lock:
            self._require_open()
            path = check_relative_path(rel)
            if path in (MANIFEST, EVENTS):
                raise EvidenceError(f"{path} is written by the record itself")
            folded = path.casefold()
            parts = folded.split("/")
            prefixes = {"/".join(parts[:i]) for i in range(1, len(parts))}
            if folded in self._folded or folded in self._dirs:
                raise EvidenceError(f"refusing to overwrite evidence file {path!r}")
            if prefixes & self._folded:
                raise EvidenceError(f"a parent of {path!r} is already an evidence file")
            target = self.root.joinpath(*path.split("/"))
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "xb") as handle:
                    handle.write(payload)
            except FileExistsError as exc:
                raise EvidenceError(f"refusing to overwrite evidence file {path!r}") from exc
            except OSError as exc:
                raise EvidenceError(f"could not write evidence file {path!r}: {exc}") from exc
            digest = _sha256(payload)
            self._files[path] = digest
            self._folded.add(folded)
            self._dirs |= prefixes
            return digest

    def write_text(self, rel: str, text: str) -> str:
        """Write `text` as UTF-8 exactly as given (no newline translation)."""
        return self.write_bytes(rel, text.encode("utf-8"))

    def write_json(self, rel: str, value: Any) -> str:
        """Write JSON with sorted keys, indent 2 and a trailing newline."""
        try:
            text = pretty_json(value)
        except (TypeError, ValueError) as exc:
            raise EvidenceError(f"value for {rel!r} is not JSON-serialisable: {exc}") from exc
        return self.write_text(rel, text)

    def event(self, kind: str, **fields: Any) -> str:
        """Append a hash-chained event line and return its sha256 (the new chain head)."""
        if not isinstance(kind, str) or not kind:
            raise EvidenceError("event kind must be a non-empty string")
        clash = RESERVED_EVENT_FIELDS & fields.keys()
        if clash:
            raise EvidenceError(f"event fields may not override {sorted(clash)}")
        with self._lock:
            self._require_open()
            record = {
                **fields,
                "seq": self._seq + 1,
                "kind": kind,
                "at": _now(),
                "prev": self._head,
            }
            try:
                line = canonical_json(record).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise EvidenceError(f"event {kind!r} is not JSON-serialisable: {exc}") from exc
            try:
                with open(self.root / EVENTS, "ab") as handle:
                    handle.write(line + b"\n")
            except OSError as exc:
                raise EvidenceError(f"could not append event {kind!r}: {exc}") from exc
            self._seq += 1
            self._head = _sha256(line)
            return self._head

    def seal(self) -> str:
        """Write manifest.json, make the files read-only and return the manifest's sha256.

        Refuses if the directory holds files this record did not write, or if any
        written file or event line changed since it was written.
        """
        with self._lock:
            self._require_open()
            files, others = _list_files(self.root)
            expected = set(self._files) | ({EVENTS} if self._seq else set())
            unexpected = sorted((files - expected) | others)
            missing = sorted(expected - files)
            if unexpected or missing:
                raise EvidenceError(
                    f"cannot seal {self.id}: unexpected {unexpected}, missing {missing}"
                )
            entries: list[dict[str, Any]] = []
            for path in sorted(files):
                data = self.root.joinpath(*path.split("/")).read_bytes()
                digest = _sha256(data)
                if path in self._files and self._files[path] != digest:
                    raise EvidenceError(
                        f"cannot seal {self.id}: {path} changed after it was written"
                    )
                if path == EVENTS:
                    problems, head, count = _scan_chain(data)
                    if problems or head != self._head or count != self._seq:
                        raise EvidenceError(f"cannot seal {self.id}: the event chain was altered")
                entries.append({"path": path, "bytes": len(data), "sha256": digest})
            manifest = {
                "format": FORMAT,
                "version": VERSION,
                "id": self.id,
                "label": self.label,
                "files": entries,
                "chain": {"head": self._head, "length": self._seq},
                "sealed_at": _now(),
            }
            data = pretty_json(manifest).encode("utf-8")
            try:
                with open(self.root / MANIFEST, "xb") as handle:
                    handle.write(data)
            except OSError as exc:
                raise EvidenceError(f"could not write {MANIFEST}: {exc}") from exc
            self._sealed = True
            for path in [*files, MANIFEST]:
                _make_read_only(self.root.joinpath(*path.split("/")))
            return _sha256(data)

    def _require_open(self) -> None:
        if self._sealed:
            raise EvidenceError(f"run {self.id} is sealed; it can no longer be written")


def _make_read_only(path: Path) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        os.chmod(path, mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    except OSError:
        pass


class EvidenceStore:
    """A directory of runs, one subdirectory per run id."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def create_run(self, label: str) -> RunRecord:
        """A new run directory named `YYYYMMDDTHHMMSSZ-<8 hex>`; fails if it already exists."""
        if not isinstance(label, str):
            raise EvidenceError("run label must be a string")
        self.root.mkdir(parents=True, exist_ok=True)
        run_id = _new_run_id()
        path = self.root / run_id
        try:
            path.mkdir()
        except FileExistsError as exc:
            raise EvidenceError(f"run directory already exists: {path}") from exc
        record = RunRecord(run_id, path, label)
        record.event("run-created", id=run_id, label=label)
        return record

    def path(self, run_id: str) -> Path:
        if not RUN_ID.match(run_id):
            raise EvidenceError(f"not a run id: {run_id!r}")
        return self.root / run_id


def verify(run_root: Path, *, expected_manifest_sha256: str | None = None) -> VerifyReport:
    """Recompute every hash, length and chain link of a sealed run. Never raises for bad input.

    `expected_manifest_sha256` (the value `seal()` returned) also pins the
    manifest itself, which otherwise only vouches for the other files.
    """
    root = Path(run_root)
    problems: list[str] = []
    manifest = _load_manifest(root, expected_manifest_sha256, problems)
    if manifest is None:
        return VerifyReport(False, tuple(problems))
    listed = _listed_files(manifest, problems)
    events = _check_files(root, listed, problems)
    _check_chain(manifest, listed, events, problems)
    return VerifyReport(not problems, tuple(problems))


def _load_manifest(
    root: Path, expected_sha256: str | None, problems: list[str]
) -> dict[str, Any] | None:
    if not root.is_dir():
        problems.append(f"run directory not found: {root}")
        return None
    try:
        raw = (root / MANIFEST).read_bytes()
    except FileNotFoundError:
        problems.append(f"{MANIFEST} is missing (the run was never sealed)")
        return None
    except OSError as exc:
        problems.append(f"{MANIFEST} could not be read: {exc}")
        return None
    if expected_sha256 is not None and _sha256(raw) != expected_sha256:
        problems.append(f"{MANIFEST} does not match the expected sha256")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        problems.append(f"{MANIFEST} is not valid JSON: {exc}")
        return None
    if not isinstance(manifest, dict):
        problems.append(f"{MANIFEST} is not a JSON object")
        return None
    if manifest.get("format") != FORMAT:
        problems.append(f"{MANIFEST} format is {manifest.get('format')!r}, expected {FORMAT!r}")
    if manifest.get("version") != VERSION:
        problems.append(f"{MANIFEST} version {manifest.get('version')!r} is not supported")
    return manifest


def _listed_files(manifest: dict[str, Any], problems: list[str]) -> dict[str, tuple[int, str]]:
    listed: dict[str, tuple[int, str]] = {}
    entries = manifest.get("files")
    if not isinstance(entries, list):
        problems.append(f"{MANIFEST} has no file list")
        return listed
    for entry in entries:
        path = entry.get("path") if isinstance(entry, dict) else None
        size = entry.get("bytes") if isinstance(entry, dict) else None
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        if (
            not isinstance(path, str)
            or not isinstance(size, int)
            or not isinstance(digest, str)
            or not _SHA256.match(digest)
        ):
            problems.append(f"{MANIFEST} has a malformed file entry: {entry!r}")
            continue
        try:
            check_relative_path(path)
        except EvidenceError:
            problems.append(f"{MANIFEST} lists an unsafe path: {path!r}")
            continue
        if path in listed or path == MANIFEST:
            problems.append(f"{MANIFEST} lists {path!r} more than once or lists itself")
            continue
        listed[path] = (size, digest)
    return listed


def _check_files(
    root: Path, listed: dict[str, tuple[int, str]], problems: list[str]
) -> bytes | None:
    """Compare listed files with the disk; returns the events.jsonl bytes when present."""
    files, others = _list_files(root)
    files.discard(MANIFEST)
    events: bytes | None = None
    for path in sorted(listed):
        size, digest = listed[path]
        if path not in files:
            problems.append(f"missing file: {path}")
            continue
        try:
            data = root.joinpath(*path.split("/")).read_bytes()
        except OSError as exc:
            problems.append(f"unreadable file: {path}: {exc}")
            continue
        if len(data) != size:
            problems.append(f"size mismatch: {path} has {len(data)} bytes, manifest says {size}")
        if _sha256(data) != digest:
            problems.append(f"content mismatch: {path} does not match its sha256")
        if path == EVENTS:
            events = data
    problems.extend(f"unlisted file: {path}" for path in sorted((files - listed.keys()) | others))
    return events


def _check_chain(
    manifest: dict[str, Any],
    listed: dict[str, tuple[int, str]],
    events: bytes | None,
    problems: list[str],
) -> None:
    chain = manifest.get("chain")
    if not isinstance(chain, dict):
        problems.append(f"{MANIFEST} has no chain record")
        chain = {}
    length = chain.get("length")
    if events is None:
        # A listed-but-absent events.jsonl was already reported as missing.
        if EVENTS not in listed and (length != 0 or chain.get("head") != ZERO_HASH):
            problems.append(f"{EVENTS} is not listed but the manifest records events")
        return
    chain_problems, head, count = _scan_chain(events)
    problems.extend(chain_problems)
    if head != chain.get("head"):
        problems.append("event chain head does not match the manifest")
    if count != length:
        problems.append(f"event chain has {count} lines, manifest says {length!r}")


def supersede(store: EvidenceStore, old_id: str, new_id: str, reason: str) -> str:
    """Record, in a new sealed run, that `new_id` supersedes `old_id`. Returns the new run's id.

    The old run is never modified. Each side's manifest sha256 is recorded when
    that run is sealed, so the link names exact content.
    """
    if old_id == new_id:
        raise EvidenceError("a run cannot supersede itself")
    if not isinstance(reason, str) or not reason.strip():
        raise EvidenceError("a supersession needs a reason")
    digests: dict[str, str | None] = {}
    for run_id in (old_id, new_id):
        path = store.path(run_id)
        if not path.is_dir():
            raise EvidenceError(f"unknown run {run_id!r} in {store.root}")
        manifest = path / MANIFEST
        digests[run_id] = _sha256(manifest.read_bytes()) if manifest.is_file() else None
    record = store.create_run(f"supersede {old_id}")
    record.write_json(
        "supersede.json",
        {
            "kind": "supersede",
            "old": old_id,
            "new": new_id,
            "reason": reason,
            "old_manifest_sha256": digests[old_id],
            "new_manifest_sha256": digests[new_id],
        },
    )
    record.event("superseded", old=old_id, new=new_id, reason=reason)
    record.seal()
    return record.id
