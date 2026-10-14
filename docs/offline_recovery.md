# Recover a stopped grading run

Use saved model responses to recover a preliminary report without API calls:

```powershell
python -m ai_grader recover-grading ..\projects\mspe_49600_fa26_pr01 --response-pattern "Student_*_20261014_*.json"
```

The pattern is restricted to filenames inside the project's
`grader/debug_responses` folder. It must select exactly one response per
manifest student. Recovery preserves the saved files and existing runs and
creates the next run version with the approved specification snapshot.

Valid responses receive locally calculated preliminary scores. A response that
fails schema or scoring validation receives blank scores, an
`invalid_model_response` status, its validation error, and the original parsed
model response in `grading_results.json`. It does not become a zero, full-credit,
or automatically corrected grade. A run containing an invalid response cannot
be finalized.

Token usage describes the original saved API calls. Recovery makes no new calls.
New model-response saves record approved-specification and submission
fingerprints. Recovery rejects fingerprint mismatches. Older saves do not have
these fingerprints, so they are flagged for instructor verification that the
inputs were unchanged. Recovery still validates them against the current
approved specification and the anonymized manifest.

To resolve an invalid response, review its cited evidence and rule conditions.
Keep the original debug response. Create a separate reviewed response set with
one file per student, copying unchanged responses and explicitly correcting the
invalid response's deductions, justification, evidence, task feedback, and
criterion arithmetic as appropriate. Then recover that set with its own narrow
filename pattern. Do not change the approved specification merely to match a
model's invented deduction. Review all feedback and approve final scores only
after validation succeeds.

## Findings for the October 14, 2026 batch

Student_001 through Student_004 validate at preliminary totals of 95, 93, 96,
and 96 out of 100. These remain subject to instructor review.

Student_005's response has two amount mismatches:

| Criterion | Rule | Model deduction | Approved deduction |
| --- | --- | ---: | ---: |
| t2_c2 | t2_c2_name_mismatch | 1 | 1.5 |
| t5_c4 | t5_c4_wrong_claim | 1 | 1.5 |

The semantic application of the rules also requires review:

- Cell 20 assigns the grouped table to `df_grouped` and uses exactly
  `mean_sector_3_time_s`, `std_sector_3_time_s`, and `valid_laps`. It displays
  the table, reports 30 configurations, and exports with `index=False`.
  The model's naming deduction appears unsupported by this evidence.
- Cell 23 reports five of the six requested values and omits standard
  deviation. The model takes two deductions under a rule for one omitted or
  mislabeled metric. Its second rationale is an unstructured presentation,
  rather than identification of a second missing metric. Review that deduction.
- Cell 43 says that 13 degrees would be closest to optimal. The rule requires
  a specific statement of confirmed or global optimality. The model instead
  cites an inconsistent wing-angle claim; the specification lists interpretation
  contradictions as review triggers. Review the distinction and any separately
  omitted discussion points before choosing deductions.

These observations do not assign Student_005 a final grade. The recovered report
withholds that score pending a reviewed response.
