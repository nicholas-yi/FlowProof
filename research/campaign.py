"""Shared plumbing for research campaigns: sealed records, parallel jobs, provenance."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from flowproof import __version__
from flowproof.evidence import EvidenceStore

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "research" / "runs"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_head() -> str | None:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                            text=True, check=False)
    return result.stdout.strip() or None


def provenance(preregistration: Path | None) -> dict[str, Any]:
    return {
        "flowproof_version": __version__,
        "git_head": git_head(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "preregistration": None if preregistration is None else {
            "path": preregistration.relative_to(ROOT).as_posix(),
            "sha256": sha256_file(preregistration),
        },
    }


class Campaign:
    """One sealed evidence record per campaign; jobs write results as they finish."""

    def __init__(self, label: str, plan: Mapping[str, Any], *, root: Path = RUNS,
                 preregistration: Path | None = None):
        self.record = EvidenceStore(root).create_run(label)
        self.lock = threading.Lock()
        self.record.write_json("plan.json", {**plan, "provenance": provenance(preregistration)})
        self.record.event("campaign-start", label=label)

    @property
    def root(self) -> Path:
        return Path(self.record.root)

    def save(self, rel: str, value: Any) -> None:
        with self.lock:
            self.record.write_json(rel, value)

    def event(self, kind: str, **fields: Any) -> None:
        with self.lock:
            self.record.event(kind, **fields)

    def run(self, jobs: Iterable[tuple[str, Callable[[], Any]]], *, workers: int) -> list[Any]:
        results: list[Any] = []
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fn): name for name, fn in jobs}
            for done, future in enumerate(as_completed(futures), 1):
                name = futures[future]
                try:
                    results.append(future.result())
                    self.event("job-done", job=name)
                except Exception as exc:  # keep going: a broken job is evidence too
                    self.event("job-error", job=name, error=repr(exc))
                elapsed = time.monotonic() - started
                print(f"[{done}/{len(futures)}] {name} ({elapsed:.0f}s)", flush=True)
        return results

    def seal(self, summary: Mapping[str, Any]) -> Path:
        self.save("summary.json", summary)
        self.event("campaign-end")
        self.record.seal()
        return self.root


def load_jsons(directory: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))]
