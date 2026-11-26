from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path

import pytest

from flowproof.evidence import (
    EVENTS,
    MANIFEST,
    EvidenceError,
    EvidenceStore,
    RunRecord,
    canonical_json,
    supersede,
    verify,
)
from flowproof.evidence import store as store_module


def _writable(path: Path) -> Path:
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    return path


def _sealed_run(tmp_path: Path) -> tuple[EvidenceStore, RunRecord, str]:
    store = EvidenceStore(tmp_path / "runs")
    run = store.create_run("demo")
    run.write_text("notes/readme.txt", "hello\n")
    run.write_bytes("blob.bin", b"\x00\x01\xff")
    run.write_json("result.json", {"b": 2, "a": [1, "é"]})
    run.event("stage", name="author", ok=True)
    run.event("stage", name="review", ok=False)
    digest = run.seal()
    return store, run, digest


def test_runs_get_unique_timestamped_ids(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "runs")
    first = store.create_run("a")
    second = store.create_run("b")
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", first.id)
    assert first.id != second.id
    assert first.root == tmp_path / "runs" / first.id
    assert first.root.is_dir()


def test_create_run_fails_if_the_directory_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EvidenceStore(tmp_path / "runs")
    monkeypatch.setattr(store_module, "_new_run_id", lambda: "20261125T000000Z-deadbeef")
    store.create_run("first")
    with pytest.raises(EvidenceError, match="already exists"):
        store.create_run("second")


def test_writes_return_sha256_and_json_is_canonical(tmp_path: Path) -> None:
    run = EvidenceStore(tmp_path).create_run("json")
    digest = run.write_json("out/value.json", {"z": 1, "a": {"y": None, "b": "é"}})
    data = (run.root / "out" / "value.json").read_bytes()
    assert digest == hashlib.sha256(data).hexdigest()
    assert data == '{\n  "a": {\n    "b": "é",\n    "y": null\n  },\n  "z": 1\n}\n'.encode()
    assert b"\r\n" not in data
    assert run.write_text("t.txt", "line\n") == hashlib.sha256(b"line\n").hexdigest()


def test_events_are_canonical_and_hash_chained(tmp_path: Path) -> None:
    run = EvidenceStore(tmp_path).create_run("chain")
    head = run.event("call", role="author", credits=1.5)
    lines = (run.root / EVENTS).read_bytes().split(b"\n")
    assert lines[-1] == b""
    first, second = (json.loads(line) for line in lines[:-1])
    assert first["seq"] == 1 and first["kind"] == "run-created" and first["prev"] == "0" * 64
    assert first["label"] == "chain" and first["id"] == run.id
    assert second["seq"] == 2
    assert second["prev"] == hashlib.sha256(lines[0]).hexdigest()
    assert head == hashlib.sha256(lines[1]).hexdigest() == run.head
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", second["at"])
    assert lines[1] == canonical_json(second).encode()
    assert b": " not in lines[1] and b", " not in lines[1]


def test_seal_writes_a_manifest_and_makes_files_read_only(tmp_path: Path) -> None:
    _, run, digest = _sealed_run(tmp_path)
    raw = (run.root / MANIFEST).read_bytes()
    assert digest == hashlib.sha256(raw).hexdigest()
    manifest = json.loads(raw)
    assert (
        raw == (json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()
    )
    assert manifest["format"] == "flowproof-evidence"
    assert manifest["version"] == 1
    assert manifest["id"] == run.id
    assert manifest["chain"] == {"head": run.head, "length": 3}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", manifest["sealed_at"])
    paths = [entry["path"] for entry in manifest["files"]]
    assert paths == ["blob.bin", "events.jsonl", "notes/readme.txt", "result.json"]
    blob = next(e for e in manifest["files"] if e["path"] == "blob.bin")
    assert blob == {
        "path": "blob.bin",
        "bytes": 3,
        "sha256": hashlib.sha256(b"\x00\x01\xff").hexdigest(),
    }
    assert run.sealed
    for rel in [*paths, MANIFEST]:
        assert not os.stat(run.root / rel).st_mode & stat.S_IWUSR, rel


def test_a_sealed_run_verifies(tmp_path: Path) -> None:
    _, run, digest = _sealed_run(tmp_path)
    report = verify(run.root)
    assert report.ok and report.problems == ()
    assert verify(run.root, expected_manifest_sha256=digest).ok
    pinned = verify(run.root, expected_manifest_sha256="0" * 64)
    assert not pinned.ok and "expected sha256" in pinned.problems[0]


def test_a_copied_or_moved_run_still_verifies(tmp_path: Path) -> None:
    _, run, digest = _sealed_run(tmp_path)
    copied = Path(shutil.copytree(run.root, tmp_path / "elsewhere" / "deep" / "renamed"))
    assert verify(copied, expected_manifest_sha256=digest).ok
    moved = run.root.rename(tmp_path / "moved")
    assert verify(moved).ok


def test_a_changed_byte_is_detected(tmp_path: Path) -> None:
    _, run, _ = _sealed_run(tmp_path)
    target = _writable(run.root / "blob.bin")
    target.write_bytes(b"\x00\x01\xfe")
    report = verify(run.root)
    assert not report.ok
    assert report.problems == ("content mismatch: blob.bin does not match its sha256",)


def test_a_changed_length_is_detected(tmp_path: Path) -> None:
    _, run, _ = _sealed_run(tmp_path)
    _writable(run.root / "notes" / "readme.txt").write_bytes(b"hello\r\n")
    problems = verify(run.root).problems
    assert any(p.startswith("size mismatch: notes/readme.txt") for p in problems)


def test_an_added_file_is_detected(tmp_path: Path) -> None:
    _, run, _ = _sealed_run(tmp_path)
    (run.root / "notes" / "extra.txt").write_bytes(b"sneaky")
    assert verify(run.root).problems == ("unlisted file: notes/extra.txt",)


def test_a_deleted_file_is_detected(tmp_path: Path) -> None:
    _, run, _ = _sealed_run(tmp_path)
    _writable(run.root / "result.json").unlink()
    assert verify(run.root).problems == ("missing file: result.json",)


def test_a_broken_chain_is_detected_even_with_a_consistent_manifest(tmp_path: Path) -> None:
    _, run, _ = _sealed_run(tmp_path)
    events = _writable(run.root / EVENTS)
    lines = events.read_bytes().split(b"\n")[:-1]
    del lines[1]
    body = b"\n".join(lines) + b"\n"
    events.write_bytes(body)
    manifest_path = _writable(run.root / MANIFEST)
    manifest = json.loads(manifest_path.read_bytes())
    for entry in manifest["files"]:
        if entry["path"] == EVENTS:
            entry["bytes"] = len(body)
            entry["sha256"] = hashlib.sha256(body).hexdigest()
    manifest["chain"]["head"] = hashlib.sha256(lines[-1]).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    problems = verify(run.root).problems
    assert any("seq 3, expected 2" in p for p in problems)
    assert any("does not link to the previous line" in p for p in problems)
    assert any("2 lines, manifest says 3" in p for p in problems)


def test_a_rewritten_event_is_detected(tmp_path: Path) -> None:
    _, run, _ = _sealed_run(tmp_path)
    events = _writable(run.root / EVENTS)
    events.write_bytes(events.read_bytes().replace(b'"ok":false', b'"ok":true '))
    problems = verify(run.root).problems
    assert any("content mismatch: events.jsonl" in p for p in problems)
    assert any("not canonical JSON" in p for p in problems)


def test_verify_reports_unsealed_and_missing_runs(tmp_path: Path) -> None:
    run = EvidenceStore(tmp_path).create_run("open")
    assert "never sealed" in verify(run.root).problems[0]
    assert "not found" in verify(tmp_path / "nope").problems[0]
    (tmp_path / "corrupt").mkdir()
    (tmp_path / "corrupt" / MANIFEST).write_bytes(b"{not json")
    assert "not valid JSON" in verify(tmp_path / "corrupt").problems[0]


def test_verify_rejects_unsafe_manifest_paths(tmp_path: Path) -> None:
    _, run, _ = _sealed_run(tmp_path)
    manifest_path = _writable(run.root / MANIFEST)
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"].append({"path": "../outside.txt", "bytes": 0, "sha256": "0" * 64})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert "unsafe path: '../outside.txt'" in " ".join(verify(run.root).problems)


def test_files_are_write_once(tmp_path: Path) -> None:
    run = EvidenceStore(tmp_path).create_run("once")
    run.write_text("a.txt", "first")
    with pytest.raises(EvidenceError, match="overwrite"):
        run.write_text("a.txt", "second")
    with pytest.raises(EvidenceError, match="overwrite"):
        run.write_text("A.TXT", "case variant collides on Windows and macOS")
    with pytest.raises(EvidenceError, match="already an evidence file"):
        run.write_text("a.txt/child", "under a file")
    assert (run.root / "a.txt").read_text() == "first"


@pytest.mark.parametrize(
    ("rel", "message"),
    [
        ("/etc/passwd", "absolute"),
        ("C:/Windows/x", "absolute"),
        ("C:x", "absolute"),
        ("..\\x", "backslashes"),
        ("dir\\x.txt", "backslashes"),
        ("../escape.txt", "'..'"),
        ("a/../../b", "'..'"),
        ("a//b", "empty"),
        ("./a", "empty or '.'"),
        ("", "non-empty"),
        ("notes:stream", "not portable"),
        ("trailing.", "space or dot"),
        ("nul.txt", "reserved device"),
        (MANIFEST, "written by the record"),
        (EVENTS, "written by the record"),
    ],
)
def test_unsafe_paths_are_refused(tmp_path: Path, rel: str, message: str) -> None:
    run = EvidenceStore(tmp_path / "runs").create_run("unsafe")
    with pytest.raises(EvidenceError, match=re.escape(message)):
        run.write_bytes(rel, b"x")
    assert not (tmp_path / "escape.txt").exists()


def test_nothing_can_be_written_after_seal(tmp_path: Path) -> None:
    _, run, _ = _sealed_run(tmp_path)
    with pytest.raises(EvidenceError, match="sealed"):
        run.write_text("late.txt", "x")
    with pytest.raises(EvidenceError, match="sealed"):
        run.event("late")
    with pytest.raises(EvidenceError, match="sealed"):
        run.seal()
    assert verify(run.root).ok


def test_events_cannot_override_chain_fields_or_hold_non_json(tmp_path: Path) -> None:
    run = EvidenceStore(tmp_path).create_run("fields")
    with pytest.raises(EvidenceError, match="override"):
        run.event("x", seq=99)
    with pytest.raises(EvidenceError, match="JSON"):
        run.event("x", value=float("nan"))
    with pytest.raises(EvidenceError, match="JSON"):
        run.write_json("x.json", {"path": Path("x")})
    run.seal()
    assert verify(run.root).ok


def test_seal_refuses_foreign_or_altered_files(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    foreign = store.create_run("foreign")
    (foreign.root / "dropped.txt").write_bytes(b"not written by the record")
    with pytest.raises(EvidenceError, match="unexpected"):
        foreign.seal()
    altered = store.create_run("altered")
    altered.write_text("a.txt", "original")
    (altered.root / "a.txt").write_text("changed")
    with pytest.raises(EvidenceError, match="changed after it was written"):
        altered.seal()
    assert not altered.sealed


def test_supersede_links_runs_without_touching_the_old_one(tmp_path: Path) -> None:
    store, old, old_digest = _sealed_run(tmp_path)
    new = store.create_run("rerun")
    new.write_text("note.txt", "fixed\n")
    new_digest = new.seal()
    old_manifest = (old.root / MANIFEST).read_bytes()

    link_id = supersede(store, old.id, new.id, "the first run used a stale base")

    assert (old.root / MANIFEST).read_bytes() == old_manifest
    assert verify(old.root, expected_manifest_sha256=old_digest).ok
    link_root = store.path(link_id)
    assert verify(link_root).ok
    record = json.loads((link_root / "supersede.json").read_bytes())
    assert record == {
        "kind": "supersede",
        "old": old.id,
        "new": new.id,
        "reason": "the first run used a stale base",
        "old_manifest_sha256": old_digest,
        "new_manifest_sha256": new_digest,
    }
    events = [json.loads(line) for line in (link_root / EVENTS).read_bytes().splitlines()]
    assert events[-1]["kind"] == "superseded" and events[-1]["new"] == new.id


def test_supersede_rejects_bad_requests(tmp_path: Path) -> None:
    store, old, _ = _sealed_run(tmp_path)
    with pytest.raises(EvidenceError, match="itself"):
        supersede(store, old.id, old.id, "x")
    with pytest.raises(EvidenceError, match="reason"):
        supersede(store, old.id, "20260101T000000Z-00000000", " ")
    with pytest.raises(EvidenceError, match="unknown run"):
        supersede(store, old.id, "20260101T000000Z-00000000", "missing")
    with pytest.raises(EvidenceError, match="not a run id"):
        supersede(store, old.id, "../escape", "path traversal")
