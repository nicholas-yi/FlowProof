# E3b preregistration (exploratory): repairing from examples alone

Status: registered after E3 and before E3b's first run. This follow-up exists because
of E3's outcome, so it is labelled exploratory.

## Why

In E3, every first attempt that had the language reference met every hidden requirement
(36 of 36). That left nothing to repair, so the pre-registered comparison of diagnostic
and blind repair could not be estimated. Without the reference, 21 of 36 first attempts
failed, and 18 of those 21 did not parse. E3b asks whether the checker's diagnostics let
a model repair a workflow in a language it has seen only in three examples.

## Design

- **Items:** every `examples`-arm first attempt in the sealed E3 record that did not
  succeed (21 items: 7 per model, for `claude-haiku-5.5`, `gpt-5.6-luna` and
  `claude-sonnet-5.5`).
- **Repair conversations:** for each item, two independent ones, each of up to 2 rounds
  and stopping at the first success. Each starts from the same `examples` prompt and the
  recorded failed reply.
  - `diag`: E3's diagnostic feedback (parse or checker diagnostics, plus whether hidden
    requirements remain unmet, without naming them).
  - `blind`: "not accepted" only.
- **Scoring:** the same hidden checks, unchanged from E3.
- **Campaign cap:** 150 credits.

## Analysis

- **Comparison:** success within 2 rounds, `diag` against `blind`, paired by item, using
  McNemar's exact test pooled over models, plus counts per model.
- **Secondary:** whether repairs parse at all, by variant.
- **Status:** exploratory and descriptive.

## Limits

- **Size:** 21 items.
- **Not independent:** the items share the same three example files.
- **Diagnostics wording:** they are written for the language's own error codes, so
  messages that name the missing construct may help more than messages in general would.
