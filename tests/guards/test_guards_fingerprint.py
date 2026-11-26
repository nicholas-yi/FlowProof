from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from flowproof.guards import (
    DEFAULT_EXCLUDES,
    Fingerprint,
    GuardViolation,
    ReadOnlyGuard,
    compare,
    fingerprint,
    glob_match,
)


def _is_root() -> bool:
    return getattr(os, "geteuid", lambda: -1)() == 0


def _tree(root: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("tests/test_a.py", "tests/**", True),
        ("tests/unit/deep/test_b.py", "tests/**", True),
        ("tests", "tests/**", False),
        ("src/tests/test_a.py", "tests/**", False),
        ("conftest.py", "**/conftest.py", True),
        ("a/b/conftest.py", "**/conftest.py", True),
        ("a/b/xconftest.py", "**/conftest.py", False),
        ("setup.py", "*.py", True),
        ("pkg/setup.py", "*.py", False),
        ("pkg/setup.py", "**/*.py", True),
        ("a/x/y/b", "a/**/b", True),
        ("a/b", "a/**/b", True),
        ("test_1.py", "test_?.py", True),
        ("test_10.py", "test_?.py", False),
        ("v1.txt", "v[0-9].txt", True),
        ("va.txt", "v[!0-9].txt", True),
        ("v/.txt", "v[!0-9].txt", False),
        ("anything/at/all", "**", True),
        ("tests/x.py", "tests/", True),
        ("tests/x.py", "./tests/**", True),
        ("tests\\x.py", "tests/**", False),
    ],
)
def test_glob_matching(path: str, pattern: str, expected: bool) -> None:
    assert glob_match(path, pattern) is expected


def test_fingerprints_hash_exact_bytes_with_relative_sorted_paths(tmp_path: Path) -> None:
    root = _tree(tmp_path / "t", {"b.txt": b"line\r\n", "a/z.txt": b"z", "a/b/c.txt": b"c"})
    print_ = fingerprint(root)
    assert list(print_.files) == ["a/b/c.txt", "a/z.txt", "b.txt"]
    assert print_.files["b.txt"] == hashlib.sha256(b"line\r\n").hexdigest()
    other = _tree(tmp_path / "u", {"b.txt": b"line\n", "a/z.txt": b"z", "a/b/c.txt": b"c"})
    assert fingerprint(other).files["b.txt"] != print_.files["b.txt"]


def test_identical_trees_have_identical_fingerprints_wherever_they_live(tmp_path: Path) -> None:
    files = {"x/y.txt": b"1", "z.bin": b"\x00\xff"}
    first = fingerprint(_tree(tmp_path / "one", files))
    second = fingerprint(_tree(tmp_path / "elsewhere" / "two", dict(reversed(files.items()))))
    assert first == second
    assert first.digest == second.digest
    assert len(first.digest) == 64


def test_default_excludes_skip_vcs_data_and_caches(tmp_path: Path) -> None:
    root = _tree(
        tmp_path / "t",
        {
            "keep.py": b"k",
            ".git/HEAD": b"ref",
            "sub/.git": b"gitdir: elsewhere",
            "pkg/__pycache__/m.pyc": b"c",
            "node_modules/x/index.js": b"j",
            ".pytest_cache/v": b"v",
            ".hypothesis/examples/e": b"e",
            "build/lib/x": b"b",
            "pkg.egg-info/PKG-INFO": b"p",
            "legacy.pyc": b"stray bytecode is still seen",
        },
    )
    assert list(fingerprint(root).files) == ["keep.py", "legacy.pyc"]
    assert "**/.git/**" in DEFAULT_EXCLUDES


def test_include_and_exclude_globs(tmp_path: Path) -> None:
    root = _tree(
        tmp_path / "t",
        {"tests/test_a.py": b"a", "tests/data/big.bin": b"b", "src/x.py": b"x"},
    )
    only_tests = fingerprint(root, include=("tests/**",), exclude=("tests/data/**",))
    assert list(only_tests.files) == ["tests/test_a.py"]
    with pytest.raises(TypeError):
        fingerprint(root, include="tests/**")


def test_fingerprint_requires_a_directory(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        fingerprint(tmp_path / "missing")


def test_compare_reports_added_removed_and_modified() -> None:
    before = Fingerprint({"same": "1", "changed": "2", "gone": "3"})
    after = Fingerprint({"same": "1", "changed": "9", "new": "4"})
    diff = compare(before, after)
    assert diff.added == ("new",)
    assert diff.removed == ("gone",)
    assert diff.modified == ("changed",)
    assert diff.paths == ("changed", "gone", "new")
    assert not diff.clean
    assert compare(before, before).clean
    assert "added: new" in diff.summary()


def test_read_only_guard_passes_for_an_untouched_tree(tmp_path: Path) -> None:
    root = _tree(tmp_path / "t", {"a.txt": b"a"})
    with ReadOnlyGuard(root) as guard:
        assert guard.check().clean
        (root / "__pycache__").mkdir()
        (root / "__pycache__" / "x.pyc").write_bytes(b"cache is excluded")
    assert guard.diff is not None and guard.diff.clean
    assert os.access(root / "a.txt", os.W_OK)


def test_read_only_guard_raises_with_the_diff_and_restores_modes(tmp_path: Path) -> None:
    root = _tree(tmp_path / "t", {"a.txt": b"a", "b.txt": b"b"})
    original = stat.S_IMODE((root / "a.txt").stat().st_mode)
    with pytest.raises(GuardViolation) as raised, ReadOnlyGuard(root):
        assert not os.access(root / "a.txt", os.W_OK) or _is_root()
        os.chmod(root / "a.txt", original)
        (root / "a.txt").write_bytes(b"changed")
        (root / "c.txt").write_bytes(b"new")
    assert raised.value.diff is not None
    assert raised.value.diff.modified == ("a.txt",)
    assert raised.value.diff.added == ("c.txt",)
    assert raised.value.root == root
    assert stat.S_IMODE((root / "b.txt").stat().st_mode) == original


def test_read_only_guard_without_protection_leaves_permissions_alone(tmp_path: Path) -> None:
    root = _tree(tmp_path / "t", {"a.txt": b"a"})
    with ReadOnlyGuard(root, protect=False):
        assert os.access(root / "a.txt", os.W_OK)


def test_read_only_guard_cannot_be_entered_twice(tmp_path: Path) -> None:
    guard = ReadOnlyGuard(_tree(tmp_path / "t", {"a.txt": b"a"}))
    with guard, pytest.raises(RuntimeError):
        guard.__enter__()
