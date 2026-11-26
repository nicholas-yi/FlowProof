from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from flowproof.guards import GuardViolation, fingerprint
from flowproof.workspace import (
    CopyBackend,
    GuardedBackend,
    GuardedLease,
    InPlaceBackend,
    Tier,
    WorkspaceError,
)
from flowproof.workspace.gitenv import Git

CACHE_FILES = (
    ".git/config",
    ".venv/lib/site.py",
    "node_modules/pkg/index.js",
    "__pycache__/mod.cpython-312.pyc",
    "pkg/__pycache__/inner.cpython-312.pyc",
    ".pytest_cache/v/cache/lastfailed",
    "build/out.o",
    "dist/pkg.whl",
    ".flowproof/leases/x",
    "pkg/build/generated.txt",
)


def _tree(root: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


@pytest.fixture
def source(tmp_path: Path) -> Path:
    return _tree(
        tmp_path / "source",
        {
            "README.md": b"hello\n",
            "pkg/mod.py": b"VALUE = 1\n",
            "pkg/no_newline.txt": b"last line",
            "gone.txt": b"delete me\n",
        },
    )


def test_copy_excludes_vcs_data_build_output_and_caches(source: Path, tmp_path: Path) -> None:
    _tree(source, dict.fromkeys(CACHE_FILES, b"cache"))
    lease = CopyBackend(tmp_path / "leases").lease(source, label="copy")
    try:
        assert lease.tier is Tier.COPY
        for rel in CACHE_FILES:
            assert not (lease.root / rel).exists(), rel
        assert (lease.root / "pkg" / "mod.py").read_bytes() == b"VALUE = 1\n"
        assert lease.base == fingerprint(source).digest
        assert source not in lease.root.parents
        assert lease.diff().empty
    finally:
        lease.release()


def test_copy_diff_reports_text_binary_added_and_deleted_files(
    source: Path, tmp_path: Path
) -> None:
    lease = CopyBackend(tmp_path / "leases").lease(source, label="diff")
    try:
        (lease.root / "pkg" / "mod.py").write_bytes(b"VALUE = 2\n")
        (lease.root / "pkg" / "no_newline.txt").write_bytes(b"changed last line")
        (lease.root / "gone.txt").unlink()
        (lease.root / "new.txt").write_bytes(b"brand new\n")
        (lease.root / "blob.bin").write_bytes(b"\x00\x01\x02\xff")
        (lease.root / "pkg" / "__pycache__").mkdir()
        (lease.root / "pkg" / "__pycache__" / "mod.pyc").write_bytes(b"cache")

        change = lease.diff()
        assert change.files == (
            "blob.bin",
            "gone.txt",
            "new.txt",
            "pkg/mod.py",
            "pkg/no_newline.txt",
        )
        assert change.added == ("blob.bin", "new.txt")
        assert change.deleted == ("gone.txt",)
        assert "-VALUE = 1\n+VALUE = 2\n" in change.patch
        assert "Binary files /dev/null and b/blob.bin differ" in change.patch
        assert "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+brand new\n" in change.patch
        assert "deleted file mode 100644" in change.patch
        assert "\\ No newline at end of file" in change.patch
    finally:
        lease.release()


def test_copy_text_patch_applies_to_the_source(source: Path, tmp_path: Path) -> None:
    lease = CopyBackend(tmp_path / "leases").lease(source, label="apply")
    try:
        (lease.root / "pkg" / "mod.py").write_bytes(b"VALUE = 2\nEXTRA = 3\n")
        (lease.root / "pkg" / "no_newline.txt").write_bytes(b"now ends with newline\n")
        (lease.root / "gone.txt").unlink()
        (lease.root / "docs").mkdir()
        (lease.root / "docs" / "new.md").write_bytes(b"one\ntwo\n")
        patch = lease.diff().patch
    finally:
        lease.release()
    with Git() as git:
        check = git.run(["apply", "--check"], cwd=source, stdin=patch.encode(), check=False)
    assert check.exit_code == 0, check.stderr.decode()


def test_copy_release_is_idempotent_and_never_touches_the_source(
    source: Path, tmp_path: Path
) -> None:
    before = fingerprint(source)
    leases = tmp_path / "leases"
    lease = CopyBackend(leases).lease(source, label="release")
    (lease.root / "README.md").write_bytes(b"agent edit\n")
    (lease.root / "locked.txt").write_bytes(b"x")
    os.chmod(lease.root / "locked.txt", stat.S_IREAD)
    root = lease.root
    lease.release()
    lease.release()
    assert not root.exists()
    assert list(leases.iterdir()) == []
    assert fingerprint(source) == before
    with pytest.raises(WorkspaceError):
        lease.diff()


def test_copy_refuses_a_lease_root_inside_the_source(source: Path) -> None:
    with pytest.raises(WorkspaceError, match="outside the source tree"):
        CopyBackend(source / "leases").lease(source, label="x")
    assert not (source / "leases").exists()


def test_two_copy_leases_are_isolated(source: Path, tmp_path: Path) -> None:
    backend = CopyBackend(tmp_path / "leases")
    first = backend.lease(source, label="a")
    second = backend.lease(source, label="b")
    try:
        (first.root / "a.txt").write_bytes(b"a")
        assert second.diff().empty
        assert first.diff().added == ("a.txt",)
    finally:
        first.release()
        second.release()


def test_guarded_release_passes_when_nothing_changed(source: Path) -> None:
    before = fingerprint(source)
    mode = stat.S_IMODE((source / "README.md").stat().st_mode)
    lease = GuardedBackend().lease(source, label="reviewer")
    assert lease.tier is Tier.GUARDED
    assert lease.root == source.resolve()
    assert lease.base == before.digest
    result = lease.run([sys.executable, "-c", "import pkg.mod"])
    assert result.exit_code == 0, result.stderr
    assert lease.diff().empty
    lease.release()
    lease.release()
    assert fingerprint(source) == before
    assert stat.S_IMODE((source / "README.md").stat().st_mode) == mode


def test_guarded_files_are_read_only_during_the_lease(source: Path) -> None:
    if getattr(os, "geteuid", lambda: -1)() == 0:
        pytest.skip("root ignores file permissions")
    lease = GuardedBackend().lease(source, label="reviewer")
    try:
        with pytest.raises(PermissionError):
            (source / "README.md").write_bytes(b"overwrite")
    finally:
        lease.release()
    (source / "README.md").write_bytes(b"writable again\n")


def test_guarded_release_raises_when_a_file_was_added(source: Path) -> None:
    lease = GuardedBackend().lease(source, label="reviewer")
    assert isinstance(lease, GuardedLease)
    (source / "sneaky.txt").write_bytes(b"x")
    change = lease.diff()
    assert change.added == ("sneaky.txt",)
    assert "Binary files /dev/null and b/sneaky.txt differ" in change.patch
    with pytest.raises(GuardViolation) as raised:
        lease.release()
    assert raised.value.diff is not None
    assert raised.value.diff.added == ("sneaky.txt",)
    assert lease.violation is raised.value
    lease.release()
    assert lease.released


def test_guarded_release_raises_when_a_file_was_modified_or_deleted(source: Path) -> None:
    lease = GuardedBackend().lease(source, label="reviewer")
    target = source / "pkg" / "mod.py"
    os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
    target.write_bytes(b"VALUE = 99\n")
    os.chmod(source / "gone.txt", stat.S_IREAD | stat.S_IWRITE)
    (source / "gone.txt").unlink()
    with pytest.raises(GuardViolation) as raised:
        lease.release()
    assert raised.value.diff is not None
    assert raised.value.diff.modified == ("pkg/mod.py",)
    assert raised.value.diff.removed == ("gone.txt",)
    assert os.access(source / "README.md", os.W_OK)


def test_guarded_without_protection_still_detects_writes(source: Path) -> None:
    lease = GuardedBackend(protect=False).lease(source, label="reviewer")
    (source / "README.md").write_bytes(b"changed\n")
    with pytest.raises(GuardViolation):
        lease.release()


def test_inplace_without_git_diffs_against_a_private_snapshot(source: Path) -> None:
    lease = InPlaceBackend().lease(source, label="author")
    try:
        assert lease.tier is Tier.SHARED
        assert lease.root == source.resolve()
        (source / "README.md").write_bytes(b"edited in place\n")
        (source / "added.txt").write_bytes(b"new\n")
        change = lease.diff()
        assert change.files == ("README.md", "added.txt")
        assert "+edited in place" in change.patch
    finally:
        lease.release()
    assert (source / "added.txt").exists()
    assert (source / "README.md").read_bytes() == b"edited in place\n"


def test_inplace_in_git_attributes_only_changes_made_during_the_lease(tmp_path: Path) -> None:
    source = tmp_path / "repo"
    source.mkdir()
    with Git() as git:
        git.run(["init", "-q"], cwd=source)
        (source / "tracked.txt").write_bytes(b"v1\n")
        git.run(["add", "-A"], cwd=source)
        git.run(["commit", "-q", "-m", "initial"], cwd=source)
        (source / "tracked.txt").write_bytes(b"caller's uncommitted edit\n")
        (source / "caller-untracked.txt").write_bytes(b"mine\n")
        index_before = (source / ".git" / "index").read_bytes()

        lease = InPlaceBackend().lease(source, label="author")
        try:
            (source / "agent.txt").write_bytes(b"agent\n")
            (source / "tracked.txt").write_bytes(b"agent edit\n")
            change = lease.diff()
            assert change.files == ("agent.txt", "tracked.txt")
            assert change.added == ("agent.txt",)
            assert "-caller's uncommitted edit\n+agent edit\n" in change.patch
            assert git.run(["cat-file", "-t", lease.base], cwd=source).stdout.strip() == b"commit"
        finally:
            lease.release()
        assert (source / ".git" / "index").read_bytes() == index_before
        refs = git.run(["for-each-ref"], cwd=source).stdout.decode()
        assert "flowproof" not in refs
    assert (source / "agent.txt").exists()
