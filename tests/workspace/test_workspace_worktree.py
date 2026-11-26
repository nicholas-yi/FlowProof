from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from flowproof.guards import fingerprint
from flowproof.workspace import Lease, Tier, WorkspaceError, WorktreeBackend
from flowproof.workspace import backends as backends_module
from flowproof.workspace.gitenv import Git


class Repo:
    """A real git repository for tests, driven through Flowproof's isolated git."""

    def __init__(self, path: Path, git: Git) -> None:
        self.path = path
        self.tool = git

    def git(self, *args: str, stdin: bytes | None = None) -> str:
        result = self.tool.run(list(args), cwd=self.path, stdin=stdin)
        return result.stdout.decode("utf-8", errors="replace")

    def write(self, rel: str, data: str | bytes) -> Path:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
        return target

    def commit_all(self, message: str = "commit") -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def state(self) -> dict[str, object]:
        """Everything a lease must leave unchanged in the source repository."""
        admin = self.path / ".git" / "worktrees"
        return {
            "status": self.git("status", "--porcelain=v1", "-uall", "--ignored"),
            "refs": self.git("for-each-ref", "--format=%(refname) %(objectname)"),
            "worktrees": self.git("worktree", "list", "--porcelain"),
            "admin": sorted(p.name for p in admin.iterdir()) if admin.is_dir() else [],
            "index": (self.path / ".git" / "index").read_bytes(),
            "head": self.git("rev-parse", "HEAD"),
            "files": dict(fingerprint(self.path, exclude=("**/.git", "**/.git/**")).files),
        }


def make_repo(path: Path, git: Git) -> Repo:
    path.mkdir(parents=True)
    repo = Repo(path, git)
    repo.git("init", "-q")
    repo.write("README.md", "hello\n")
    repo.write("src/app.py", "print('hi')\n")
    repo.write(".gitignore", "*.log\n")
    repo.commit_all("initial")
    return repo


def git_version(git: Git) -> tuple[int, int]:
    text = git.run(["version"], cwd=Path.cwd()).stdout.decode()
    match = re.search(r"(\d+)\.(\d+)", text)
    assert match is not None
    return int(match.group(1)), int(match.group(2))


@pytest.fixture
def git_tool() -> Iterator[Git]:
    tool = Git()
    yield tool
    tool.close()


@pytest.fixture
def repo(tmp_path: Path, git_tool: Git) -> Repo:
    return make_repo(tmp_path / "repo", git_tool)


@pytest.fixture
def leases(tmp_path: Path) -> Path:
    return tmp_path / "leases"


def test_base_includes_uncommitted_and_untracked_files(repo: Repo, leases: Path) -> None:
    repo.write("README.md", "changed but not committed\n")
    repo.write("notes/untracked.txt", "untracked\n")
    repo.write("debug.log", "ignored\n")
    (repo.path / "src" / "app.py").unlink()

    lease = WorktreeBackend(leases).lease(repo.path, label="author")
    try:
        assert lease.tier is Tier.WORKTREE
        assert (lease.root / "README.md").read_bytes() == b"changed but not committed\n"
        assert (lease.root / "notes" / "untracked.txt").read_bytes() == b"untracked\n"
        assert not (lease.root / "debug.log").exists()
        assert not (lease.root / "src" / "app.py").exists()
        assert repo.git("cat-file", "-t", lease.base).strip() == "commit"
        assert repo.git("show", f"{lease.base}:notes/untracked.txt") == "untracked\n"
        assert (
            repo.git("rev-parse", f"{lease.base}^").strip() == repo.git("rev-parse", "HEAD").strip()
        )
        assert lease.diff().empty
        assert repo.path not in lease.root.parents
    finally:
        lease.release()


def test_base_keeps_exact_bytes_despite_gitattributes(
    repo: Repo, leases: Path, git_tool: Git
) -> None:
    if git_version(git_tool) < (2, 40):
        pytest.skip("GIT_ATTR_SOURCE needs git 2.40 or later")
    repo.write(".gitattributes", "* text=auto eol=lf\n")
    repo.commit_all("attributes")
    repo.write("crlf.txt", b"one\r\ntwo\r\n")
    lease = WorktreeBackend(leases).lease(repo.path, label="bytes")
    try:
        assert (lease.root / "crlf.txt").read_bytes() == b"one\r\ntwo\r\n"
        assert lease.diff().empty
    finally:
        lease.release()


def test_diff_includes_new_deleted_and_binary_files(repo: Repo, leases: Path) -> None:
    lease = WorktreeBackend(leases).lease(repo.path, label="diff")
    try:
        (lease.root / "README.md").write_bytes(b"edited\n")
        (lease.root / "src" / "app.py").unlink()
        (lease.root / "data.bin").write_bytes(bytes(range(256)))
        (lease.root / "docs").mkdir()
        (lease.root / "docs" / "new.md").write_bytes(b"new\n")
        (lease.root / "scratch.log").write_bytes(b"ignored by .gitignore\n")

        change = lease.diff()
        assert change.files == ("README.md", "data.bin", "docs/new.md", "src/app.py")
        assert change.added == ("data.bin", "docs/new.md")
        assert change.deleted == ("src/app.py",)
        assert change.modified == ("README.md",)
        assert "GIT binary patch" in change.patch
        assert "new file mode 100644" in change.patch
        assert "deleted file mode 100644" in change.patch
        assert "+edited" in change.patch
        # The source still holds the base, so the patch must apply to it cleanly.
        repo.git("apply", "--check", stdin=change.patch.encode("utf-8"))
    finally:
        lease.release()


def test_release_is_idempotent_and_leaves_the_source_untouched(repo: Repo, leases: Path) -> None:
    repo.write("README.md", "dirty\n")
    repo.write("scratch.txt", "untracked\n")
    before = repo.state()

    lease = WorktreeBackend(leases).lease(repo.path, label="clean")
    (lease.root / "README.md").write_bytes(b"agent\n")
    (lease.root / "agent.txt").write_bytes(b"x")
    result = lease.run([sys.executable, "-c", "open('made.txt', 'w').write('y')"])
    assert result.exit_code == 0
    assert (lease.root / "made.txt").exists()
    root = lease.root

    lease.release()
    lease.release()

    assert lease.released
    assert not root.exists()
    assert list(leases.iterdir()) == []
    assert repo.state() == before
    assert "refs/flowproof" not in repo.git("for-each-ref")
    with pytest.raises(WorkspaceError):
        lease.diff()
    with pytest.raises(WorkspaceError):
        lease.run([sys.executable, "-c", "pass"])


def test_two_concurrent_leases_are_isolated(repo: Repo, leases: Path) -> None:
    before = repo.state()
    backend = WorktreeBackend(leases)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(lambda label: backend.lease(repo.path, label=label), ["a", "b"])
    try:
        assert first.root != second.root
        (first.root / "README.md").write_bytes(b"from a\n")
        (first.root / "a.txt").write_bytes(b"a")
        (second.root / "b.txt").write_bytes(b"b")

        assert not (second.root / "a.txt").exists()
        assert (second.root / "README.md").read_bytes() == b"hello\n"
        assert first.diff().files == ("README.md", "a.txt")
        assert second.diff().files == ("b.txt",)
        assert second.diff().added == ("b.txt",)
    finally:
        first.release()
        second.release()
    assert repo.state() == before


def test_a_lease_from_a_subdirectory_is_rooted_there(repo: Repo, leases: Path) -> None:
    lease = WorktreeBackend(leases).lease(repo.path / "src", label="sub")
    try:
        assert lease.root.name == "src"
        assert (lease.root / "app.py").exists()
        (lease.root / "new.py").write_bytes(b"x = 1\n")
        (lease.root.parent / "README.md").write_bytes(b"outside the subdirectory\n")
        change = lease.diff()
        assert change.files == ("new.py",)
        assert change.added == ("new.py",)
    finally:
        lease.release()


def test_a_repository_without_commits_can_be_leased(tmp_path: Path, git_tool: Git) -> None:
    source = tmp_path / "fresh"
    source.mkdir()
    git_tool.run(["init", "-q"], cwd=source)
    (source / "only.txt").write_bytes(b"first\n")
    lease = WorktreeBackend(tmp_path / "leases").lease(source, label="fresh")
    try:
        assert (lease.root / "only.txt").read_bytes() == b"first\n"
        parents = git_tool.run(["rev-list", "--parents", "-n", "1", lease.base], cwd=source)
        assert parents.stdout.decode().split() == [lease.base]
    finally:
        lease.release()
    status = git_tool.run(["status", "--porcelain"], cwd=source).stdout.decode()
    assert status == "?? only.txt\n"


def test_refuses_a_directory_outside_git(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorkspaceError, match="not inside a git work tree"):
        WorktreeBackend(tmp_path / "leases").lease(plain, label="x")


def test_refuses_a_lease_root_inside_the_source(repo: Repo) -> None:
    before = repo.state()
    with pytest.raises(WorkspaceError, match="outside the source tree"):
        WorktreeBackend(repo.path / "leases").lease(repo.path, label="x")
    assert not (repo.path / "leases").exists()
    assert repo.state() == before


def test_default_lease_root_is_the_temporary_directory(repo: Repo) -> None:
    lease = WorktreeBackend().lease(repo.path, label="default")
    try:
        assert Path(tempfile.gettempdir()).resolve() in lease.root.parents
    finally:
        lease.release()
    assert not lease.root.exists()


def test_release_removes_read_only_files(repo: Repo, leases: Path) -> None:
    lease = WorktreeBackend(leases).lease(repo.path, label="ro")
    locked = lease.root / "readonly.txt"
    locked.write_bytes(b"x")
    os.chmod(locked, stat.S_IREAD)
    os.chmod(lease.root / "README.md", stat.S_IREAD)
    lease.release()
    assert not lease.root.exists()


def test_release_waits_for_a_file_lock_to_clear(repo: Repo, leases: Path) -> None:
    lease = WorktreeBackend(leases).lease(repo.path, label="locked")
    handle = open(lease.root / "held-open.txt", "wb")  # noqa: SIM115 - closed by the timer
    timer = threading.Timer(0.5, handle.close)
    timer.start()
    try:
        lease.release()
    finally:
        timer.join()
        handle.close()
    assert not lease.root.exists()
    assert "refs/flowproof" not in repo.git("for-each-ref")


def test_a_failed_release_can_be_retried(
    repo: Repo, leases: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = repo.state()
    lease = WorktreeBackend(leases).lease(repo.path, label="retry")
    assert isinstance(lease, backends_module.WorktreeLease)
    real_remove = backends_module._remove_tree

    def refuse(path: Path) -> None:
        raise WorkspaceError(f"simulated lock on {path}")

    monkeypatch.setattr(backends_module, "_remove_tree", refuse)
    with pytest.raises(WorkspaceError, match="simulated lock"):
        lease.release()
    assert not lease.released
    assert lease.ref in repo.git("for-each-ref")
    monkeypatch.undo()
    assert backends_module._remove_tree is real_remove

    lease.release()
    assert lease.released
    assert repo.state() == before


def test_remove_tree_retries_transient_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "doomed"
    (target / "inner").mkdir(parents=True)
    (target / "inner" / "file.txt").write_bytes(b"x")
    real_rmtree = backends_module._rmtree
    calls: list[Path] = []

    def flaky(path: Path) -> None:
        calls.append(path)
        if len(calls) < 3:
            raise PermissionError(32, "The process cannot access the file")
        real_rmtree(path)

    monkeypatch.setattr(backends_module, "_rmtree", flaky)
    monkeypatch.setattr(backends_module.time, "sleep", lambda _s: None)
    backends_module._remove_tree(target)
    assert len(calls) == 3
    assert not target.exists()


def test_remove_tree_gives_up_with_a_workspace_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "stuck"
    target.mkdir()

    def always(_path: Path) -> None:
        raise PermissionError(32, "locked")

    monkeypatch.setattr(backends_module, "_rmtree", always)
    monkeypatch.setattr(backends_module.time, "sleep", lambda _s: None)
    with pytest.raises(WorkspaceError, match="could not remove"):
        backends_module._remove_tree(target)
    assert target.exists()


def test_inherited_git_environment_cannot_redirect_the_lease(
    tmp_path: Path, git_tool: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    decoy = make_repo(tmp_path / "decoy", git_tool)
    target = make_repo(tmp_path / "target", git_tool)
    target.write("target-only.txt", "t\n")
    monkeypatch.setenv("GIT_DIR", str(decoy.path / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy.path))
    lease: Lease = WorktreeBackend(tmp_path / "leases").lease(target.path, label="env")
    try:
        assert (lease.root / "target-only.txt").exists()
    finally:
        lease.release()
