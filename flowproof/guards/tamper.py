"""Frozen tests: capture the grader's files before an agent runs, then report and undo tampering."""

from __future__ import annotations

import contextlib
import difflib
import glob
import hashlib
import json
import os
import shutil
import stat
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flowproof.guards.fingerprint import (
    DEFAULT_EXCLUDES,
    Fingerprint,
    PathFilter,
    fingerprint,
    is_junction,
    normalize_pattern,
)
from flowproof.guards.signals import EARLY_EXIT, SKIP_MARKER, TRIVIAL_ASSERT, scannable, signals

MODIFIED_TEST = "modified-test"
DELETED_TEST = "deleted-test"
ADDED_TEST_FILE = "added-test-file"
CONFIG_OVERRIDE = "heuristic-config-override"
HEURISTIC_PREFIX = "heuristic-"

# Any change to these files is reported and undone by `restore`.
PURE_CONFIG_PATTERNS: tuple[str, ...] = (
    "**/conftest.py",
    "**/pytest.ini",
    "**/.pytest.ini",
    "**/tox.ini",
    "**/jest.config.*",
    "**/vitest.config.*",
)
# Only the test-related part of these files counts: `[tool.pytest*]` tables in
# pyproject.toml, `[tool:pytest]` in setup.cfg, test scripts and `jest` in package.json.
SECTION_CONFIG_PATTERNS: tuple[str, ...] = (
    "**/pyproject.toml",
    "**/setup.cfg",
    "**/package.json",
)
TEST_CONFIG_PATTERNS: tuple[str, ...] = PURE_CONFIG_PATTERNS + SECTION_CONFIG_PATTERNS

TEST_FILE_PATTERNS: tuple[str, ...] = (
    "**/test_*.py",
    "**/*_test.py",
    "**/tests/**",
    "**/test/**",
    "**/conftest.py",
    "**/*.test.*",
    "**/*.spec.*",
    "**/__tests__/**",
)

_EXCERPT_LIMIT = 160

__all__ = [
    "ADDED_TEST_FILE",
    "CONFIG_OVERRIDE",
    "DELETED_TEST",
    "EARLY_EXIT",
    "HEURISTIC_PREFIX",
    "MODIFIED_TEST",
    "PURE_CONFIG_PATTERNS",
    "SECTION_CONFIG_PATTERNS",
    "SKIP_MARKER",
    "TEST_CONFIG_PATTERNS",
    "TEST_FILE_PATTERNS",
    "TRIVIAL_ASSERT",
    "FrozenTests",
    "TamperFinding",
    "TamperReport",
]


@dataclass(frozen=True)
class TamperFinding:
    """One sign that the grader's files were changed. Heuristic kinds start with `heuristic-`."""

    kind: str
    path: str
    evidence: str

    @property
    def definitive(self) -> bool:
        return not self.kind.startswith(HEURISTIC_PREFIX)

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "evidence": self.evidence,
            "definitive": self.definitive,
        }


@dataclass(frozen=True)
class TamperReport:
    findings: tuple[TamperFinding, ...]

    @property
    def tampered(self) -> bool:
        """True when a frozen file under the patterns was modified, deleted or added to."""
        return any(f.definitive for f in self.findings)

    @property
    def definitive(self) -> tuple[TamperFinding, ...]:
        return tuple(f for f in self.findings if f.definitive)

    @property
    def heuristic(self) -> tuple[TamperFinding, ...]:
        return tuple(f for f in self.findings if not f.definitive)

    @property
    def suspicious(self) -> bool:
        """True when any heuristic finding was raised."""
        return any(not f.definitive for f in self.findings)

    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted({f.kind for f in self.findings}))

    def to_json(self) -> dict[str, Any]:
        return {
            "tampered": self.tampered,
            "suspicious": self.suspicious,
            "findings": [f.to_json() for f in self.findings],
        }


class FrozenTests:
    """The exact bytes of the grader's files, captured from the base tree.

    The frozen set is every file under `patterns` plus every test configuration
    file (`TEST_CONFIG_PATTERNS`) present in the base. Caches and VCS data
    (`DEFAULT_EXCLUDES`) are never frozen.
    """

    def __init__(self, patterns: Sequence[str]) -> None:
        if isinstance(patterns, str):
            raise TypeError("patterns must be a sequence of globs, not a single string")
        self.patterns: tuple[str, ...] = tuple(normalize_pattern(p) for p in patterns)
        if not self.patterns:
            raise ValueError("at least one frozen pattern is required")
        self.exclude: tuple[str, ...] = DEFAULT_EXCLUDES
        self._in_patterns = PathFilter(self.patterns, ())
        self._configs = PathFilter(TEST_CONFIG_PATTERNS, ())
        self._pure_configs = PathFilter(PURE_CONFIG_PATTERNS, ())
        self._test_files = PathFilter(TEST_FILE_PATTERNS, ())
        self._frozen: dict[str, bytes] = {}
        self._base: Fingerprint | None = None
        self._base_signals: dict[str, tuple[tuple[str, str], ...]] = {}

    @classmethod
    def capture(cls, base_root: Path, patterns: Sequence[str]) -> FrozenTests:
        """Freeze the files under `patterns` (and test configuration) as they are in `base_root`.

        Capture before the agent starts, from the tree it will start from.
        """
        frozen = cls(patterns)
        frozen._capture(Path(base_root))
        return frozen

    @property
    def captured(self) -> bool:
        return self._base is not None

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._frozen))

    def manifest(self) -> dict[str, str]:
        """Frozen path -> sha256 of the captured bytes, for evidence records."""
        return {path: _sha256(data) for path, data in sorted(self._frozen.items())}

    def inspect(self, lease_root: Path) -> TamperReport:
        """Report tampering in `lease_root` without writing anything."""
        findings, _ = self._analyse(Path(lease_root))
        return TamperReport(tuple(findings))

    def restore(self, lease_root: Path) -> TamperReport:
        """Report as `inspect` does, then put the frozen state back.

        Writes frozen bytes over modified or deleted frozen files, deletes files
        added under the patterns, and undoes the test configuration changes that
        were reported. Every overwrite corresponds to a finding in the report.
        """
        root = Path(lease_root)
        findings, actions = self._analyse(root)
        for path, data in sorted(actions.items()):
            if data is None:
                _remove_file(root, path)
            else:
                _write_file(root, path, data)
            if path.endswith(".py"):
                _purge_bytecode(root, path)
        return TamperReport(tuple(findings))

    def _capture(self, root: Path) -> None:
        base = fingerprint(root, exclude=self.exclude)
        hashes = dict(base.files)
        for path in base.files:
            freeze = self._in_patterns.wants(path) or self._configs.wants(path)
            if not (freeze or scannable(path)):
                continue
            full = root.joinpath(*path.split("/"))
            if freeze:
                data = full.read_bytes()
                self._frozen[path] = data
                if not full.is_symlink():
                    hashes[path] = _sha256(data)
            else:
                data = _read_bytes(root, path)
                if data is None:
                    continue
            found = signals(path, data, test_file=self._is_test_file(path))
            if found:
                self._base_signals[path] = tuple((s.kind, s.line) for s in found)
        self._base = Fingerprint(hashes)

    def _is_test_file(self, path: str) -> bool:
        return self._in_patterns.wants(path) or self._test_files.wants(path)

    def _analyse(self, root: Path) -> tuple[list[TamperFinding], dict[str, bytes | None]]:
        base = self._base
        if base is None:
            raise ValueError("FrozenTests has no captured base; use FrozenTests.capture()")
        now = fingerprint(root, exclude=self.exclude).files
        findings: list[TamperFinding] = []
        actions: dict[str, bytes | None] = {}

        for path, data in self._frozen.items():
            if not self._in_patterns.wants(path):
                continue
            if path not in now:
                findings.append(TamperFinding(DELETED_TEST, path, f"deleted ({len(data)} bytes)"))
                actions[path] = data
            elif now[path] != base.files[path]:
                current = _read_bytes(root, path) or b""
                findings.append(TamperFinding(MODIFIED_TEST, path, _change_excerpt(data, current)))
                actions[path] = data
        for path in now:
            if path not in self._frozen and self._in_patterns.wants(path):
                excerpt = _added_excerpt(_read_bytes(root, path) or b"")
                findings.append(TamperFinding(ADDED_TEST_FILE, path, excerpt))
                actions[path] = None

        configs = {p for p in self._frozen if self._configs.wants(p)}
        configs |= {p for p in now if self._configs.wants(p)}
        for path in sorted(configs):
            if self._in_patterns.wants(path):
                continue
            if path in self._frozen and now.get(path) == base.files.get(path):
                continue
            old = self._frozen.get(path)
            new = _read_bytes(root, path) if path in now else None
            evidence = self._config_change(path, old, new)
            if evidence is not None:
                findings.append(TamperFinding(CONFIG_OVERRIDE, path, evidence))
                actions[path] = old

        for path, digest in now.items():
            if base.files.get(path) == digest or not scannable(path):
                continue
            data = _read_bytes(root, path)
            if data is not None:
                findings.extend(self._signal_findings(path, data))

        findings.sort(key=lambda f: (not f.definitive, f.path, f.kind))
        return findings, actions

    def _signal_findings(self, path: str, data: bytes) -> list[TamperFinding]:
        already = Counter(self._base_signals.get(path, ()))
        out: list[TamperFinding] = []
        for found in signals(path, data, test_file=self._is_test_file(path)):
            key = (found.kind, found.line)
            if already[key] > 0:
                already[key] -= 1
                continue
            out.append(TamperFinding(found.kind, path, _clip(f"line {found.lineno}: {found.line}")))
        return out

    def _config_change(self, path: str, old: bytes | None, new: bytes | None) -> str | None:
        if old == new:
            return None
        if self._pure_configs.wants(path):
            if old is None:
                return _clip(f"test configuration added: {_first_line(new or b'')}")
            if new is None:
                return "test configuration deleted"
            return _clip(f"test configuration modified: {_change_excerpt(old, new)}")
        label = _section_label(path)
        before = _test_section(path, old) if old is not None else None
        after = _test_section(path, new) if new is not None else None
        if before == after:
            return None
        if before is None:
            return _clip(f"{label} added: {_first_line((after or '').encode('utf-8'))}")
        if after is None:
            return f"{label} removed"
        return _clip(f"{label} changed: {_change_excerpt(before.encode(), after.encode())}")


def _section_label(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    if name == "pyproject.toml":
        return "[tool.pytest] section"
    if name == "setup.cfg":
        return "[tool:pytest] section"
    return "test script"


def _test_section(path: str, data: bytes) -> str | None:
    """The test-relevant part of a mixed configuration file, or None if it has none."""
    name = path.rsplit("/", 1)[-1]
    text = data.decode("utf-8", errors="replace")
    if name == "pyproject.toml":
        return _sections(text, lambda h: h == "tool.pytest" or h.startswith("tool.pytest."))
    if name == "setup.cfg":
        return _sections(text, lambda h: h == "tool:pytest")
    return _package_tests(text)


def _sections(text: str, wanted: Callable[[str], bool]) -> str | None:
    collected: list[str] = []
    inside = False
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("[") and "]" in stripped:
            header = stripped.lstrip("[").split("]", 1)[0].strip().strip("\"'")
            inside = wanted(header)
        if inside:
            collected.append(line)
    return "\n".join(collected) if collected else None


def _package_tests(text: str) -> str | None:
    try:
        package = json.loads(text)
    except ValueError:
        return text
    if not isinstance(package, dict):
        return text
    part: dict[str, Any] = {}
    scripts = package.get("scripts")
    if isinstance(scripts, dict):
        for key, value in scripts.items():
            if key in ("test", "pretest", "posttest") or str(key).startswith("test:"):
                part[f"scripts.{key}"] = value
    if "jest" in package:
        part["jest"] = package["jest"]
    return json.dumps(part, sort_keys=True) if part else None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bytes(root: Path, rel: str) -> bytes | None:
    try:
        return root.joinpath(*rel.split("/")).read_bytes()
    except OSError:
        return None


def _clip(text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) <= _EXCERPT_LIMIT:
        return flat
    return flat[: _EXCERPT_LIMIT - 3] + "..."


def _first_line(data: bytes) -> str:
    if b"\0" in data[:8192]:
        return f"binary ({len(data)} bytes)"
    for line in data.decode("utf-8", errors="replace").splitlines():
        if line.strip():
            return line.strip()
    return "(empty)"


def _added_excerpt(data: bytes) -> str:
    return _clip(f"added ({len(data)} bytes): {_first_line(data)}")


def _change_excerpt(old: bytes, new: bytes) -> str:
    if b"\0" in old[:8192] or b"\0" in new[:8192]:
        return f"binary content changed ({len(old)} -> {len(new)} bytes)"
    a = old.decode("utf-8", errors="replace").splitlines()
    b = new.decode("utf-8", errors="replace").splitlines()
    removed: str | None = None
    added: str | None = None
    for index, line in enumerate(difflib.unified_diff(a, b, n=0, lineterm="")):
        if index < 2 or line.startswith("@@"):
            continue
        if line.startswith("-") and removed is None:
            removed = line[1:].strip()
        elif line.startswith("+") and added is None:
            added = line[1:].strip()
        if removed is not None and added is not None:
            break
    parts = [f"-{removed}"] if removed is not None else []
    if added is not None:
        parts.append(f"+{added}")
    if not parts:
        return "line endings or trailing bytes changed"
    return _clip(" | ".join(parts))


def _unlink_link(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        os.rmdir(path)


def _make_writable(path: Path) -> None:
    with contextlib.suppress(OSError):
        os.chmod(path, stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR | stat.S_IRUSR)


def _clear(path: Path) -> None:
    """Remove whatever is at `path` without following links out of the lease."""
    if path.is_symlink() or is_junction(path):
        _unlink_link(path)
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        _make_writable(path)
        path.unlink()


def _write_file(root: Path, rel: str, data: bytes) -> None:
    parts = rel.split("/")
    current = root
    for part in parts[:-1]:
        current = current / part
        if (
            current.is_symlink()
            or is_junction(current)
            or (current.exists() and not current.is_dir())
        ):
            _clear(current)
        current.mkdir(exist_ok=True)
    target = current / parts[-1]
    if target.is_symlink() or is_junction(target) or target.is_dir():
        _clear(target)
    elif target.exists():
        _make_writable(target)
    with open(target, "wb") as handle:
        handle.write(data)


def _remove_file(root: Path, rel: str) -> None:
    target = root.joinpath(*rel.split("/"))
    if os.path.lexists(target):
        _clear(target)


def _purge_bytecode(root: Path, rel: str) -> None:
    # A restored file can have the same size and mtime second as the agent's
    # version, which would let Python and pytest reuse the agent's cached bytecode.
    parts = rel.split("/")
    cache = root.joinpath(*parts[:-1], "__pycache__")
    if not cache.is_dir():
        return
    stem = parts[-1][: -len(".py")]
    for compiled in cache.glob(f"{glob.escape(stem)}.*.pyc"):
        with contextlib.suppress(OSError):
            compiled.unlink()
