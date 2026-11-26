"""Independence audit: is this repository free of copied text and of terms it must not contain?

    python scripts/audit_independence.py --against DIR [--against DIR ...] [--json OUT]

1. Similarity: MOSS-style winnowing (Schleimer, Wilkerson and Aiken, 2003) over normalised
   token k-grams. For every file here it reports the share of its fingerprints found in the
   reference trees (containment) and the longest run of consecutive shared k-grams. Files
   above the thresholds are listed for manual review.
2. Terms: every word is hashed and compared with SHA-256 digests of terms that must not
   appear. Only the digests are stored, so the terms themselves are not in the repository.

Reference trees are only read. Exit status 1 if anything is flagged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
K, WINDOW = 12, 8
CONTAINMENT_LIMIT, RUN_LIMIT = 0.10, 6  # a run of 6 shared 12-grams is 17+ shared tokens
SOURCE_SUFFIXES = {".py", ".md", ".fp", ".flow", ".rs", ".js", ".ts", ".toml", ".txt", ".yml",
                   ".yaml", ".sh", ".ps1"}
SKIP_DIRS = {".git", ".venv", "node_modules", "target", "dist", "build", "__pycache__",
             ".pytest_cache", ".ruff_cache", ".hypothesis", "runs", "results", "figures"}
MAX_BYTES = 400_000
TOKEN = re.compile(r"[a-z0-9_]+")
WORD = re.compile(r"[a-z0-9][a-z0-9._@-]*[a-z0-9]|[a-z0-9]")
BANNED_SHA256 = {
    "085c36e6dc1aeb5f6de97e53a91c43ce54e9169c3af7486fb0e2e7b2d5db3306",
    "1ca5d01d614359b2db743956c675b3fbc3a45c7f5075da3b596747c1d2985681",
    "22937faf45d40d33276b876093da6267806547f2e6be7a443031f2d2d32e6c3c",
    "48dd969488eb543b329e6892aa011fc48318193df6eab304b60a3ef0f4d74f35",
    "55213f39076bfe8f5b385c6146bdbaeeaefc77e4e81825a9138e1fc1989d68b9",
    "6277a58afca46ac01c87533fb365c2f94484a9ee00a0929666d39310afa91a36",
    "683502cbcc1d1bd9a7a64334f0f1fe0391c1c2b682598daabcb36facf3c61413",
    "71fe64c2634e404c0900c7d9a0ae038d7834b24987d82ccf3c01ad7d8022bff4",
    "861b305198f5d9f4c1060ed8235d6f289285b77362061425a25f2588c8dc8200",
    "9fbf261b62c1d7c00db73afb81dd97fdf20b3442e36e338cb9359b856a03bdc8",
    "a56f3dbc3053cc78282ebb4360025187945043453f94099157496b76530a404d",
    "a60f312ec784bcff3ec76cc79ece0c1b12e143a2e1ad6ca4485f9f475c953524",
    "a87e5da1a1cc66559cc4ceeca6036bae5840e910ec75bc41d06d9cbad67c06b0",
    "ae55fae9aec258b9e74a3388bb750f82cb757a98f2c2f96bd65d8761b4767882",
    "b9352494463399aa6a44ed5e39425b8a0bc39b2fc3d0184ba2583ce9bc4e4c1d",
    "dacddd19044d0f0466ccab9aa58a18ac7c2810a1e59129ebfe6d9569bee7821d",
}


def term_digest(term: str) -> str:
    return hashlib.sha256(term.strip().lower().encode("utf-8")).hexdigest()


def files(root: Path, suffixes: set[str] = SOURCE_SUFFIXES) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        if path.is_file() and path.suffix.lower() in suffixes and path.stat().st_size <= MAX_BYTES:
            yield path


IMPORT_LINE = re.compile(r"^\s*(from\s+\S+\s+import\s|import\s|use\s|#!|//!)", re.M)


def tokens(text: str) -> list[str]:
    """Normalised tokens, without import lines: shared boilerplate is not shared content."""
    kept = "\n".join(line for line in text.splitlines() if not IMPORT_LINE.match(line))
    return TOKEN.findall(kept.lower())


def fingerprints(toks: list[str]) -> list[int]:
    """Winnowed k-gram hashes, in order (with the position of each kept)."""
    if len(toks) < K:
        return []
    grams = [int.from_bytes(hashlib.blake2b(" ".join(toks[i:i + K]).encode(), digest_size=8)
                            .digest(), "big") for i in range(len(toks) - K + 1)]
    kept: list[int] = []
    last = -1
    for start in range(max(1, len(grams) - WINDOW + 1)):
        window = grams[start:start + WINDOW]
        offset = min(range(len(window)), key=lambda i: (window[i], -i))
        if start + offset != last:
            kept.append(window[offset])
            last = start + offset
    return kept


def all_grams(toks: list[str]) -> list[int]:
    return [int.from_bytes(hashlib.blake2b(" ".join(toks[i:i + K]).encode(), digest_size=8)
                           .digest(), "big") for i in range(max(0, len(toks) - K + 1))]


def reference_index(trees: Iterable[Path]) -> set[int]:
    index: set[int] = set()
    for tree in trees:
        for path in files(tree):
            index.update(all_grams(tokens(path.read_text("utf-8", errors="replace"))))
    return index


def longest_run(grams: list[int], index: set[int]) -> int:
    best = run = 0
    for gram in grams:
        run = run + 1 if gram in index else 0
        best = max(best, run)
    return best


def scan_terms(paths: Iterable[Path], extra: Iterable[str] = ()) -> list[dict[str, object]]:
    digests = BANNED_SHA256 | {term_digest(t) for t in extra}
    hits = []
    for path in paths:
        for number, line in enumerate(path.read_text("utf-8", errors="replace").splitlines(), 1):
            for word in WORD.findall(line.lower()):
                parts = {word, *re.split(r"[._@-]", word)}
                if any(term_digest(p) in digests for p in parts if p):
                    hits.append({"path": path.relative_to(ROOT).as_posix(), "line": number})
                    break
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="audit_independence")
    parser.add_argument("--against", type=Path, action="append", default=[],
                        help="a reference tree to compare with (read only)")
    parser.add_argument("--banned-file", type=Path,
                        help="extra terms, one per line, kept outside the repository")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    ours = [p for p in files(ROOT) if "scripts" not in p.relative_to(ROOT).parts[:1]
            or p.name != Path(__file__).name]
    extra = []
    if args.banned_file:
        extra = [t for t in args.banned_file.read_text("utf-8").splitlines() if t.strip()]
    report: dict[str, object] = {"files": len(ours), "k": K, "window": WINDOW}
    records = list(files(ROOT / "research", {".json", ".jsonl", ".md", ".patch", ".fp"}))
    records += [p for p in (ROOT / "research" / "runs").rglob("*") if p.is_file()] \
        if (ROOT / "research" / "runs").exists() else []
    term_hits = scan_terms(sorted(set(ours) | set(records)), extra)
    report["term_hits"] = term_hits
    flagged = []
    if args.against:
        index = reference_index(args.against)
        report["reference_grams"] = len(index)
        rows = []
        for path in ours:
            toks = tokens(path.read_text("utf-8", errors="replace"))
            prints = fingerprints(toks)
            if not prints:
                continue
            containment = sum(p in index for p in prints) / len(prints)
            run = longest_run(all_grams(toks), index)
            row = {"path": path.relative_to(ROOT).as_posix(), "containment": round(containment, 4),
                   "longest_run": run}
            rows.append(row)
            if containment > CONTAINMENT_LIMIT or run >= RUN_LIMIT:
                flagged.append(row)
        rows.sort(key=lambda r: (-r["containment"], -r["longest_run"]))
        report["top"] = rows[:15]
        report["flagged"] = flagged
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "top"}, indent=2))
    for row in report.get("top", [])[:10]:  # type: ignore[union-attr]
        print(row)
    return 1 if term_hits or flagged else 0


if __name__ == "__main__":
    sys.exit(main())
