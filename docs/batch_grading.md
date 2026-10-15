# Grading a large batch

## Instructor calibration

The approved project specification can set `grading_policy.discussion_policy`
to `presence_only` and `grading_policy.filename_policy` to `warning_only`.
The MSPE 49600 Project 1 v003 revision records the instructor's October 14, 2026
instructions using those settings. It awards discussion credit for any relevant
discussion or rationale, without evaluating depth, nuance, or subtopic coverage.
Technical computations, feature counts, genuinely absent files, and required
numeric values remain governed by their approved rules.

Local file-role checks distinguish presence from original naming compliance.
Recognizable naming variations receive credit. Unclassified or conflicting file
roles are ambiguous and require review without an automatic missing-file penalty.
Only confirmed absence can support a `file_presence` deduction. Reminders about
file names remain part of feedback for future submissions.

The five recalibrated preliminary totals are 100, 100, 100, 100, and 98.
Student_005's remaining two points are the absent standard-deviation value in
the Markdown report and the absent standalone correlation PNG. The notebook
heatmap receives technical visualization credit. Final instructor scores have
not been assigned.

## Run and resume

Use the approved specification and prepared anonymized submissions:

```powershell
python -m ai_grader grade-submissions PROJECT_PATH --resume
```

Each model response is saved through an atomic temporary-file replacement
before scoring. Invalid responses or ordinary API request failures become
unscored review rows while the remaining students continue. Authentication and
authorization errors stop the batch to avoid repeating unusable requests.

If the process is interrupted, run the same command again. Saved responses are
reused only when the student ID, model, approved specification, submission
manifest record, complete request content, instructions, and response schema
match. Legacy response saves lack this provenance and are not reused
automatically. Model, policy, prompt, or artifact changes force new calls.

To retry only failed responses while keeping successful ones:

```powershell
python -m ai_grader grade-submissions PROJECT_PATH --resume --retry-failed
```

Each completed invocation writes a new run version. `batch_execution` records
new calls and reused responses. `api_usage` includes the historical usage of
responses used in the report, including cached responses; it is not a count of
new spending. Invalid or failed responses remain unscored and prevent finalization.

## Review and approve a CSV

Generate and open a named review report with one command:

```powershell
python -m ai_grader generate-report PROJECT_PATH --run run_vNNN --open
```

Omit `--run` to select the latest grading run. The command joins student names
from the local private map, preserves the proposed scores and feedback, and
leaves `total_instructor_score` blank. It makes no API calls. The named report
is private instructor material. Repeating the command creates a new version,
preserving earlier review files and the anonymous preliminary report.

Review each submission and fill `total_instructor_score` explicitly for each
student. Save the CSV. The command prints the exact finalization command for
that review file; run it after completing your review:

```powershell
python -m ai_grader finalize-grading PROJECT_PATH --run run_vNNN --scores-file REVIEWED_CSV
```

The CSV must contain `student_id` and `total_instructor_score` columns. Blank,
duplicate, malformed, or nonfinite approvals are rejected. Model scores are never
copied into approval fields automatically. Existing finalization checks still
verify roster alignment, score bounds, criterion arithmetic, and the approved
specification snapshot. If approved totals differ from the criterion totals,
provide the corresponding `--criterion-score` overrides; merely editing task
columns in the review CSV does not override criterion scores.

## Capacity and current evidence limits

The saved five-response batch took approximately 70 seconds from start to last
response, roughly 14 seconds per student. A serial run of 160 comparably sized
submissions projects to about 37 minutes. This is an extrapolation, not a measured
160-submission benchmark; latency, throttling, retries, prompt size, and different
assignments can change the time. The calls used about 48,000 input tokens per
student on average. Cost and time should be measured on a small representative
pilot before a large run.

The tests verify checkpointing, interruption/resumption, invalid-response
isolation, cached-failure retry, cache invalidation, presence checks, and a
160-row approval CSV. They do not establish grading accuracy on 160 real
submissions. Calibrate the next assignment with stronger and weaker submissions,
review exceptions, and spot-check apparently clean results.

Current content inspection covers Python, MATLAB, exported VBA `.bas` source,
notebook source and saved outputs, readable text, CSVs, and supplied images.
Excel workbooks receive presence-only evidence; formulas, sheets, and macros
inside `.xlsm` are not extracted. PDF contents remain uninspected and receive
presence-only evidence. Simulink `.slx` files receive structural package checks,
not block-level or simulation verification. An assignment depending on those
contents needs further extraction and validation before dependable bulk grading.
