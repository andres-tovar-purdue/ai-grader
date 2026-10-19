# AI Grader User Manual

## 1. Overview

**AI Grader** is a human-supervised grading system for engineering computing projects. It combines deterministic file processing, AI-assisted rubric evaluation, anonymized student submissions, and instructor review.

AI Grader does **not** automatically assign final grades. Important stages require review and approval by the instructor or grader. The human reviewer remains responsible for the grading specification, preliminary grading decisions, final scores, and release of feedback.

The standard workflow is:

```text
Initialize project
    ↓
Inspect project
    ↓
Prepare project
    ↓
Review generated Markdown files
    ↓
Generate grading specification
    ↓
Review and approve specification
    ↓
Prepare and anonymize submissions
    ↓
AI-assisted grading
    ↓
Instructor review
    ↓
Finalize grades and feedback
```

A useful rule is:

> **When in doubt, run `inspect`.**

The `inspect` command reports the current project contents, identifies the workflow stage, tells the user what should be reviewed, and prints the next command to run.

---

## 2. Terminology and Folder Layout

Throughout this manual, the software is called **AI Grader**.

The current GitHub repository is:

```text
https://github.com/andres-tovar-purdue/ai-grader
```

The Python command-line package is invoked with:

```powershell
python -m ai_grader
```

A recommended Windows workspace is:

```text
C:\Users\<username>\code\ai_grader\
├── .venv\
├── ai-grader\
├── projects\
└── .env
```

The repository contains the AI Grader software. The `projects` folder contains local course and student data and should remain outside the Git repository.

---

## 3. Requirements

AI Grader currently requires:

- Windows with PowerShell for the workflow shown in this manual
- Visual Studio Code
- Git
- Python 3.10 or newer
- an OpenAI API key

The Python package requires Python 3.10 or newer.

---

# Part I — First-Time Installation

## 4. Create the AI Grader Workspace

Create a local workspace. For example:

```text
C:\Users\<username>\code\ai_grader
```

Open Visual Studio Code and select:

```text
File → Open Folder...
```

Open the new `ai_grader` folder.

Then open the integrated terminal:

```text
Terminal → New Terminal
```

The terminal should start in the workspace, for example:

```text
PS C:\Users\<username>\code\ai_grader>
```

---

## 5. Clone the Repository

From the AI Grader workspace, run:

```powershell
git clone https://github.com/andres-tovar-purdue/ai-grader.git
```

The workspace should now contain:

```text
ai_grader\
└── ai-grader\
```

---

## 6. Create the Local Projects Folder

From the AI Grader workspace:

```powershell
mkdir projects
```

The workspace should now look like:

```text
ai_grader\
├── ai-grader\
└── projects\
```

Student submissions and course-specific grading projects will be stored under `projects\`, not inside the Git repository.

---

## 7. Create the Python Virtual Environment

Create the virtual environment at the **AI Grader workspace root**:

```powershell
python -m venv .venv
```

Activate it:

```powershell
.\.venv\Scripts\Activate.ps1
```

The terminal prompt should now begin with `(.venv)`, for example:

```text
(.venv) PS C:\Users\<username>\code\ai_grader>
```

The workspace should now look like:

```text
ai_grader\
├── .venv\
├── ai-grader\
└── projects\
```

---

## 8. Install AI Grader

With the virtual environment active, run:

```powershell
python -m pip install --upgrade pip
python -m pip install -e .\ai-grader
```

The `-e` option installs the local repository in editable mode. Changes made to the local AI Grader source code are therefore available immediately without reinstalling the package unless the dependencies change.

Verify the installation:

```powershell
python -m ai_grader --help
```

The command should display the available AI Grader commands.

---

## 9. Configure the `.env` File

Create a file named:

```text
.env
```

in the AI Grader workspace root:

```text
C:\Users\<username>\code\ai_grader\.env
```

Add:

```text
OPENAI_API_KEY=your_api_key_here
OPENAI_MODEL=gpt-5.4-mini
GRADER_INSTRUCTOR_NAME=Full Name
```

`GRADER_INSTRUCTOR_NAME` should be the person who actually reviews and approves the grading specification.

Do **not** commit `.env` to Git or share the API key.

To verify the configuration without printing the API key:

```powershell
python -c "from dotenv import load_dotenv; import os; load_dotenv(); print(os.getenv('OPENAI_MODEL')); print(os.getenv('GRADER_INSTRUCTOR_NAME'))"
```

The setup is now complete.

---

# Part II — Starting a New Grading Project

## 10. Initialize a Project

Run AI Grader from the workspace root:

```text
(.venv) PS C:\Users\<username>\code\ai_grader>
```

Create a project using a short project identifier:

```powershell
python -m ai_grader init-project PROJECT_ID
```

For example:

```powershell
python -m ai_grader init-project mspe_49600_fa26_pr01
```

AI Grader creates:

```text
projects\
└── PROJECT_ID\
    ├── project\
    ├── datasets\
    ├── rubric\
    ├── reference\
    ├── submissions\
    └── grader\
```

The command also prints the next recommended step.

---

## 11. Add the Project Inputs

Before grading begins, the user supplies files to three folders.

### `project\`

Place the **official assignment instructions** here.

Supported project-preparation sources include PDF, Markdown, and plain-text assignment files.

Example:

```text
project\
└── Project_01_Intro_to_Modeling.pdf
```

The original published assignment is the primary authority for student requirements.

### `datasets\`

Place instructor-provided datasets here.

Example:

```text
datasets\
└── ims_setup_raw.csv
```

For project preparation, smaller CSV files can be supplied to the model in full; large CSV files may be represented by a labeled sample.

### `submissions\`

Place the extracted Brightspace student submission folders here.

The expected folder format is the format produced by Brightspace downloads, for example:

```text
100001-2000001 - astudent Avery Student - Jul 30, 2026 1053 PM
```

Do not rename the Brightspace folders before running submission preparation.

---

## 12. Inspect the Project

After copying the inputs, run:

```powershell
python -m ai_grader inspect .\projects\PROJECT_ID
```

For example:

```powershell
python -m ai_grader inspect .\projects\mspe_49600_fa26_pr01
```

`inspect` lists the files in the project folders and determines the current workflow stage.

At the end of the output, AI Grader prints the current workflow status, what the user should verify or review, and the next command to run.

This command can be used at any time to determine where a project is in the grading workflow.

---

# Part III — Preparing the Grading Materials

## 13. Prepare the Project

Run:

```powershell
python -m ai_grader prepare-project .\projects\PROJECT_ID
```

This step uses the AI model to read the assignment instructions and instructor-provided dataset information and creates three instructor-reviewable Markdown files:

```text
project\project_instructions.md
rubric\instructor_rubric.md
reference\reference_solution.md
```

`prepare-project` does not grade student submissions.

AI Grader refuses to silently overwrite these preparation files if they already exist.

---

## 14. Review the Generated Markdown Files

This is an intentional human checkpoint.

Open the three files in Visual Studio Code and review them in this order.

### `project\project_instructions.md`

Confirm that AI Grader interpreted the published assignment correctly. Required tasks, files, filenames, methods, and software should match the original assignment, and no new requirements should be introduced.

### `rubric\instructor_rubric.md`

This is the most important grading-policy review. Confirm that task point totals match the published assignment, criterion allocations are reasonable, deductions are proportional, acceptable alternatives are not unfairly excluded, and the total available points match the assignment total.

The instructor or grader may edit this Markdown file directly.

### `reference\reference_solution.md`

Confirm that the expected calculations, outputs, figures, and interpretations are technically correct.

The reference solution represents **one valid approach**. It should not imply that only one coding implementation can receive credit unless the assignment explicitly requires that method.

---

## 15. Generate the Grading Specification

After reviewing and, if necessary, editing the Markdown files, run:

```powershell
python -m ai_grader generate-spec .\projects\PROJECT_ID
```

AI Grader creates:

```text
grader\grading_spec_v001.json
```

The original published assignment is used as the primary authority when generating the grading specification.

The generated specification includes project information, sources, deliverables, scored tasks, criteria, deduction rules, evidence requirements, review triggers, and any grading ambiguities that require human judgment.

AI Grader also checks the scoring structure before writing the specification. Criterion points must sum to their task maxima, and task maxima must sum to the project total. A separately scored Deliverables section must be represented in the scored task total.

---

## 16. Review the Grading Specification

Open:

```text
projects\PROJECT_ID\grader\grading_spec_v001.json
```

Confirm that the project total is correct, task totals match the assignment, criterion totals match each task, separately scored deliverables are represented correctly, required methods are enforced only when the assignment requires them, permitted student choices remain permitted, deduction rules are reasonable, and there are no unresolved grading decisions that still require instructor judgment.

The grading specification remains a draft until it is explicitly approved.

---

## 17. Approve the Grading Specification

Run the command printed by AI Grader, or:

```powershell
python -m ai_grader approve-spec ".\projects\PROJECT_ID\grader\grading_spec_v001.json"
```

The approval is recorded using `GRADER_INSTRUCTOR_NAME` from the `.env` file.

If unresolved grading ambiguities remain, AI Grader blocks normal approval and prints the unresolved decisions rather than silently proceeding.

Review or resolve those decisions before approving.

If the instructor intentionally accepts the remaining ambiguities, the specification can be approved explicitly with:

```powershell
python -m ai_grader approve-spec ".\projects\PROJECT_ID\grader\grading_spec_v001.json" --allow-unresolved
```

Use `--allow-unresolved` only after consciously reviewing the remaining issues.

After approval, the grading specification status becomes `approved`.

---

# Part IV — Preparing Student Submissions

## 18. Prepare and Anonymize Submissions

After the grading specification is approved, run:

```powershell
python -m ai_grader prepare-submissions .\projects\PROJECT_ID
```

AI Grader processes the Brightspace submission folders, groups multiple attempts by username, selects the latest parsed attempt, assigns anonymized IDs such as `Student_001`, `Student_002`, and `Student_003`, and creates:

```text
grader\
├── submission_manifest.json
├── student_map.json
└── anonymized_submissions\
    ├── Student_001\
    ├── Student_002\
    └── ...
```

`submission_manifest.json` is the sanitized manifest used for downstream AI grading.

`student_map.json` is the private mapping between anonymous IDs and actual students.

**Do not send `student_map.json` to the AI and do not publish it.**

The `anonymized_submissions\` folders are physical agent-facing copies of the selected student submissions.

Known student usernames and names are redacted from text and filenames. Image pixels are **not** redacted. Screenshots and other submitted images may still contain visible identity information and may require instructor review.

AI Grader validates the anonymized manifest before grading.

---

## 19. Review Submission Preparation

Before grading, review:

```text
grader\submission_manifest.json
grader\anonymized_submissions\
```

Confirm that the expected number of students was found, no Brightspace folders were left unparsed unexpectedly, multiple attempts were handled correctly, anonymized student folders exist, and the files expected for each student are present.

Keep `student_map.json` private.

---

# Part V — AI-Assisted Grading

## 20. Run Preliminary Grading

Run:

```powershell
python -m ai_grader grade-submissions .\projects\PROJECT_ID
```

The grading model defaults to the model configured in `OPENAI_MODEL`. A model can also be selected for one run:

```powershell
python -m ai_grader grade-submissions .\projects\PROJECT_ID --model MODEL_ID
```

AI Grader uses the approved grading specification, sanitized submission manifest, and anonymized student files.

Each student's work is graded independently.

The output is stored in a versioned grading-run folder:

```text
grader\grading_runs\run_v###\
```

Typical outputs include:

```text
grading_results.json
preliminary_grading_report.csv
```

`grading_results.json` contains structured criterion-level evidence, preliminary scores, deductions, review flags, and feedback.

`preliminary_grading_report.csv` is the instructor-review worksheet.

The preliminary report does **not** assign the final instructor score automatically.

---

## 21. Rounding Policy

New grading runs use the default policy:

```text
generous-v1
```

Under this policy, criterion scores remain exact, each task subtotal is rounded upward to the next 0.5 point without exceeding the task maximum, and the summed task score is rounded upward to the next whole point without exceeding the assignment maximum.

To disable this rounding for a grading run:

```powershell
python -m ai_grader grade-submissions .\projects\PROJECT_ID --rounding-policy exact-v1
```

The selected rounding policy is recorded with the grading run.

---

## 22. Review Preliminary Grades

This is another required human checkpoint.

Review:

```text
grader\grading_runs\run_v###\preliminary_grading_report.csv
grader\grading_runs\run_v###\grading_results.json
```

For each student, confirm that the AI used appropriate evidence, deductions correspond to the approved grading specification, missing evidence was handled appropriately, technically valid alternatives were accepted, repeated consequences of the same underlying error were not unfairly double-counted, review flags were investigated, and the proposed feedback is appropriate.

AI-generated grades remain preliminary until the instructor or grader approves them.

---

# Part VI — Finalizing Grades

## 23. Approve Final Scores

Before finalization, determine the instructor-approved score for every anonymous student ID.

The final score must agree with the task scores after any criterion overrides.

A finalization command requires one `--score` entry for each student.

Example:

```powershell
python -m ai_grader finalize-grading ".\projects\PROJECT_ID" `
    --run run_v001 `
    --score Student_001=94 `
    --score Student_002=88.5 `
    --score Student_003=97
```

PowerShell allows the backtick character shown above to continue a command across multiple lines. The command may also be entered on one line.

If a criterion-level score needs to be changed, use:

```text
--criterion-score Student_###:CRITERION_ID=POINTS
```

For example:

```powershell
python -m ai_grader finalize-grading ".\projects\PROJECT_ID" `
    --run run_v001 `
    --score Student_001=94 `
    --criterion-score Student_001:t3_c2=4
```

If criterion overrides change the calculated task totals, the approved final score must be adjusted so that the final arithmetic remains consistent.

---

## 24. Finalization Outputs

Finalization creates a versioned folder under:

```text
grader\finalizations\run_v###\finalization_v###\
```

The finalization contains an instructor summary CSV, one student-facing feedback TXT file per student, and offline validation of name mapping, score arithmetic, and feedback consistency.

The anonymous grading run is not overwritten.

Review the final instructor summary and student feedback before releasing grades.

AI Grader does **not** currently post grades to Brightspace automatically. The instructor or grader verifies the outputs and enters the grades manually.

---

# Part VII — Returning to an Existing Project

## 25. Start a New Work Session

Open the AI Grader workspace in Visual Studio Code:

```text
C:\Users\<username>\code\ai_grader
```

Open a terminal and activate the environment:

```powershell
.\.venv\Scripts\Activate.ps1
```

Then inspect the project:

```powershell
python -m ai_grader inspect .\projects\PROJECT_ID
```

AI Grader should identify the current workflow stage and print the next command.

This is the preferred way to resume an unfinished grading project.

---

## 26. Update the AI Grader Software

If the shared repository has been updated, update the local software copy before starting a new grading session.

From the repository:

```powershell
cd .\ai-grader
git pull
cd ..
```

Because AI Grader is installed in editable mode, normal source-code updates do not require another `pip install -e` unless the package dependencies change.

Do not use `git pull` blindly if you have made uncommitted source-code changes.

---

# Part VIII — Command Reference

## 27. Main Commands

```text
python -m ai_grader --help
```

Display the available commands.

```text
python -m ai_grader init-project PROJECT_ID
```

Create a new local grading project.

```text
python -m ai_grader inspect PROJECT_PATH
```

Inspect the project and determine the current workflow stage.

```text
python -m ai_grader manifest PROJECT_PATH
```

Write a machine-readable inventory of the project files to `grader\project_manifest.json`.

```text
python -m ai_grader prepare-project PROJECT_PATH
```

Generate instructor-reviewable project instructions, rubric, and reference solution.

```text
python -m ai_grader generate-spec PROJECT_PATH
```

Generate the draft grading specification.

```text
python -m ai_grader validate SPEC_PATH
```

Validate the grading specification against the local schema.

```text
python -m ai_grader approve-spec SPEC_PATH
```

Approve an instructor-reviewed grading specification.

```text
python -m ai_grader prepare-submissions PROJECT_PATH
```

Parse, select, anonymize, and inventory student submissions.

```text
python -m ai_grader refresh-submission-checks PROJECT_PATH
```

Re-check original filenames against the approved specification without replacing the anonymized submissions.

```text
python -m ai_grader grade-submissions PROJECT_PATH
```

Generate a preliminary AI-assisted grading run.

```text
python -m ai_grader recover-grading PROJECT_PATH --response-pattern PATTERN
```

Rebuild a grading run offline from saved model responses in `grader\debug_responses\`, with no new API calls. Responses that fail validation remain unscored.

```text
python -m ai_grader generate-report PROJECT_PATH --run RUN_ID --open
```

Create a named instructor review CSV with blank approval fields. `--run` defaults to the latest grading run.

```text
python -m ai_grader finalize-grading PROJECT_PATH --run RUN_ID --score Student_###=POINTS
```

Create final instructor and student-facing reports from instructor-approved scores.

---

# Part IX — Troubleshooting

## 28. The Command `python -m ai_grader` Does Not Work

Confirm that the terminal prompt begins with `(.venv)`, AI Grader was installed with `python -m pip install -e .\ai-grader`, and the terminal is running from the AI Grader workspace.

Then try:

```powershell
python -m ai_grader --help
```

---

## 29. The API Configuration Is Not Found

Confirm that `.env` is located at:

```text
C:\Users\<username>\code\ai_grader\.env
```

and contains:

```text
OPENAI_API_KEY=...
OPENAI_MODEL=...
GRADER_INSTRUCTOR_NAME=...
```

Verify the non-secret values with:

```powershell
python -c "from dotenv import load_dotenv; import os; load_dotenv(); print(os.getenv('OPENAI_MODEL')); print(os.getenv('GRADER_INSTRUCTOR_NAME'))"
```

Never print the API key into screenshots, logs, assignments, or repository files.

---

## 30. `prepare-project` Refuses to Overwrite Files

This behavior is intentional.

AI Grader preserves the instructor-reviewed preparation files rather than silently replacing them.

If the files already exist, review and edit them directly:

```text
project\project_instructions.md
rubric\instructor_rubric.md
reference\reference_solution.md
```

Only remove them and regenerate if the instructor intentionally wants to restart the preparation stage.

---

## 31. `approve-spec` Reports Unresolved Ambiguities

Approval is blocked when the grading specification contains unresolved grading decisions.

Review:

```text
grader\grading_spec_v001.json
```

Resolve the grading issue, update the specification or its reviewed source material as appropriate, and then approve again.

Use `--allow-unresolved` only when the instructor intentionally accepts the remaining unresolved issues.

---

## 32. `prepare-submissions` Refuses to Overwrite Anonymized Submissions

This is a safety feature.

AI Grader does not silently replace an existing anonymized submission tree.

Before rebuilding anonymized submissions, confirm that the earlier generated artifacts are no longer needed. Archive or remove the generated submission-preparation artifacts intentionally, then rerun the command.

Do not delete the original Brightspace submissions unless they have been backed up elsewhere.

---

## 33. A PDF Contains No Extractable Text

AI Grader's PDF processing relies on extractable embedded text.

A scanned image-only PDF may require OCR before AI Grader can use it as project instructions.

---

## 34. The Number of Students Is Incorrect

Review:

```text
grader\submission_manifest.json
```

Check for unparsed Brightspace folders, malformed folder names, multiple attempts, missing extracted submission folders, or unexpected files placed directly under `submissions\`.

AI Grader records unparsed submission-folder counts for instructor review.

---

# Part X — Privacy and Data Handling

## 35. Keep Student Data Out of the Git Repository

The recommended workspace intentionally places:

```text
projects\
```

outside:

```text
ai-grader\
```

Do not copy course projects, student submissions, grading reports, `student_map.json`, or `.env` into the public software repository.

---

## 36. Protect `student_map.json`

`student_map.json` contains the mapping between anonymized IDs and student identities.

It is required later for finalization, but it is not intended for AI grading.

Keep it local and private.

---

## 37. Image-Anonymization Limitation

AI Grader redacts known names and usernames from text and filenames during submission preparation.

It does **not** redact image pixels.

A screenshot, figure, or image may therefore still show a student's name, username, or other identifying information. Suspected visible identity should trigger instructor review.

---

# Part XI — Current Scope

AI Grader currently supports a human-supervised local grading workflow:

```text
assignment materials
    ↓
instructor-reviewed grading specification
    ↓
anonymized submissions
    ↓
AI-assisted preliminary grading
    ↓
instructor-approved finalization
```

Brightspace grade entry remains manual.

The software should be treated as a grading assistant, not as an autonomous decision maker. The instructor or grader is responsible for verifying the grading basis, reviewing evidence, approving final scores, and releasing student feedback.
