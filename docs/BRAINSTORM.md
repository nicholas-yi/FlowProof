# Brainstorm: continuations and open ideas

None of these are built yet. Each item gives the idea, why it might matter, and the
smallest experiment that would test it. Ordered roughly by expected value per effort.

## Measurement

1. **How often does a verdict flip?** Rerun each comparison K times with fresh seeds and
   report how often the winner changes. A comparison whose verdict flips 30% of the time
   is not a result.
   - Test: rerun one E1 cell 5 times and report the distribution of the winner.
2. **Measure the tamper detector itself.** Hand-label every E2 transcript and report the
   precision and recall of the heuristic findings, which are currently only labelled
   *heuristic*.
3. **Claimed versus true as a CI monitor.** Run every agent-authored pull request's tests
   twice (as submitted, then with the base branch's tests restored) and flag false passes
   automatically.
4. **Credits per verified outcome.** Report spend per frozen-test pass instead of per
   call, so cheap but unreliable arms can't look efficient.

## Orchestration

5. **Early-stop fork.** `fork 5 isolated until passed`: launch branches one at a time,
   or in small waves, and stop once one passes the frozen tests. The static upper bound is
   unchanged and the expected cost drops by roughly a factor of 1/pass@1.
   - Test: compare it with a full fork on the same tasks at equal ceilings.
6. **Choose N from measurements.** Use measured pass@1, test sensitivity and specificity,
   and judge accuracy with `select.simulate` to pick the fork width that maximises
   expected success per credit. The simulation's curves (study O2) make the trade-off
   explicit.
7. **Judges from a different family.** Extend the self-review lint (W302) to warn when a
   judge shares a model family with the author, not just the role.
8. **Clustering by differential testing.** Generate extra inputs and cluster candidates
   by their outputs, in the style of AlphaCode, when the frozen tests are too coarse to
   separate them.
9. **A self-improving workflow, gated.** Let an agent propose edits to a `.fp` workflow,
   and accept an edit only on a fresh, frozen, simultaneous win over the incumbent
   workflow (rule 4 in METHODS.md).

## Language

10. **Cost annotations.** `role author can edit costs at most 6 credits per call` would
    turn the static call bound into a static credit bound without command-line prices.
11. **Contracts.** `ensure` clauses on `give` (for example "the change touches no file
    under `tests/`") would be checked by the runtime and recorded as evidence.
12. **Record and replay.** A replay harness would re-execute a sealed run deterministically
    from its recorded responses, for debugging and for auditing changes to selection
    policies.

## Testbed

13. **Tuning the adaptive arm.** Sweep `M` (the predictions needed before switching) and
    let the arm switch back after a mismatch inside a plan, not only between calls.
14. **Predicting only where it is new.** Ask for predictions only for actions the agent
    has not seen in the current level, and batch the rest.
15. **Harder rule changes.** Rules that change mid-level, or depend on history, to test
    whether gating helps more when the world is less stationary.

## Safety

16. **A real sandbox.** E2 showed that a shell allowlist is not a sandbox. Run agentic
    sessions in a container with the workspace mounted and no network, and keep the
    fingerprint check as a second line of defence.
