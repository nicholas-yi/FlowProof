# Edit, grade against frozen tests, and feed the failure back at most three times.
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
