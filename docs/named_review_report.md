# Named instructor review report

Install this update in the ai-grader repository. It adds
`generate-report` to the existing workflow. No project data is included.

Run from the repository root:

```powershell
python -m ai_grader generate-report "..\projects\mspe_49600_fa26_pr01" --run run_v004 --open
```

This creates `instructor_review_v001.csv` in the selected run and opens it with
the default application. Repeated invocations create a new review version.
The report contains student names, proposed scores, task feedback and review
flags. Fill `total_instructor_score` for every student, then save the CSV.
The command prints the exact finalization command to use after your review.

Names are joined locally using student_map.json. No new model calls or grade
approvals occur during report generation. Existing preliminary reports, private
mapping and grading results remain unchanged. A mismatched or incomplete roster
blocks report creation. Invalid results remain unscored and flagged.

If you change the proposed total, the finalizer still requires matching criterion
adjustments. Editing task-score columns alone does not alter criterion scores.

Validation: all 100 unit tests passed, including report generation, approval
loading, roster matching, preserved edits and the Windows open action (mocked).
