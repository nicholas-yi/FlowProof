# A scout picks files; a reviewer reads at most five; the scout summarises.
workflow review_files
  needs change_summary: text
  role scout can answer
  role reviewer can answer
  budget at most 20 credits, 7 calls

  let files = ask scout: "List the files most likely affected by: {change_summary}" as list

  for each file in files at most 5 into reviews
    let review = ask reviewer: "Review {file} for risks related to: {change_summary}"
    keep review
  end

  let summary = ask scout: """
    Summarise these reviews as a short risk list:
    {reviews}
    """
  give summary
end
