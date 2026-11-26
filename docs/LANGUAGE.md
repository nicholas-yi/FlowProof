# The Flowproof language

A Flowproof workflow (`.fp`) says which agents do what, in which order, with
which evidence, and at what maximum cost. It deliberately cannot do general
computation: no recursion, no unbounded loops, no shell commands built from
agent output. In exchange, every workflow can be type-checked, drawn and
bounded before a single agent runs.

```text
workflow fix_and_pick
  needs issue: text
  role author can edit
  role reviewer can answer
  budget at most 60 credits, 12 calls

  let plan = ask reviewer: "Outline a minimal fix for: {issue}"

  fork 3 isolated into attempts
    let patch = edit author: """
      Fix this issue: {issue}
      A reviewer suggested: {plan}
      Attempt {branch} of 3.
      """
    keep patch
  end

  let graded = test attempts with "python -m pytest -q" frozen "tests/**"
  let best = choose graded by tests, then pairwise reviewer else abstain
  require best.passed, "no attempt passed the frozen tests"
  give best
end
```

`flowproof check` reports, before anything runs, that this workflow makes
between 4 and 10 agent calls (1 plan, 3 edits, and 0 to 6 judge calls
depending on how many attempts tie on tests). It also warns if the reviewer
and the author are the same role.

## Lexical structure

- **Statements end at a newline.** Indentation is not significant; blocks
  close with `end`. Blank lines and `#` comments are ignored.
- **Names** are `[A-Za-z_][A-Za-z0-9_]*`. Keywords cannot be names.
- **Numbers** are decimal: `3`, `0.5`.
- **Strings** are `"..."` on one line, or `"""..."""` across lines. A
  triple-quoted string drops the newline right after the opening quotes and
  the common indentation of its lines. Escapes: `\"`, `\\`, `\n`, `\t`.
  `{name}` or `{name.field}` interpolates a value; `{{` and `}}` are literal
  braces.
- **Commands are literal.** The strings after `exec`, `test ... with` and
  `frozen` must not interpolate anything, so an agent can never inject text
  into a shell command (`E207`).

Keywords: `workflow end needs role can budget at most let var set ask edit exec test
with frozen choose by then else fork isolated into keep for each in repeat
until if require note give as and or not true false len`.

## Grammar

```text
file      = workflow EOF
workflow  = "workflow" NAME NL { header } { stmt } "end" NL
header    = "needs" param { "," param } NL
          | "role" NAME "can" cap { "," cap } NL
          | "budget" "at" "most" NUMBER NAME { "," NUMBER NAME } NL
param     = NAME ":" ("text" | "number" | "bool") [ "=" literal ]
cap       = "answer" | "edit" | "shell"

stmt      = ("let" | "var" | "set") NAME "=" value NL
          | action NL
          | "fork" NUMBER [ "isolated" ] "into" NAME NL { stmt } "keep" expr NL "end" NL
          | "for" "each" NAME "in" expr "at" "most" NUMBER "into" NAME NL
                { stmt } "keep" expr NL "end" NL
          | "repeat" "at" "most" NUMBER "until" expr NL { stmt } "end" NL
          | "if" expr NL { stmt } [ "else" NL { stmt } ] "end" NL
          | "require" expr "," STRING NL
          | "note" STRING NL
          | "give" expr NL

value     = action | test | choose | expr
action    = "ask" NAME ":" STRING [ "as" ("text" | "number" | "bool" | "list") ]
          | "edit" NAME ":" STRING
          | "exec" STRING
test      = "test" expr "with" STRING [ "frozen" STRING { "," STRING } ]
choose    = "choose" expr "by" policy { "," "then" policy } [ "else" ("first" | "abstain") ]
policy    = "tests" | "behavior" | "exact" | "pairwise" NAME

expr      = and { "or" and }
and       = not { "and" not }
not       = "not" not | compare
compare   = path [ ("==" | "!=" | "<" | "<=" | ">" | ">=") path ]
path      = atom { "." NAME }
atom      = NUMBER | STRING | "true" | "false" | NAME | "len" "(" expr ")" | "(" expr ")"
```

Headers (`needs`, `role`, `budget`) come before statements. Capabilities:
`answer` lets a role be asked questions; `edit` lets it change the workspace
and implies `answer`; `shell` lets it run commands itself during a call, so it
can only be bound to a harness whose agent has command tools. `budget` takes
any number of `amount unit` pairs; the unit `calls` caps agent calls and
every other unit (`credits`, `usd`, `tokens`) caps that native unit.

## Types and values

| Type | Produced by | Fields |
|---|---|---|
| `text` | `ask`, string literals, `needs x: text` | |
| `number` | `ask ... as number`, literals, `len(...)` | |
| `bool` | `ask ... as bool`, comparisons, `true`/`false` | |
| `change` | `edit` | `patch: text`, `files: list of text`, `empty: bool` |
| `result` | `exec` | `ok: bool`, `exit: number`, `out: text` |
| `graded` | `test` | `passed: bool`, `claimed: bool`, `tampered: bool`, `false_pass: bool`, `tests_passed: number`, `tests_failed: number`, `change: change`, `patch: text`, `out: text` |
| `list of T` | `fork`, `for each`, `ask ... as list`, `test` on a list | |

Every `let` name is bound once (`E202`). A `var` name may be rebound with
`set` to a value of the same type. A `var` cannot hold a list, so list sizes
stay static. It also cannot be set from inside a `fork` or `for each` branch,
because branches would race (`E219`, `E220`). Names bound inside `fork` and `for each`
bodies are local to one branch or iteration; the block's result is the list
collected by `keep`. Names bound inside `repeat` are re-bound on each
iteration and, because the body runs at least once, remain visible after the
loop with their last values. A name bound in both arms of an `if`/`else` with
the same type is visible after it; otherwise branch bindings are local. Inside
a fork body, `branch` is the branch number (1 to N).

**Unknown values.** Some values may be unknown at run time: a test count the
runner could not parse, a command that timed out, a judge that never
answered. Unknown is neither true nor false. Comparing, branching on or
requiring an unknown value stops the run with outcome `invalid`
(`unknown-condition`). It never takes either branch silently.

## Semantics

**Workspace.** A run gets one root workspace, leased from the source
directory with the backend chosen at run time (`--workspace`; default
`worktree`). Statements outside an isolated fork use the root workspace.
`fork N isolated` gives each branch its own lease of the root workspace's
current state (a `worktree` when the source is a git repository, otherwise a
`copy`). A fork without `isolated` runs its branches against the shared root,
so its body may only `ask` (`E208`).

**Agent calls.** `ask` sends a prompt in answer mode: the agent may read the
workspace but not change it. `ask ... as number`, `as bool` and `as list`
append a short reply-format instruction and parse the reply strictly (a lone
number; yes/no/true/false; a JSON array of strings or a `- ` list). A reply
that does not parse makes the stage `invalid`, not a guess. `edit` sends a
prompt in edit mode and returns the workspace's cumulative change since it
was leased. A change remembers its workspace. `test` grades it there, and only
while the workspace still holds exactly that change; otherwise the stage is
`invalid` (`stale-change`). After grading, frozen files in that workspace
hold their original content again.

**Commands.** `exec "cmd"` runs a literal command in the current workspace.
`ok` is true when the exit code is 0 and unknown when the command could not
start or timed out.

**Frozen tests.** Every `frozen` pattern in the workflow is known statically.
The runtime snapshots the matching files from the source **before the first
agent call**. `test X with "cmd" frozen "tests/**"` grades each change in its
own workspace twice. The first run is with the files as the agent left them
(`claimed`). The second is after restoring the frozen files (`passed`;
`out` is the tail of that run's output).
Changes to frozen files are reported (`tampered`), as are heuristic signals
such as new skip markers, and `false_pass` is `claimed and not passed`.

**Selection.** `choose LIST by p1, then p2 ...` applies the policies in
order, keeping the best tier after each, and stops as soon as one candidate
remains:

- `tests` ranks by frozen-test results; ungraded candidates are unranked,
  never treated as failures, and tampered candidates rank after clean ones;
- `behavior` groups candidates by identical test outcomes and keeps the
  largest group;
- `exact` groups identical normalised patches and keeps the largest group;
- `pairwise ROLE` asks the role to compare every pair in both orders and
  counts a win only when both answers agree.

If several candidates remain, `else first` takes the first in list order. The
default, `else abstain`, ends the run with outcome `failed` (`abstained`)
rather than pretending to know.

**Branches end alone.** Inside a `fork` branch or a `for each` iteration, a
failed `require`, an agent call that ends `invalid` or an unknown condition
ends only that branch. Its item in the result list is *unavailable*, and
`test` and `choose` skip it. A budget stop always ends the whole run.

**Control.** `repeat at most N until C` runs its body, then evaluates `C`;
it stops when `C` is true or after `N` iterations. `for each x in L at most N`
visits at most the first `N` items (truncation is logged). `require C, "msg"`
ends the run with outcome `failed` when `C` is false. `note "..."` writes an
event to the run log. `give X` names the run's result; it may appear once, as
the last top-level statement.

**Budget.** The runtime admits each agent call against the declared ceilings
(and any stricter ones given on the command line) and settles its usage
afterwards. A call that would start at or above a ceiling ends the run with
outcome `limited`. A capped unit whose spend is unknown ends it with outcome
`invalid` (`accounting-unknown`).

### The repair loop

`var` and `set` exist for the most common agent loop: edit, grade, and feed
the failure back.

```text
workflow repair
  needs task: text
  role author can edit
  budget at most 40 credits, 4 calls

  let first = edit author: "{task}"
  var check = test first with "python -m pytest -q" frozen "tests/**"
  if not check.passed
    repeat at most 3 until check.passed
      let fix = edit author: """
        The frozen tests still fail:
        {check.out}
        Fix the code, not the tests.
        """
      set check = test fix with "python -m pytest -q" frozen "tests/**"
    end
  end
  require check.passed, "still failing after 4 attempts"
  give check
end
```

Its bound is 1 to 4 calls, so the `calls` ceiling is exactly the worst case.

## Static checks

`flowproof check` parses, resolves and type-checks the workflow and computes
its call bound. With `--bind`, it also checks the bindings. Errors block a
run; warnings do not.

| Code | Severity | Meaning |
|---|---|---|
| `E100` | error | syntax error |
| `E201` | error | name used before it is bound |
| `E202` | error | name bound twice |
| `E203` | error | role used but not declared |
| `E204` | error | role lacks the capability its use needs (`edit` needs `can edit`) |
| `E205` | error | type mismatch |
| `E206` | error | unknown field |
| `E207` | error | command or frozen pattern interpolates a value |
| `E208` | error | `edit`, `exec` or `test` inside a fork that is not `isolated` |
| `E209` | error | `pairwise` over a list with no size bound |
| `E210` | error | the minimum number of calls exceeds the `calls` ceiling |
| `E211` | error | a bound harness cannot measure a budgeted unit |
| `E212` | error | a bound harness lacks a capability a role declares |
| `E213` | error | a role has no binding |
| `E214` | error | `edit` or `test` in a read-only workspace (`guarded`); grading restores frozen files |
| `E215` | error | `keep` missing or not the last statement of its block |
| `E216` | error | `give` not last, or given twice |
| `E217` | error | duplicate input, role or budget unit |
| `E218` | error | a bound of zero (`fork 0`, `at most 0`) |
| `E219` | error | `set` on a name that was not declared with `var` |
| `E220` | error | `set` on an outer `var` from inside a `fork` or `for each` branch |
| `E221` | error | more than one `pairwise` policy in one `choose` |
| `W301` | warning | bound but never used |
| `W302` | warning | the judge role also authored the candidates it judges |
| `W303` | warning | `pairwise` without `tests` earlier in the chain: judge-only selection |
| `W304` | warning | `test` without `frozen`: tampering cannot be detected |
| `W305` | warning | the worst case exceeds a ceiling, so the run may be limited |
| `W306` | warning | a `repeat` condition does not depend on anything its body binds or sets |
| `W307` | warning | edits in a `shared` (in-place) workspace change the caller's files |
| `W308` | warning | agent calls without a `budget` |

### The call bound

The checker computes an interval `[lo, hi]` of agent calls for every
statement and role. `lo` is the fewest calls on any path that reaches the
end; `hi` is the most on any path (possibly unbounded).

| Construct | Calls |
|---|---|
| `ask`, `edit` | `[1, 1]` for that role |
| `exec`, `test`, `note`, `require` | `[0, 0]` |
| sequence | sum of intervals |
| `if A else B` | `[min(lo_A, lo_B), max(hi_A, hi_B)]`; a missing `else` is `[0, 0]` |
| `fork N` | `N` times the body |
| `for each ... at most N` over a list of size `[a, b]` | `[min(a, N) * lo, min(b, N) * hi]` |
| `repeat at most N` | `[lo, N * hi]` (the body runs at least once) |
| `pairwise` over a pool of size `[a, b]` | first policy: `[a(a-1), b(b-1)]`; later: `[0, b(b-1)]` |

List sizes are tracked the same way: `fork N` makes exactly `N` items,
`for each ... at most N` at most `N`, and `ask ... as list` an unbounded number
(hence `E209`). If the `calls` ceiling is below `lo`, the workflow can never
finish (`E210`); if it is below `hi`, it may be limited (`W305`).
`flowproof estimate --price ROLE=CREDITS` turns the per-role call intervals
into a credit interval and compares it with the credit ceiling in the same
way.

## Tools

- `flowproof check FILE [--bind ROLE=HARNESS[:MODEL] ...] [--workspace KIND]`
- `flowproof fmt FILE [--write]` prints the canonical form; `fmt` is idempotent and
  `parse(fmt(w)) == parse(w)`.
- `flowproof graph FILE` prints a Mermaid flowchart.
- `flowproof estimate FILE --price ROLE=CREDITS ...`
- `flowproof run FILE --bind ... [--input NAME=VALUE] [--source DIR] [--evidence DIR]`
