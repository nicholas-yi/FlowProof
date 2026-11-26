# Flowproof design

Flowproof is a small, typed language for orchestrating coding agents, a runtime
that executes it, and a set of evaluation tools that make agent experiments
hard to fool. This document is the contract between its components. Each
section names the module that owns it and the exact interfaces other modules
may rely on.

## Principles

1. **A workflow is a source file.** It is parsed, type-checked, graphed and
   bounded before any agent runs, so mistakes cost nothing.
2. **The language is bounded.** Every loop has a literal upper bound, there is
   no recursion and no general computation. The worst-case number of agent
   calls is computable statically.
3. **Roles are bound late.** A workflow names roles (`author`, `reviewer`).
   The command line binds each role to an agent product, a model and a
   workspace backend. Changing vendors is a binding change, not an edit.
4. **Outcomes are typed.** A wrong answer (`failed`) is never confused with a
   broken run (`invalid`), a budget stop (`limited`) or a cancellation.
5. **Unknown is not zero.** Usage is recorded in each provider's native unit.
   A missing measurement stays unknown, propagates through sums, and stops a
   capped run rather than letting it look cheap.
6. **Evidence is sealed.** Every run writes an append-only, hash-chained record
   that can be verified later, from any directory, on any operating system.
7. **Graders are out of the agent's reach.** Tests that decide success are
   frozen before the agent starts and restored right before grading. Any
   change the agent made to them is reported, not silently overwritten.

## Package layout

| Module | Owner | Purpose |
|---|---|---|
| `flowproof/outcomes.py` | core | `Status`, `Outcome` |
| `flowproof/accounting.py` | core | `Amount`, `Usage`, `Ledger`, errors |
| `flowproof/lang/` | language | lexer, parser, AST, printer, checker, graph |
| `flowproof/runtime/` | runtime | executor, values, bindings, report |
| `flowproof/harness/` | runtime | agent adapters: mock and Copilot CLI (a replay adapter is planned) |
| `flowproof/workspace/` | workspace | backends, leases, changes, commands |
| `flowproof/guards/` | workspace | fingerprints, read-only guard, frozen tests, grading |
| `flowproof/evidence/` | workspace | sealed run store and verifier |
| `flowproof/select/` | selection | selection policies, headroom report, simulation |
| `flowproof/testbed/` | testbed | rule-learning grid games and agent harness arms |

Runtime code has **no third-party dependencies**. Tests use `pytest` and
`hypothesis`. Target Python 3.11+. Use `from __future__ import annotations`,
dataclasses (frozen where practical), type hints everywhere, and no global
mutable state. All file paths in persisted data are relative and use `/`.
Text written to disk is UTF-8 with `\n` line endings.

## Core types (owned by core; already implemented)

```python
# flowproof/outcomes.py
class Status(StrEnum):
    OK = "ok"                # produced a value
    FAILED = "failed"        # the task was not achieved (a real negative)
    INVALID = "invalid"      # the run could not produce a trustworthy result
    LIMITED = "limited"      # a budget ceiling stopped further work
    CANCELLED = "cancelled"

@dataclass(frozen=True)
class Outcome:
    status: Status
    reason: str = ""             # short machine-readable code, e.g. "timeout"
    detail: Mapping[str, Any] = {}
```

```python
# flowproof/accounting.py
@dataclass(frozen=True)
class Amount:
    unit: str                    # "credits", "usd", "tokens", ...
    value: float | None          # None means unknown
    # a + b requires equal units; unknown + anything = unknown

@dataclass(frozen=True)
class Usage:
    amounts: Mapping[str, Amount]   # by unit
    calls: int = 0                  # counted by the caller, always known
    @staticmethod
    def unknown(*units: str, calls: int = 1) -> Usage
    @staticmethod
    def of(calls: int = 1, **values: float) -> Usage   # Usage.of(credits=2.5)
    def __add__(self, other: Usage) -> Usage            # union of units
    def get(self, unit: str) -> Amount                  # missing unit -> unknown

class LimitReached(Exception)       # a ceiling stops the next call
class AccountingUnknown(Exception)  # a capped unit's spend is unknown

class Ledger:
    def __init__(self, ceilings: Mapping[str, float] = {}, max_calls: int | None = None)
    def admit(self) -> None         # before a call; may raise either error
    def settle(self, usage: Usage) -> None   # after a call, always
    def remaining(self, unit: str) -> float | None
    spent: Usage                    # running total
```

`admit` is checked **between** calls: a call that starts below a ceiling may
end above it, and the next `admit` refuses. Callers that can pass a hard cap
to the provider should pass `remaining(unit)` so a single call cannot
overshoot far.

## Workspace, guards and evidence (owned by workspace)

### Backends and leases (`flowproof/workspace/`)

```python
class Tier(IntEnum):
    SHARED = 0      # agents work in the caller's directory
    GUARDED = 1     # in place, but read-only and fingerprint-checked
    COPY = 2        # a private copy of the source tree
    WORKTREE = 3    # a git worktree at a pinned commit

@dataclass(frozen=True)
class CommandResult:
    exit_code: int | None       # None if it never started or timed out
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool

@dataclass(frozen=True)
class Change:
    patch: str                  # unified diff, git --binary compatible
    files: tuple[str, ...]      # relative paths, sorted
    added: tuple[str, ...]
    deleted: tuple[str, ...]
    @property
    def empty(self) -> bool

class Lease:
    id: str
    root: Path                  # where the agent works
    base: str                   # commit hash or tree fingerprint it started from
    tier: Tier
    def run(self, command: str | Sequence[str], *, timeout_s: float = 600,
            env: Mapping[str, str] | None = None) -> CommandResult
    def diff(self) -> Change    # includes new and deleted files
    def release(self) -> None   # idempotent; never deletes the caller's tree

class Backend(Protocol):
    name: str
    tier: Tier
    def lease(self, source: Path, *, label: str) -> Lease
```

Implementations: `InPlaceBackend` (tier SHARED; `diff` uses git if the source
is a repository), `GuardedBackend` (tier GUARDED; `release` raises
`GuardViolation` if the tree changed), `CopyBackend` (tier COPY; copies the
tree excluding `.git`, build output and caches, diffs against the copy's
starting fingerprint), `WorktreeBackend` (tier WORKTREE; pins the base with a
temporary index so uncommitted and untracked files are included, then
`git worktree add --detach`; removal retries briefly on Windows file locks).
Git is always run with an isolated configuration (`GIT_CONFIG_GLOBAL` pointing
at an empty file, `GIT_CONFIG_NOSYSTEM=1`, `core.autocrlf=false`).

### Guards (`flowproof/guards/`)

```python
@dataclass(frozen=True)
class Fingerprint:
    files: Mapping[str, str]    # relative path -> sha256 of exact bytes
def fingerprint(root: Path, *, include: Sequence[str] = ("**",),
                exclude: Sequence[str] = DEFAULT_EXCLUDES) -> Fingerprint
@dataclass(frozen=True)
class FingerprintDiff:
    added: tuple[str, ...]; removed: tuple[str, ...]; modified: tuple[str, ...]
    @property
    def clean(self) -> bool
def compare(before: Fingerprint, after: Fingerprint) -> FingerprintDiff

class GuardViolation(Exception)
class ReadOnlyGuard:            # context manager over a directory
    def __enter__(self) -> ReadOnlyGuard
    def __exit__(...) -> None   # raises GuardViolation with the diff if changed

@dataclass(frozen=True)
class TamperFinding:
    kind: str     # "modified-test", "deleted-test", "added-test-file",
                  # "skip-marker", "trivial-assert", "config-override", ...
    path: str
    evidence: str # a short excerpt
@dataclass(frozen=True)
class TamperReport:
    findings: tuple[TamperFinding, ...]
    @property
    def tampered(self) -> bool

class FrozenTests:
    def __init__(self, patterns: Sequence[str])   # globs, e.g. ("tests/**",)
    @classmethod
    def capture(cls, base_root: Path, patterns: Sequence[str]) -> FrozenTests
    def inspect(self, lease_root: Path) -> TamperReport   # no writes
    def restore(self, lease_root: Path) -> TamperReport   # writes frozen bytes back,
                                                          # removes added files under the patterns

@dataclass(frozen=True)
class Grade:
    claimed: CommandResult      # the command as the agent left the tree
    true: CommandResult         # after restoring frozen tests
    claimed_passed: bool | None # None if the command could not run
    true_passed: bool | None
    tamper: TamperReport
    tests_passed: int | None    # parsed from the true run when recognisable
    tests_failed: int | None
    @property
    def false_pass(self) -> bool    # claimed passed but true did not
def grade(lease: Lease, command: str, frozen: FrozenTests, *,
          timeout_s: float = 600) -> Grade
```

The tamper heuristics also scan non-frozen files the agent touched for
signals such as newly added skip decorators, `pytest.skip`, `@unittest.skip`,
`assert True`, `sys.exit(0)` in test entry points, edits to `conftest.py`,
`pytest.ini`, `setup.cfg`, `pyproject.toml` test sections or `package.json`
test scripts. Heuristic findings are labelled as such; only frozen-path changes
are definitive.

### Evidence (`flowproof/evidence/`)

```python
class EvidenceError(Exception)
class EvidenceStore:
    def __init__(self, root: Path)
    def create_run(self, label: str) -> RunRecord    # new unique directory, never reused
class RunRecord:
    id: str
    root: Path
    def write_bytes(self, rel: str, data: bytes) -> str   # returns sha256; refuses overwrite
    def write_text(self, rel: str, text: str) -> str
    def write_json(self, rel: str, value: Any) -> str      # sorted keys, indent 2, trailing \n
    def event(self, kind: str, **fields: Any) -> str       # append to events.jsonl, hash-chained
    def seal(self) -> str                                  # writes manifest.json; returns its sha256
    sealed: bool
@dataclass(frozen=True)
class VerifyReport:
    ok: bool
    problems: tuple[str, ...]
def verify(run_root: Path) -> VerifyReport
def supersede(store: EvidenceStore, old_id: str, new_id: str, reason: str) -> str
```

Each event line is `{"seq": n, "kind": ..., "at": iso-utc, "prev": sha256-of-previous-line, ...fields}`.
`manifest.json` lists every file (except itself) with its byte length and
sha256, the event chain head, and a format version. `verify` recomputes all of
it, flags missing, extra or changed files and broken chains, and works after
the directory is moved or cloned elsewhere. Writes after `seal` raise. The
repository's `.gitattributes` marks evidence directories `-text` so line-ending
conversion can never break a seal.

## Selection (owned by selection, `flowproof/select/`)

```python
@dataclass(frozen=True)
class Candidate:
    id: str
    change: Change | None
    grade: Grade | None
    outcome: Outcome

class Preference(StrEnum):
    FIRST = "first"; SECOND = "second"; TIE = "tie"; INVALID = "invalid"
Judge = Callable[[Candidate, Candidate], Preference]   # shown (a, b) in that order

@dataclass(frozen=True)
class Selection:
    chosen: str | None          # candidate id, or None when abstaining
    basis: tuple[str, ...]      # policy names that decided, in order
    fallback: str | None        # "incumbent", "first", "abstain" if used
    trace: Mapping[str, Any]    # per-policy evidence for the report

def by_tests(cands) -> list[list[str]]                 # tiers of ids, best first
def pairwise(cands, judge: Judge) -> PairwiseResult    # both orders per pair
def behavior_clusters(cands) -> list[list[str]]        # identical per-test outcome vectors
def exact_vote(cands) -> list[list[str]]               # identical normalised patches
def choose(cands, chain: Sequence[str], *, judge: Judge | None = None,
           incumbent: str | None = None, fallback: str = "abstain") -> Selection
```

`pairwise` asks the judge about every unordered pair in both presentation
orders. A candidate wins a pair only when both answers name it; disagreement
is a draw and is counted as position inconsistency. Scores are Copeland
scores; cycles in the win graph are reported. `PairwiseResult` also reports
first-position and second-position preference rates so position bias can be
measured. `choose` applies the chain in order (`"tests"`, `"behavior"`,
`"pairwise"`, `"exact"`), keeping only the best tier at each step; it never
treats missing evidence as a loss and records when a fallback decided.

```python
@dataclass(frozen=True)
class Headroom:
    n: int; pass_at_1: float; random_choice: float; oracle: float
    selected: float; regret: float
def headroom(fields: Sequence[tuple[Sequence[Candidate], Selection]],
             correct: Callable[[Candidate], bool]) -> Headroom
```

`simulate.py` provides a seeded Monte Carlo of best-of-N under correlated
candidate errors, judge accuracy and position bias, and imperfect tests. It is
used to explain when fan-out pays, and returns plain dictionaries for
plotting.

## Testbed (owned by testbed, `flowproof/testbed/`)

Rule-learning grid games where an agent must infer hidden mechanics from
observation, and harness arms that differ only in how the agent is allowed to
act. The model never sees game source: the runner sends it observations and
receives structured actions, so rules cannot leak through the filesystem.

```python
@dataclass(frozen=True)
class Frame:
    grid: tuple[str, ...]       # rows of single-character glyphs
    level: int                  # 1-based
    levels: int
    actions: tuple[str, ...]    # legal action words, e.g. ("up","down","press 2 3")
    status: str                 # "playing", "level-cleared", "won"

class Game(Protocol):
    name: str
    levels: int
    def start(self, level: int, seed: int) -> State
    def step(self, state: State, action: str) -> State   # illegal action -> unchanged state
    def frame(self, state: State) -> Frame
    def solved(self, state: State) -> bool
    def key(self, state: State) -> Hashable              # for search

def optimal_actions(game: Game, level: int, seed: int, *, limit: int = 2_000_000) -> int
```

`ModelClient` is the only way the testbed talks to a model:

```python
@dataclass(frozen=True)
class Completion:
    text: str
    usage: Usage
    error: str | None = None
class ModelClient(Protocol):
    def complete(self, prompt: str, *, max_credits: float | None) -> Completion
```

Arms (all use the same observation format and the same prompt preamble; they
differ only in the response contract and the runner's behaviour):

- `single`: exactly one action per call.
- `plan`: a list of up to `K` actions per call; the runner plays them all,
  stopping early only at a level change.
- `predict`: a list of up to `K` steps, each an action plus the cells the
  agent expects to change (`"r,c=g"` items, or an empty list). The runner
  plays steps until the first prediction that does not match the observed
  change, stops, and shows the agent the actual change next to its prediction.
- `adaptive`: starts each level in `predict`; after `M` consecutive verified
  predictions it switches to `plan` for the rest of the level; any later
  mismatch or a new level switches it back.

`run_episode(game, seed, arm, client, ledger, *, K=12, M=4, window=6) -> Episode`
plays from level 1 until the game is won, the ledger refuses, or a call cap is
reached. Malformed responses are recorded as invalid calls and play nothing.
`Episode` records levels cleared, per-level actions and optimal actions,
calls, usage, stop reason (`"won"`, `"limited"`, `"accounting-unknown"`,
`"call-cap"`, `"invalid-streak"`), and the full transcript.

Score: for each cleared level, efficiency `min(1, optimal / actions)`; the
episode score is the level-weighted mean (level `l` has weight `l`), with
uncleared levels scoring 0.

Each game module defines a `CANARY` string that never appears in prompts;
`canary_hits(transcript)` must return an empty list for any honest run.

## Language (owned by language, `flowproof/lang/`)

See [LANGUAGE.md](LANGUAGE.md) for syntax and semantics. The runtime consumes
`lang.parse(text, path) -> Workflow` and `lang.check(workflow, bindings=None) -> CheckReport`.

## Runtime (owned by runtime)

Bindings map roles to `harness[:model]`, for example
`author=copilot:gpt-5.6-sol`. The workspace backend is one run option
(`--workspace inplace|guarded|copy|worktree`, default `worktree`) because two
roles editing the same branch must share one workspace; isolated forks lease
child workspaces from it. Harnesses implement:

```python
@dataclass(frozen=True)
class AgentRequest:
    role: str
    prompt: str
    mode: str                   # "answer" (no writes) or "edit"
    workspace: Path | None
    model: str | None
    max_credits: float | None
    timeout_s: float
    label: str
@dataclass(frozen=True)
class AgentResponse:
    text: str
    usage: Usage
    outcome: Outcome
    raw: Mapping[str, Any]
class Harness(Protocol):
    name: str
    reports: frozenset[str]       # units it can measure
    capabilities: frozenset[str]  # "answer", "edit", "shell"
    def invoke(self, request: AgentRequest) -> AgentResponse
```

## Clean-room rule

Flowproof is an independent implementation. Contributors work only from this
repository's documents. Do not consult, copy or paraphrase code from any other
agent-workflow engine.
