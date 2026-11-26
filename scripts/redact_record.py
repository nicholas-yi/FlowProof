"""Publish a redacted copy of a sealed record without editing the original.

    python scripts/redact_record.py SOURCE_RUN --store research/runs \
        --replace-file PRIVATE_RULES.txt [--move-original PRIVATE_DIR]

The rules file holds one `old => new` replacement per line and stays outside the repository,
so the strings being removed never enter it. The new record copies every file of the
original with the replacements applied, records the original's id and manifest digest, and
is sealed. With `--move-original`, the verified original is moved to a private directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from flowproof.evidence import EvidenceStore, verify  # noqa: E402


def load_rules(path: Path) -> list[tuple[str, str]]:
    rules = []
    for line in path.read_text("utf-8").splitlines():
        if "=>" in line:
            old, new = (part.strip() for part in line.split("=>", 1))
            if old:
                rules.append((old, new))
                rules.append((json.dumps(old)[1:-1], json.dumps(new)[1:-1]))  # JSON-escaped
    return sorted(set(rules), key=lambda r: -len(r[0]))


def redact(text: str, rules: list[tuple[str, str]]) -> tuple[str, int]:
    count = 0
    for old, new in rules:
        if old in text:
            count += text.count(old)
            text = text.replace(old, new)
    return text, count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="redact_record")
    parser.add_argument("source", type=Path)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--replace-file", type=Path, required=True)
    parser.add_argument("--move-original", type=Path)
    args = parser.parse_args(argv)
    check = verify(args.source)
    if not check.ok:
        raise SystemExit(f"the original does not verify: {check.problems[:3]}")
    rules = load_rules(args.replace_file)
    manifest = (args.source / "manifest.json").read_bytes()
    plan = json.loads((args.source / "plan.json").read_text("utf-8"))
    record = EvidenceStore(args.store).create_run(f"{plan.get('experiment', 'run')}-redacted")
    replaced = 0
    for path in sorted(p for p in args.source.rglob("*") if p.is_file()):
        rel = path.relative_to(args.source).as_posix()
        if rel == "manifest.json":
            continue
        target = "original-events.jsonl" if rel == "events.jsonl" else rel
        text, n = redact(path.read_text("utf-8"), rules)
        replaced += n
        record.write_text(target, text)
    record.write_json("redaction.json", {
        "supersedes": {"id": args.source.name,
                       "manifest_sha256": hashlib.sha256(manifest).hexdigest()},
        "reason": "local paths and account names replaced with placeholders",
        "placeholders": sorted({new for _, new in rules}),
        "replacements": replaced,
        "note": "original-events.jsonl is the original event log with the same replacements; "
                "its hash chain refers to the unredacted lines and is kept for reference only.",
    })
    record.event("redacted-copy", supersedes=args.source.name, replacements=replaced)
    record.seal()
    print(f"redacted copy: {record.root} ({replaced} replacements)")
    if args.move_original:
        args.move_original.mkdir(parents=True, exist_ok=True)
        shutil.move(str(args.source), str(args.move_original / args.source.name))
        print(f"original moved to {args.move_original / args.source.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
