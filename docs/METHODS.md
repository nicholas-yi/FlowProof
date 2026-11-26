# Methods

This page lists the orchestration and measurement methods Flowproof implements and the
experimental rules its studies follow. Each one says where it comes from:
- **established:** published, with a citation;
- **prior work:** methods developed earlier by others that this project reimplements
  independently, without claiming them;
- **new here:** introduced by this project, as far as I know. I have not done a
  systematic literature search, so treat these as candidates, not priority claims.

## Orchestration

| Method | What it does | Where it lives | Origin |
|---|---|---|---|
| Workflows as checked programs | A workflow is a source file that is parsed, type-checked and drawn before any agent runs. | `flowproof/lang` | prior work (the idea); the language itself is new here |
| Static call bounds | Interval arithmetic over forks, loops, branches and pairwise judging gives the fewest and most agent calls per role before running. A budget the workflow can never meet is an error, and one it might exceed is a warning. | `lang/checker.py` | new here |
| Literal commands | `exec`, `test` and `frozen` strings cannot interpolate values, so agent output never reaches a shell. | `lang/checker.py` (E207) | new here |
| Late role binding | A workflow names roles; the command line binds each to a harness and model. | `runtime/bindings.py` | prior work |
| Typed outcomes | `failed` (a real negative), `invalid` (untrustworthy), `limited` (stopped by budget) and `cancelled` are never merged. | `outcomes.py` | prior work |
| Branch-local failure | A failure inside one fork branch removes that candidate, not the run. A budget stop always ends the run. | `runtime/executor.py` | new here |
| Unknown is neither true nor false | A condition on an unmeasured value ends the run `invalid` (three-valued logic), instead of taking a branch. | `runtime/executor.py` | new here |
| Isolation tiers | `inplace`, `guarded` (read-only and fingerprinted), `copy` and `worktree`. The worktree base is pinned from the working tree, including uncommitted and untracked files, using a scratch index. | `flowproof/workspace` | prior work (tiers); the pinning method is new here |
| Best-of-N selection, execution first | Rank by frozen tests, then by behaviour, then by a judge. Missing evidence is never counted as a loss. | `flowproof/select` | established ([Chen et al., 2021](https://arxiv.org/abs/2107.03374); [Li et al., 2022](https://arxiv.org/abs/2203.07814)) |
| Pairwise judging in both orders | A win counts only when both presentation orders agree. Results are scored by the Copeland method, cycles are reported, and the first-position rate is measured. | `select/judging.py` | established ([Zheng et al., 2023](https://arxiv.org/abs/2306.05685) on position bias) |
| Behavioural clustering | Group candidates by identical per-test outcomes rather than identical patches. | `select/ranking.py` | established ([Li et al., 2022](https://arxiv.org/abs/2203.07814)) |
| Abstain or keep the incumbent | Ties are not broken by guessing; a challenger must be strictly better to replace the incumbent. | `select/chain.py` | prior work |
| Headroom reporting | Report pass@1, random choice, oracle, selected and regret together. | `select/report.py` | established (pass@k, Chen et al., 2021); the combined report is prior work |
| Self-review lint | Warns when a judge role also authored the candidates it judges. | `lang/checker.py` (W302) | new here |
| Rule-learning harness arms | `single`, `plan` (batch K actions) and `predict` (batch actions with predicted effects, stop at the first wrong prediction). | `flowproof/testbed` | prior work |
| Adaptive arm | Start each level predicting, then switch to plain batching after M predictions in a row come true. | `testbed/runner.py` | new here |
| Bounded, stable context | The prompt carries a fixed window of recent (action, observed change) records and a summary of earlier levels. | `testbed/runner.py` | prior work |

## Measurement

| Method | What it does | Origin |
|---|---|---|
| Native units | Usage is kept in each provider's unit (credits, premium requests, tokens) and never converted silently. | prior work |
| Unknown is not zero | An unmeasured quantity stays unknown through every sum. A ceiling in that unit stops the run instead of letting it look cheap (see study O1). | prior work (principle); the ledger is new here |
| Between-call admission plus provider cap | The ledger checks ceilings between calls, and each call passes the remaining budget to the provider as a hard cap. Overshoot is bounded by one call. | new here |
| Frozen graders, claimed versus true | Tests are snapshotted before the first agent call. Grading runs the test command as the agent left the tree, then again after restoring the snapshot. A **false pass** is a claimed pass the frozen tests do not confirm. | prior work (frozen tests); the two-run grade is new here |
| Tamper findings | Definitive findings are frozen files modified, deleted or added. Heuristic findings are new skip markers, trivial asserts, early exits and test-configuration edits outside the frozen set, and they are labelled as heuristic. | new here |
| Grading from source | Graders and `exec` run Python with an empty private bytecode cache, because a same-size edit within one second otherwise reuses the old `.pyc` and runs stale code. | new here |
| Impossible tasks | Tasks whose tests contradict each other or the specification, so any pass is a shortcut. | established ([Zhong et al., 2025](https://arxiv.org/abs/2510.20270)) |
| Sealed evidence | Append-only, hash-chained events and a manifest of every file. Verification still works after the record is moved or copied. Corrections are made by superseding a record, never by editing it. | prior work (sealing); relocation-safe verification is new here |
| Session transcripts and source integrity | The agent CLI's own transcript, including its tool calls, is kept per call, and each task's source repository is fingerprinted before and after. | new here |

## Experimental rules

These rules apply to every study in `research/`. Most are standard experimental design.
The last three were learned while building this project.

1. **Equal budget or no comparison.** Arms get the same ceiling in the same native unit.
2. **Pre-register.** Design, outcomes and analysis are fixed and committed before any
   evaluation run, and anything seen earlier is disclosed (`research/preregistration/`).
3. **Separate development from evaluation.** Development seeds and tasks size budgets and
   debug pipelines, and they never appear in results.
4. **Promote only on a fresh win.** A change replaces the incumbent only after it wins
   under frozen criteria, on items it has not seen, against an incumbent measured at the
   same time.
5. **Same build, same time.** Treatment and control run on the same code and in the same
   window. Platform drift can be larger than the effect being studied.
6. **Report invalid runs separately.** A run that could not produce a trustworthy result
   is not a failure and not a success.
7. **Prove the control can win.** Solvable controls (E2) and reference workflows (E3)
   show that the outcome is attainable, so a zero means something.
8. **Validate the scorer on its own.** Every scorer is tested against reference solutions
   and near misses before any model is scored (`tests/research`).
9. **Separate bundled interventions.** A change that alters two things gets two arms or
   none.
10. **Count whole loops and never double-count.** Resumed runs are admitted against the
    remaining budget, and failed runs keep their reports and telemetry.
11. **Name every factor in record keys.** A preflight here lost results when records from
    different models collided. The evidence store refused to overwrite them, so the loss
    was visible rather than silent.
12. **Fix tool availability per model family.** Models from different families edit files
    through different tools. Leaving one family's edit tool out of an allowlist pushed
    those models to shell workarounds, which changed how easily they could tamper.
13. **An allowlist is not a sandbox.** An allowlisted interpreter such as `python` can in
    principle run anything. In our runs the CLI still refused some `python -c` commands
    that wrote files, so the allowlist doesn't fully predict what an agent can do. Isolate
    the workspace, and check the source tree afterwards.
14. **Let CI try to break the pipeline.** On a fast CI machine, two grader tests failed
    because same-size edits within one second reused stale bytecode. The grader now runs
    from source. A local run had passed only because it was slower.
