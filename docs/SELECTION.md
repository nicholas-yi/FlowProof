# Selection

`flowproof/select/` picks one candidate from a best-of-N field, reports how much better
that is than one attempt, and models when fan-out is worth paying for. It implements the
"Selection" contract in [DESIGN.md](DESIGN.md). It has no runtime dependency on
`flowproof.workspace` or `flowproof.guards`. `select/types.py` defines structural protocols
(`ChangeLike`, `GradeLike`, `TamperLike`) for exactly the attributes it reads:

| Object | Attributes read |
|---|---|
| change | `patch: str`, `empty: bool` |
| grade | `true_passed: bool \| None`, `tests_passed: int \| None`, `tests_failed: int \| None`, `tamper.tampered: bool` |
| grade (optional) | `per_test: Mapping[str, bool]` (test id to passed), read with `getattr` |

All results are sorted by candidate id, so they never depend on the order of the input
(a hypothesis property checks this). Candidate ids must be unique.

## Policies

Each policy turns a pool into a `Ranking`: ranked `tiers` (best first, ties share a tier)
plus `unranked` candidates it had no evidence for. The list-returning functions in
DESIGN.md append the unranked candidates as a final tier or group.

| Chain name | Function | Ranks by | Unranked when |
|---|---|---|---|
| `tests` | `by_tests` | untampered before tampered, then passed before failed, then more `tests_passed` first | outcome not OK, no grade, or `true_passed is None` |
| `behavior` | `behavior_clusters` | size of the cluster with identical behaviour | as `tests` |
| `pairwise` | `pairwise` | Copeland score from a judge asked in both orders | every pair involving it was INVALID |
| `exact` | `exact_vote` | size of the group with an identical normalised patch | outcome not OK, or no change |

- **tests.** A tampered candidate ranks after every untampered one even if it passed, and is
  listed under `flagged["tampered"]`. Test counts are compared only when both are known.
  A candidate with an unknown count is beaten by nobody in its pass/fail class, so it
  joins that class's top tier.
- **behavior.** Clusters use per-test outcome vectors when every ranked candidate has
  `per_test`. Otherwise they use the `(true_passed, tests_passed, tests_failed)` triple.
  `detail["mode"]` records `"per-test"` or `"summary"`. Clusters are ordered largest first,
  with ties broken by smallest member id. That order is only for display. Inside `choose`,
  clusters of equal size form one tier, because a vote between equal clusters is a tie.
- **exact.** `normalise_patch` drops `index` lines and the line numbers in hunk headers
  (`@@ -1,3 +1,4 @@ f` becomes `@@ @@ f`). It also drops trailing whitespace (including
  CR) and lines that are blank or add, remove or keep only a blank line. Empty changes form
  their own group (`flagged["empty-patch"]`) and compete like any other group.
- **pairwise.** See the next section. Cycles in the strict win graph appear in
  `PairwiseResult.cycles` and `flagged["in-cycle"]`.

## Why the judge sees both orders

LLM judges often prefer whichever candidate is shown first. A single call per pair cannot
tell that bias from a real preference. `pairwise` asks `judge(a, b)` and `judge(b, a)` for
every unordered pair (`n(n-1)` calls):

- A candidate wins only if both answers name it.
- TIE in both orders is a consistent draw.
- Any other disagreement is a position-inconsistent draw. This includes choosing the
  first-shown candidate both times, and TIE in one order only.
- INVALID in either order leaves the pair unjudged. A candidate with no judged pair is
  unranked.

`PairwiseResult` reports:

- the wins matrix, draws, inconsistent and unjudged pairs;
- Copeland scores (wins minus losses), tiers and cycles;
- `first_position_rate` and `second_position_rate`: the share of FIRST/SECOND answers that
  named the first- or second-shown candidate;
- `consistency`: the share of judged pairs whose two answers agree;
- `calls`, and every raw answer.

A judge that always answers FIRST wins nothing, has consistency 0 and has a first-position
rate of 1.

## Why missing evidence is not a loss

INVALID, LIMITED or CANCELLED runs, ungraded candidates and unjudged pairs say nothing about
quality. Counting them as failures would let a flaky grader or a timeout decide the result.
So:

- They are reported as `unranked` with a reason (`detail["unranked_reasons"]`), never as
  losers.
- `choose` drops unranked candidates only when some candidate in the pool has evidence. The
  trace lists them under `dropped_unranked`.
- If nobody in the pool has evidence, the policy leaves the pool unchanged.
- An unranked incumbent is never dropped (see below).

The `tests` tiers rank a known failure above an unranked candidate, as this package's
specification requires.
If every ranked candidate failed, the pool can therefore narrow to failures. Read the trace
in that case.

## `choose` and incumbent retention

`choose(cands, chain, *, judge=None, incumbent=None, fallback="abstain")` works as follows:

1. **Validate.** It raises `ValueError` for:
   - an unknown policy name;
   - `"pairwise"` in the chain without a judge;
   - an unknown fallback;
   - an incumbent id that is not among the candidates;
   - duplicate candidate ids;
   - a chain given as a bare string.
2. **Narrow.** It applies each policy to the current pool and keeps only the best tier. It
   stops early once one candidate remains; later policies are listed under
   `trace["skipped"]` and the judge is not called.
3. **Record.** `basis` lists the policies that actually narrowed the pool, in order.
4. **Resolve a final tie.**
   - The incumbent, if it is still in the pool, is chosen whatever `fallback` says (the
     selection reports `fallback="incumbent"`).
   - Otherwise `"first"` chooses the smallest id.
   - Otherwise `"abstain"` chooses `None`. So does `"incumbent"` when there is no incumbent
     or evidence has removed it.

The incumbent leaves the pool only when a policy ranks it strictly below that policy's best
tier. Then every survivor was placed strictly above it, and `trace["incumbent_eliminated_by"]`
names the policy. An equal challenger never replaces the incumbent. When the incumbent has
no evidence under a policy it stays in the pool (`"kept-without-evidence"`). A later policy
with evidence can still remove it. Whenever the incumbent is chosen, `basis` ends with
`"incumbent-retained"`.

`trace` holds one entry per step: the pool, tiers, unranked candidates, flags, policy detail
(for `pairwise`, the full `PairwiseResult`), the candidates kept and the incumbent's status.
It also holds the final pool, the requested fallback and the total number of judge calls.
Every value is JSON-ready.

## Headroom reporting

`headroom(fields, correct)` takes `(candidates, selection)` pairs and a ground-truth
predicate. It returns averages over `n` fields plus one `HeadroomRow` per field:

| Field | Meaning per field |
|---|---|
| `pass_at_1` | share of candidates that are correct: the expected value of one random attempt |
| `random_choice` | the same number, kept separate so reports name the baseline |
| `oracle` | 1 if any candidate is correct |
| `selected` | 1 if the chosen candidate is correct; abstaining or choosing wrong scores 0 |
| `regret` | `oracle - selected` (on the averages) |

`abstained` counts the fields with no choice. A field with no candidates scores 0
everywhere. An empty `fields` list, or a choice that is not in its field, raises
`ValueError`. Always report all five numbers. `selected` alone hides whether the field had
any headroom (`oracle - pass_at_1`) and how much of it the policy recovered.

## Simulation

`flowproof.select.simulate.simulate(...)` is a seeded Monte Carlo that uses only `random`
and `math`. For each task it draws:

- **Difficulty.** `d ~ N(0, task_difficulty_sd)`. A candidate solves the task with
  probability `logistic(ability - d)`, where `ability` is calibrated so the mean solve rate
  is `p_solve_mean`.
- **Shared errors.** Each candidate copies a shared per-task draw with probability
  `sqrt(correlation)`. This gives a within-task correlation of `correlation`. Copiers write
  identical patches, so identical patches become more likely as correlation rises.
- **Patches of independent candidates.** A wrong one writes a unique patch. A correct one
  writes the canonical correct patch with probability `convergence`. This parameter is an
  addition to the requested signature and defaults to 0.
- **Tests.** One verdict per distinct patch, with `test_sensitivity` and
  `test_specificity`.
- **Judge.** Each call answers "first" with probability `judge_position_bias`. Otherwise it
  names the correct member of a mixed pair with probability `judge_accuracy`, and guesses on
  an equal pair.

The simulated policies are `single`, `random`, `tests`, `pairwise`, `tests+pairwise`,
`exact-vote` and `oracle`. They follow `choose`: keep the best tier, then score the exact
expected value of a uniform tie-break. All policies and Ns share the same draws, so
comparisons are paired and sweeps change only thresholds.

`simulate` returns per-N `success`, `stderr` and `judge_calls` lists. `sweep(parameter,
values, **settings)` varies one parameter. `standard_sweeps()` runs five sweeps (24 runs) in
about 1 s on the development machine.

Numbers below are model outputs, not measurements of real agents. They come from
`sweep(name, values, tasks=2000, seed=0, n_candidates=(1, 8))`; every other setting is the
default: `p_solve_mean=0.3`, `task_difficulty_sd=1`, `correlation=0.3`,
`judge_accuracy=0.7`, `judge_position_bias=0.2`, `test_sensitivity=0.9`,
`test_specificity=0.8`. The standard error is about 0.01. All values are at N = 8.

- **Correlated errors cap the gain from fan-out.** The oracle falls from 0.84 at
  `correlation=0` to 0.70 at 0.3 and 0.37 at 0.9. At 0.9, `tests+pairwise` reaches 0.36,
  against 0.30 for a random pick.
- **Position bias destroys pairwise selection even with both orders.** At accuracy 0.7,
  pairwise scores 0.57 with no bias, 0.54 at bias 0.2, 0.40 at 0.8, and 0.30 (equal to
  random) at bias 1.0. A 0.5-accuracy judge at bias 0.2 is no better than random (0.303
  against 0.302), at 56 calls per task.
- **Tests first, then the judge, is best and cheapest.** `tests+pairwise` scores 0.61 using
  21 judge calls per task, against 0.54 for `tests` alone and 0.54 for `pairwise` alone
  (56 calls). With specificity 0.5 instead of 0.8, `tests` drops to 0.40.
- **Exact-patch voting measures agreement, not correctness.** With no convergence it matches
  random (0.30 to 0.31) at every correlation; at `correlation=0` it is identical by
  construction. Only when correct answers converge does it help: 0.38 at
  `convergence=0.3` and 0.51 at 0.6, with `correlation=0`.

At N = 16 with defaults (`simulate(tasks=2000, seed=0)`), the oracle is 0.82,
`tests+pairwise` is 0.71 (68 judge calls per task), `pairwise` is 0.69 (240 calls),
`tests` is 0.57, and `random` is 0.29. Results are deterministic for a given seed and set of
arguments. Changing the largest N changes the draw layout and so the exact numbers.

## Validation

Run these from the repository root with the project virtual environment:

```text
.venv\Scripts\python.exe -m pytest tests/select
.venv\Scripts\ruff.exe check flowproof/select tests/select
.venv\Scripts\pyright.exe flowproof/select
```

The simulation tests check:

- determinism for a seed;
- that no policy beats the oracle;
- that perfect tests equal the oracle;
- that a perfect unbiased judge equals the oracle;
- that a 0.5-accuracy, always-first judge equals random;
- that `standard_sweeps()` finishes in under 5 s.

These tests establish internal consistency only. They do not show that the model's
parameters describe real agents or judges.
