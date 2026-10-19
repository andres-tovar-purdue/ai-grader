# AI Grader — Target Grading Workflow

Agreed design (2026-10-18). The grading agent (Claude Code today; a standalone AI Grader agent later) drives the CLI and states the current stage, what it needs and the next step at every point. Nothing is approved, regraded or released without explicit instructor confirmation.

## Stages

| # | Stage | Agent does | Instructor does | Code today |
|---|---|---|---|---|
| 0 | **Set up** | Instructor drops the Brightspace ZIP (or the extracted folders) into a new project folder and says "initialize". Agent extracts the ZIP, creates the standard folders and moves submissions + index.html into `submissions\`. Reports counts only. | — | `init-project NAME` initializes `projects\NAME\` in place: extracts the Brightspace ZIP (kept), moves extracted folders (including a Windows "Extract All" folder) into `submissions\`, creates missing folders, lists other files as not moved. `--dry-run` previews. Never overwrites; duplicates are compared by SHA-256 and identical ones skipped, so reruns are safe. |
| 1 | **Assignment & policies** | Asks for the assignment PDF, datasets, the instructor's own solution if any, the due date and the late policy. | Provides files and answers. | Manual file placement. **Needs:** due date/late policy capture; lateness computed from Brightspace timestamps. |
| 2 | **Draft rubric** | Drafts `project_instructions.md`, `instructor_rubric.md` and `reference_solution.md`, then summarizes the rubric in plain language (tasks, points, partial-credit rules, open questions). | Reviews and edits the MD files, then says what changed. | `prepare-project` exists. |
| 3 | **Check & lock rubric** | Re-reads the edited MD and double-checks: points sum, every task is covered, no ambiguities, the reference solution matches. Generates the machine-readable spec, shows a plain-language summary (and the diff on later versions) and asks for approval. Instructor never edits JSON. | Approves. | `generate-spec`, `validate`, `approve-spec` exist. **Needs:** plain-language spec summary/diff. |
| 4 | **Calibration round** | Grades a random sample: max(5, ~10%), capped at N, so a 5-student class grades everyone. Seed is recorded; sample is stratified when possible (e.g. include unusual or incomplete submissions). Writes a review CSV with AI scores, per-criterion comments, evidence and **blank instructor columns** (score, agree/disagree, note). | Reviews in Excel and fills the instructor columns. | `grade-submissions` grades all; `generate-report` makes a CSV with blank approval fields. **Needs:** `--sample N --seed`. |
| 5 | **Adjust** | Reads the instructor's columns, measures agreement, finds the patterns behind disagreements and proposes specific rubric/spec edits. A new rubric version is created; earlier runs are kept. Regrades the same sample (to confirm the fix) and optionally a fresh one. | Accepts or edits proposals. Decides: another round, or grade everyone. | Versioned specs and runs exist. **Needs:** agreement summary, change-impact explanation. |
| 6 | **Full batch** | Grades the remaining submissions with the approved version (resumable). Flags items for review: uncertain judgments, missing or unreadable evidence, large deductions, outliers, lateness. | Reviews **flagged** items plus a few random spot checks, not every submission. | Resume/recovery exist; invalid responses flagged. **Needs:** review flags + prioritized review list. |
| 7 | **Finalize** | Applies instructor overrides and writes (a) an internal CSV with every criterion, comment, evidence and override, and (b) Brightspace-ready output: final score + short feedback explaining where points were lost (brief praise if full marks). Versioned; never overwrites. | Approves final grades. | `finalize-grading` writes an instructor summary CSV + per-student TXT feedback. **Needs:** Brightspace import CSV. |
| 8 | **Release** | Prepares the upload. Nothing is published without explicit authorization. | Uploads / releases. | Manual. See below. |

At any point: "Where are we?" / "Continue where we stopped" returns the stage, what's done, what's blocked and the next step (`inspect` already does part of this; needs `--json`).

## Getting grades into Brightspace (options, best first)
1. **Grades CSV import** (Brightspace Grades → Import): scores for all students in one upload, no IT approval. AI Grader writes the file in Brightspace's import format, keyed by username/OrgDefinedId from the private student map. Whether feedback text can go in the same import needs checking.
2. **Feedback:** if the import can't carry comments, the agent produces a per-student list (username → feedback) for quick copy-paste, in Brightspace's grading order.
3. **Brightspace API (Valence):** can write assignment scores and feedback directly, but requires app registration/API access approved by Purdue IT. Worth asking about; don't depend on it this term.

## Privacy across stages
The agent works with Student_### IDs. Names and usernames appear only in locally generated files (named review report, Brightspace import) that the agent creates but does not read back into the conversation.

## Build order (gaps above)
1. ~~Init in place + unzip (stage 0).~~ Done.
2. Calibration sampling `--sample N --seed` (stage 4).
3. Brightspace grades import CSV + copy-paste feedback list (stages 7–8).
4. Review flags and prioritized review list (stage 6).
5. Agreement summary and rubric-change impact (stage 5).
6. Spec plain-language summary/diff (stage 3); `inspect --json` (status).
7. Due date / late policy (stage 1).
Plus, for MSPE 49600: `check-github-repos`. For MSPE 29800: Excel/PDF content extraction.
