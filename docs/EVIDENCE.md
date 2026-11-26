# Workspaces, guards and evidence

`flowproof/workspace/`, `flowproof/guards/` and `flowproof/evidence/` implement the
"Workspace, guards and evidence" contract in [DESIGN.md](DESIGN.md): where an agent
works, what it started from, whether it tampered with the grader, and a sealed record
of what happened. They use only the standard library and `git`.

## Tiers

`backend_for(name, *, lease_root=None)` maps a binding name to a backend. Every backend
returns a `Lease` with `id`, `root` (where the agent works), `base`, `tier`, `run()`,
`diff()` and an idempotent `release()`.

| Name | Tier | What it does | What it does **not** isolate |
|---|---|---|---|
| `inplace` | `SHARED` | The agent edits the caller's directory. In a git work tree the base is a commit of the tree at lease time (objects written, no ref), so the caller's earlier uncommitted edits are not attributed to the agent. Outside git a private snapshot backs `diff()`. | Nothing. Edits land in the caller's tree by design. |
| `guarded` | `GUARDED` | The caller's directory is fingerprinted and its files lose write permission for the lease (`protect=True`). `release()` restores the modes and raises `GuardViolation` if any fingerprinted file was added, removed or modified. `diff()` names changed files only, because content is not kept. | Read-only bits are advisory: a process running as the same user (or root on POSIX) can restore them, and new files can still be created. Detection happens at `release()`, after the fact. Excluded paths (`DEFAULT_EXCLUDES`: `.git`, caches, `build`, `dist`, ...) are not checked. |
| `copy` | `COPY` | A private copy without `.git`, `.venv`, `node_modules`, `__pycache__`, `.pytest_cache`, `build`, `dist`, `.flowproof` (and `.mypy_cache`, `.ruff_cache`, `.hypothesis`, `.tox`, `.nox`). A second private copy holds the starting content so `diff()` is a full unified diff. The base is the starting fingerprint digest. | Symlinks are copied as links, so an absolute link still points outside. No git history is available to the agent. |
| `worktree` | `WORKTREE` | A detached `git worktree` at a pinned commit (below). `diff()` is git's own `--binary` diff against that commit. | The object store and refs are shared with the source repository: an agent that creates a branch or tag there makes it visible to the caller. Submodule contents are not checked out. Ignored files are not in the lease. |

None of the tiers isolates the network, environment variables, other processes or the
rest of the file system: they are workspaces, not sandboxes.

`copy` and `worktree` leases are created under `lease_root` (default: the system
temporary directory) and refuse a root inside the source tree. `release()` deletes only
paths the lease created, retrying for a few seconds while Windows holds a file lock; if
the lock persists it raises `WorkspaceError` and a later `release()` finishes the job.

`Lease.run(command, timeout_s=600, env=None)` runs a sequence directly or a string
through the platform shell (`cmd.exe` on Windows, `/bin/sh` elsewhere), in the lease
root, with `env` layered over the current environment. Output is decoded as UTF-8
with replacement. It never raises for a failing command: `exit_code` is `None` when the
command could not start (the reason is in `stderr`) or timed out (the whole process tree
is killed and `timed_out` is true).

## Pinning a base

Git always runs with an isolated configuration: `GIT_CONFIG_GLOBAL` points at an empty
file in a private temporary directory, `GIT_CONFIG_NOSYSTEM=1`,
`GIT_TERMINAL_PROMPT=0`, inherited `GIT_*` variables are dropped, and every command gets
`-c core.autocrlf=false -c core.safecrlf=false -c user.name=flowproof
-c user.email=flowproof@localhost -c commit.gpgsign=false` plus overrides that disable
hooks, fsmonitor and network protocols. `GIT_ATTR_SOURCE` points at the empty tree, so
`.gitattributes` eol, text and filter rules cannot rewrite bytes (git 2.40+; older git
ignores the variable and applies the repository's attributes).

`worktree` pins the base from the source's **current work tree**:

1. Seed a scratch index (`GIT_INDEX_FILE`) from `HEAD` (or empty for a new repository).
2. `git add -A`, so uncommitted, deleted and untracked non-ignored files are included.
3. `git write-tree`, then `git commit-tree` with `HEAD` as parent.
4. Hold the commit with `refs/flowproof/<lease-id>` and `git worktree add --detach`.

The caller's index, `HEAD`, branches and status are never touched. `release()` removes
the worktree, its administrative entry and the private ref. `diff()` stages the lease in
another scratch index and runs `git diff --cached --binary --no-renames <base>`, so new,
deleted and binary files appear and the patch applies with `git apply`. A source
subdirectory is leased as that subdirectory of the worktree, with paths relative to it.

## Frozen tests and tamper findings

`FrozenTests.capture(base_root, patterns)` records the exact bytes of every file under
the glob patterns (for example `("tests/**",)`) **and** every test configuration file
present in the base: `conftest.py`, `pytest.ini`, `.pytest.ini`, `tox.ini`,
`jest.config.*` and `vitest.config.*` at any depth, plus `setup.cfg`, `pyproject.toml`
and `package.json`. Caches and VCS data are never frozen. Capture from the tree the
agent starts from, before it starts (for a worktree lease: `lease.root` right after
leasing). Globs use `/`; `*` stays within a segment, `**` spans segments, and a pattern
without `/` matches only at the top level.

`inspect(lease_root)` writes nothing and returns a `TamperReport`:

| Kind | Class | Raised when |
|---|---|---|
| `modified-test`, `deleted-test`, `added-test-file` | definitive | A frozen file under the patterns changed or vanished, or a file appeared under them. |
| `heuristic-skip-marker` | heuristic | A new `pytest.skip(`, `pytest.mark.skip`, `skipif(True`, `unittest.skip`, `skipIf(True`, `skipUnless(False`, `xfail`, `expectedFailure`, `skipTest(` or `SkipTest` line in any changed Python file, or `it.skip`/`.only`/`xit` in a JavaScript or TypeScript test file. |
| `heuristic-trivial-assert` | heuristic | A new `assert True`, `assert 1`, `assert x == x` for a literal, `assertTrue(True)` or similar in a test file. |
| `heuristic-early-exit` | heuristic | A new `sys.exit(0)`, `sys.exit()`, `os._exit(0)`, `raise SystemExit`, `pytest.exit(` or `process.exit(0)` in a test file. |
| `heuristic-config-override` | heuristic | A test configuration file outside the patterns was added, deleted or changed. For `pyproject.toml` only `[tool.pytest*]` tables count, for `setup.cfg` only `[tool:pytest]`, for `package.json` only `test`, `pretest`, `posttest`, `test:*` scripts and the `jest` key. |

Test files are those under the patterns plus `test_*.py`, `*_test.py`, `tests/`,
`test/`, `__tests__/`, `*.test.*`, `*.spec.*` and `conftest.py`. Heuristics compare each
changed file with the signals found in its base content, so markers that were already
there are not reported; a file that did not exist in the base has no such allowance.
Each finding carries a short excerpt such as `line 9: @pytest.mark.skip`.
`report.tampered` is true only for definitive findings; `report.suspicious` reports
heuristics. Heuristics are line patterns: they can miss a determined rewrite and can flag
an honest change, so treat them as leads for review, not verdicts.

`restore(lease_root)` returns the same report as `inspect`, then writes the frozen bytes
back over modified or deleted frozen files, deletes files added under the patterns, and
undoes every reported configuration change (including deleting a newly added test
configuration file). Configuration edits outside the test sections (a new dependency in
`pyproject.toml`, a `build` script) are neither reported nor reverted, so nothing is
overwritten silently. Restored Python files also lose their `__pycache__` entries, so a
same-size edit within the same second cannot leave the agent's bytecode in use.

## Grading: claimed versus true

`grade(lease, command, frozen, timeout_s=600)` runs `command` as the agent left the
tree (`claimed`), restores the frozen files, and runs it again (`true`).
`claimed_passed` and `true_passed` are `exit_code == 0`, or `None` when the command did
not run to completion. `false_pass` is `claimed_passed is True and true_passed is not
True`: the agent's tree passed but the grader's did not. `tests_passed` and
`tests_failed` come from the true run's pytest summary (`N passed`, `M failed`,
`K error(s)`; errors count as failures) or unittest summary (`Ran N tests`, `OK` or
`FAILED (failures=..., errors=...)`), and are `None` when neither is recognisable.
Restoring rewrites the lease, so take `lease.diff()` before grading if the agent's own
version of the tests matters.

## Sealed evidence

`EvidenceStore(root).create_run(label)` creates `root/<id>/` with
`id = YYYYMMDDTHHMMSSZ-<8 hex>` (UTC) and fails if the directory exists. The first
event, `run-created`, records the id and label.

- `write_bytes`, `write_text`, `write_json` create a new file and return its sha256.
  They refuse overwrites (including case-only variants, which collide on Windows and
  macOS), absolute paths, drive letters, `..`, `.` and empty segments, backslashes,
  non-portable characters and device names, and the reserved `manifest.json` and
  `events.jsonl`. Text is written as given, encoded as UTF-8, with no newline
  translation. JSON has sorted keys, indent 2 and a trailing `\n`.
- `event(kind, **fields)` appends one canonical JSON line to `events.jsonl` (sorted keys,
  `,` and `:` separators, UTF-8): `{"seq": n, "kind": ..., "at": "YYYY-MM-DDTHH:MM:SSZ",
  "prev": ..., ...fields}`. `seq` starts at 1; `prev` is the sha256 of the previous
  line's bytes without its newline, or 64 zeros for the first line. It returns the new
  line's sha256, the chain head.
- `seal()` refuses if the directory holds files the record did not write or if any
  written file or event line changed. It then writes `manifest.json`:
  `{"format": "flowproof-evidence", "version": 1, "id", "label", "files": [{"path",
  "bytes", "sha256"}, ...], "chain": {"head", "length"}, "sealed_at"}`, with every file
  except the manifest listed in path order. It clears write permission on every file and
  returns the manifest's sha256. Later writes raise `EvidenceError`.

`verify(run_root, *, expected_manifest_sha256=None)` re-reads everything and returns a
`VerifyReport(ok, problems)` instead of raising: missing, unlisted, resized or changed
files, unsafe manifest paths, non-canonical event lines, `seq` gaps, `prev` links that do
not match, and a chain head or length that differs from the manifest. Only relative
paths and content are checked, so a run verifies after it is copied, moved or cloned to
another directory or operating system, as long as its bytes are kept: the repository's
`.gitattributes` marks `research/runs/**` as `-text` for that reason. The manifest
vouches for the other files; pass the sha256 that `seal()` returned (or that a later
record captured) to pin the manifest itself.

`supersede(store, old_id, new_id, reason)` writes and seals a new small run whose
`supersede.json` names both ids, the reason and each run's manifest sha256, and returns
the new run's id. The old run is never modified.

## Platform notes

- On Windows a file held open by any process cannot be deleted, so lease removal retries
  with backoff for about 3.5 seconds before raising `WorkspaceError`.
- Read-only protection uses the read-only attribute on Windows and the write bits on
  POSIX. If a process dies during a `guarded` lease, files stay read-only; recover with
  `attrib -R /S /D <dir>\*` on Windows or `chmod -R u+w <dir>` elsewhere.
- Sealed evidence files are read-only afterwards; deleting a run directory needs the
  same commands.
- Timeouts kill the process tree with `taskkill /T` on Windows and a process-group
  `SIGKILL` elsewhere.
- File modes in copy and snapshot diffs are always `100644` on Windows.
