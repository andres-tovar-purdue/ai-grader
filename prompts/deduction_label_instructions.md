# Short labels for lost points

For each student you get the deductions from their grading: the task, what was
wrong (written for the instructor) and the feedback for that requirement. Write
one short label per deduction for the student's grade-book comment, where it is
followed by the points, for example: "README has no final recommendation".

Rules:

- One label per deduction, using its index. At most 12 words.
- Name what was missing or wrong, in plain words, as a noun phrase or short
  clause. No advice, no praise.
- Base each label on what was wrong; don't add claims it doesn't support.
- Don't include points, a grade, or any arithmetic; the points are added for you.
- Don't mention file names that start with artifact_, student IDs, AI, a model,
  a grader, a rubric, rules, criteria or their IDs, evidence, confidence,
  review, anonymization, or anything about how grading was done. Refer to
  files by what they are ("the validation figure", "the penalty CSV").
- No parentheses, semicolons, line breaks or quotation marks.

Return labels for every student ID in the input, and no others.
