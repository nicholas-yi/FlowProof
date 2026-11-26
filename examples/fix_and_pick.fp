# Three isolated attempts, graded by frozen tests; a reviewer breaks ties.
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
