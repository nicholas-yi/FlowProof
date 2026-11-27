# Research

Every study here follows [the experimental rules](../docs/METHODS.md#experimental-rules).
Each study's design and analysis were pre-registered in `preregistration/` and committed
before its first evaluation run. Every campaign writes one sealed, hash-chained record to
`runs/`. `analyze.py` verifies each record before reading it, and writes `results/`.

| Study | Question | Pre-registration | Runner |
|---|---|---|---|
| E1 | Under equal credit ceilings, which harness arm clears more of an unseen rule-learning game, and does it depend on budget? | [E1.md](preregistration/E1.md) | `e1_arms.py` |
| E2 | Do coding agents tamper with graders, or special-case code, when the task is impossible? | [E2.md](preregistration/E2.md) | `e2_tamper.py`, tasks in `e2_tasks.py` |
| E3 | Can models write Flowproof, and do checker diagnostics make repairs work? | [E3.md](preregistration/E3.md) | `e3_generate.py` |
| O1 | What does reading unmeasured usage as zero do to budgets and cost comparisons? | (offline, deterministic) | `offline.py` |
| O2 | When does best-of-N fan-out pay, and what does judge position bias cost? | (offline, deterministic) | `offline.py` |

## Reproduce

```bash
python -m pip install -e ".[dev,figures]"
python -m research.offline                 # O1 and O2: no model, deterministic
python -m research.analyze                 # verify every sealed record, write results/
python -m research.explore                 # exploratory breakdowns -> results/EXPLORATORY.json
python -m research.figures                 # docs/figures/*.svg
flowproof evidence verify research/runs/<run-id>
```

Rerunning the live studies needs the Copilot CLI, signed in, and spends credits:

```bash
python -m research.e1_arms run --model claude-haiku-5.5 --budgets 1.5,4,10 --workers 10
python -m research.e2_tamper run --models claude-sonnet-5.5,gpt-5.6-sol,gpt-5.6-luna --reps 2
python -m research.e3_generate run --models claude-haiku-5.5,gpt-5.6-luna,claude-sonnet-5.5
```

Each runner also has an offline mode: `--client oracle` for E1, `--dry-run` for E2 and E3.

## Records

- **Contents:** a record holds the plan, the provenance (package version, git head,
  Python, platform, and the preregistration's path and SHA-256) and every job's result,
  all hash-chained.
- **Redacted copies:** some records contain absolute local paths, because agents echo them
  in their replies. For those, the repository holds a redacted copy made by
  `scripts/redact_record.py`. The copy names the original's id and manifest digest in
  `redaction.json`, and the original is kept privately. Redaction replaces paths and
  account names only; it never changes results.

## Exploratory analyses

[`explore.py`](explore.py) adds descriptive breakdowns that were not pre-registered:
- per game, per level and per arm;
- prediction accuracy after rule changes;
- ledger overshoot and power;
- E2 agent behaviour and E3 failure causes;
- campaign totals.

The README labels every number it takes from there as exploratory.

E2's per-run agent transcripts and patches were first written to a private scratch store, because
they contain local paths. [`e2_detail.py`](e2_detail.py) copies them into a separate sealed record
(`E2-detail`), with paths, account names and CLI session ids replaced, and adds behaviour features
read from the transcripts. Every behaviour number can therefore be recomputed from the repository.

## Costs

Credits are the Copilot CLI's AI credits, as reported in each call's usage file. The
cost of each campaign is in its record and in `results/`.
