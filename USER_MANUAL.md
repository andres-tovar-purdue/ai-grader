# AI Grader User Manual

## Set up on a new computer

You need Git and Python 3.10 or newer. In PowerShell, from the folder that should hold AI Grader (for example `C:\Users\<username>\code\ai_grader`):

```powershell
git clone https://github.com/andres-tovar-purdue/ai-grader.git
cd ai-grader
python install.py
```

`install.py` creates the virtual environment, installs AI Grader, creates the `projects\` folder next to the repository, asks for your OpenAI API key and model to create `.env`, and runs `check-setup` (including one test API request). It is safe to rerun and never overwrites an existing `.env`. If a check fails, fix it (for example, the key in `ai-grader\.env`) and run `python install.py` again. When everything passes, it prints the command to activate the environment and the next step. Part I explains each step.

---

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

The **AI Grader workspace** is the folder that holds the repository and the `projects` folder side by side, for example:

```text
C:\Users\<username>\code\ai_grader\      the workspace
├── ai-grader\                           the Git repository
│   ├── .venv\                           Python virtual environment (not in Git)
│   ├── .env                             API key and settings (not in Git)
│   └── .env.example                     template for .env
└── projects\                            grading projects and student data
```

The repository contains the AI Grader software, its virtual environment and its `.env` settings file; Git ignores `.venv\` and `.env`. The `projects` folder contains local course and student data and stays outside the repository. Run AI Grader commands from the workspace.

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

## 4. Choose the AI Grader Workspace

Create a folder for AI Grader, for example:

```text
C:\Users\<username>\code\ai_grader
```

Open it in Visual Studio Code (**File → Open Folder...**), then open a terminal (**Terminal → New Terminal**). The terminal should start in the workspace:

```text
PS C:\Users\<username>\code\ai_grader>
```

---

## 5. Clone the Repository

From the workspace, run:

```powershell
git clone https://github.com/andres-tovar-purdue/ai-grader.git
cd ai-grader
```

---

## 6. Run the Setup Script

From the repository folder (`ai-grader\`), run:

```powershell
python install.py
```

Use any Python 3.10 or newer; no virtual environment is needed yet. The script:

1. checks the Python version;
2. creates the virtual environment `ai-grader\.venv\` if it is missing;
3. installs AI Grader into it with `pip install -e .`;
4. asks before creating the `projects\` folder next to the repository;
5. creates `ai-grader\.env` from `.env.example` if `.env` is missing, asking for:
   - `OPENAI_API_KEY` (not shown while you type or paste it);
   - `OPENAI_MODEL`, for example `gpt-5.4-mini` (required);
   - `GRADER_INSTRUCTOR_NAME`, the person who reviews and approves grading specifications (optional now; `approve-spec` needs it);
6. runs `python -m ai_grader check-setup` from the workspace with the new environment (Section 8), which includes one small test request to the OpenAI API;
7. if every check passes, prints the command to activate the environment and the next step. If a check fails, it says where `.env` is; fix the problem (for example, paste the key into `.env` yourself) and run `python install.py` again. The rerun keeps everything already set up and repeats the check.

It is safe to rerun at any time. It keeps an existing virtual environment and never overwrites or reads an existing `.env`.

The workspace should now look like:

```text
ai_grader\
├── ai-grader\
│   ├── .venv\
│   └── .env
└── projects\
```

---

## 7. Activate the Virtual Environment

Return to the workspace and activate the environment:

```powershell
cd ..
.\ai-grader\.venv\Scripts\Activate.ps1
```

The prompt should now begin with `(.venv)`:

```text
(.venv) PS C:\Users\<username>\code\ai_grader>
```

Activate it this way at the start of every work session. `install.py` cannot do this for you: a script cannot change the environment of the terminal that started it.

---

## 8. Check the Setup

`install.py` already ran this check. Run it again at any time (for example after editing `.env`) with the environment active, from the workspace:

```powershell
python -m ai_grader check-setup
```

It reports whether the virtual environment is active, `.env` exists, `OPENAI_API_KEY` is set (the key is never shown), `OPENAI_MODEL` is set, the `projects\` folder exists, and the API is reachable. Add `--skip-api` to skip the test request. Any problem is listed with what to do.

The setup is now complete.

---

## 9. Change the `.env` Settings

`install.py` creates `.env` only once. To change a setting later, edit `ai-grader\.env` in a text editor:

```text
OPENAI_API_KEY=your_api_key_here
OPENAI_MODEL=gpt-5.4-mini
GRADER_INSTRUCTOR_NAME=Full Name
```

To start over, delete `.env` and rerun `python install.py`. `.env.example` also lists Anthropic settings; these are planned and not yet supported.

Do **not** commit `.env` to Git or share the API key.

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

If you run the command from inside `projects\` or from inside the project folder itself, AI Grader uses that folder instead of creating `projects\projects\PROJECT_ID`, and says which folder it used. You can also pass the name as a path such as `projects\PROJECT_ID`. If a folder named `PROJECT_ID` sits in the current folder but not in `projects\`, the command stops without changing anything and asks you to move it into `projects\` first.

If the project folder would be inside a Git repository (for example, because you ran the command from `ai-grader\`), the command stops without changing anything and asks you to run it from the workspace folder. This keeps student data out of Git. The repository's `.gitignore` also ignores `projects\` as a second safeguard.

### Starting from a Brightspace download

You can also create `projects\PROJECT_ID\` yourself, drop the Brightspace download into it, and run the same command. The download can be:

- the Brightspace ZIP itself;
- its extracted submission folders, placed directly in the project folder; or
- the folder that Windows "Extract All" creates next to the ZIP.

AI Grader then completes the existing folder:

- creates any missing standard folders and leaves existing ones untouched;
- extracts the Brightspace ZIP and keeps the ZIP where it is;
- moves the Brightspace submission folders (names that begin `<digits>-<digits> - `) and Brightspace's `index.html` into `submissions\`, and removes the Windows extraction folder once it is empty;
- leaves every other file where it is and lists it as "not moved".

A ZIP counts as a Brightspace download when it contains `index.html` or submission folders at its top level. Other ZIP files, such as a zipped dataset, are left alone. Files in the Brightspace ZIP that are neither submission folders nor `index.html` are not extracted; AI Grader reports how many there are.

Preview the changes first with `--dry-run`:

```powershell
python -m ai_grader init-project mspe_49600_fa26_pr01 --dry-run
```

Example output:

```text
Dry run: nothing was changed.

Existing project folder: C:\Users\<username>\code\ai_grader\projects\mspe_49600_fa26_pr01

Would create folders:
  - project\
  - datasets\
  - rubric\
  - reference\
  - submissions\
  - grader\

Would extract from Brightspace ZIP into submissions\: 5 submission folders, 1 index.html
  - Project 1 Download Oct 18, 2026.zip (kept)
```

Then run the command again without `--dry-run` to apply the changes.

AI Grader reports counts only and never prints student names. Everything runs locally: AI Grader reads the Brightspace ZIP to extract it, and when the same submission exists in two places it compares their contents locally. Nothing is sent to the AI model at this stage.

Running the command again is safe. Submissions already in `submissions\` with the same file names and identical contents (compared by SHA-256 checksum) are reported as "Already in submissions\ (skipped)", so a kept ZIP is not extracted twice. If Windows briefly locks a folder (for example, while antivirus scans it or Explorer has it open), AI Grader retries the move for about 2 seconds. If the folder is still locked, it stops and reports how many items it placed; close the program using the folder and run the command again. If an earlier run was interrupted, running it again finishes the remaining items.

Nothing is overwritten. If `submissions\` already holds a different folder or `index.html` with the same name, or if the ZIP and an extracted copy of the same submission differ, the command stops before changing anything and reports how many items clash. Compare them with `submissions\`, remove or rename the duplicates, then run the command again. The command also stops without changes if the ZIP is password-protected, contains unsafe file paths, or needs more disk space than is free.

Files whose names follow the Brightspace pattern but are not folders are left in place and counted rather than listed by name.

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

Place the extracted Brightspace student submission folders here. If you put the Brightspace ZIP or its extracted folders in the project folder itself, `init-project` can extract or move them here for you (see Section 10).

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

If the model copies JSON Schema keywords (`$schema`, `$id`, `$comment`) from the schema into the top level of its answer, AI Grader removes them before checking.

If the generated specification still fails the checks, no draft is written. AI Grader lists the problems and saves the model's raw response, so the paid response is not lost:

```text
grader\invalid_spec_response_<timestamp>.json
```

Rerun `generate-spec` to try again.

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

AI Grader processes the Brightspace submission folders, groups multiple attempts by username, merges each student's attempts (see below), assigns anonymized IDs such as `Student_001`, `Student_002`, and `Student_003`, and creates:

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

Known student usernames and names are redacted from text and filenames, including a first name or surname that appears on its own (for example, a notebook named after the student's surname). Each part of the name is matched as a whole word, ignoring case: digits, underscores, spaces and punctuation count as word boundaries, but letters do not, so a surname such as Chen is redacted in `chen_prj1.ipynb` but not inside `kitchen`. Parts with fewer than two letters, such as a middle initial, are not redacted on their own. A first name that is also an ordinary word (for example, Will or May) is redacted wherever that word appears in the student's files. Image pixels are **not** redacted. Screenshots and other submitted images may still contain visible identity information and may require instructor review.

AI Grader validates the anonymized manifest before grading.

### PDF reports

AI Grader extracts the text of each submitted PDF on this computer (with pypdf, no upload) so the grader can read the report: results tables, discussion, limitations, links. The text is redacted like other student text and saved next to the anonymized PDF as `artifact_###_<name>.pdf.txt`.

- Figures, images, tables drawn as graphics, and page layout are **not** extracted, and the order of text may differ from the page. The grader is told this, and flags criteria that depend on a figure for review instead of penalizing them.
- Up to 50,000 characters of text are kept per PDF. A longer PDF is cut there; the grader is told, and the student is flagged so you can review the rest.
- A PDF with no extractable text (for example a scanned PDF) or a damaged PDF is presence-only, as before, and the student is flagged: "No text could be extracted from ...; verify the PDF manually."

### Unreadable notebooks

A notebook that is not valid JSON, for example a damaged file, no longer stops grading. Notebooks saved with a UTF-8 or UTF-16 byte-order mark are read correctly. If a notebook still cannot be read, the grader is told so, the student is flagged ("could not be read as a notebook; ... Review it manually."), and the rest of the batch continues.

### Students with several attempts

Brightspace creates one folder per attempt. AI Grader builds each student's submission from **all** of their attempts: every file from every attempt is included, and when the same file (the same relative path, ignoring case) appears in more than one attempt, the copy from the newest attempt is used. A student whose last attempt re-uploaded only the PDF therefore keeps the notebook, script and data files from earlier attempts.

- The newest attempt's time is the submission time, including for the GitHub repository snapshot.
- `student_map.json` (private) records, for each anonymized file, its original path, the attempt and Brightspace folder it came from, and which older attempts it replaced.
- `submission_manifest.json` records only counts: the number of attempts and how many files were replaced by a newer attempt. The student is flagged for review.
- `prepare-submissions` prints the same counts per student, by anonymous ID only, for example `Student_003: 3 attempts merged, 2 file(s) replaced by a newer attempt`.
- If an attempt's time cannot be read from its folder name, that attempt is treated as the oldest, and the student is flagged.

You can rerun `prepare-submissions` until grading starts, for example after adding a late submission. It builds and validates the new output first, then replaces the earlier `anonymized_submissions\`, `submission_manifest.json`, `student_map.json` and repository evidence. Once grading has started (anything in `grader\grading_runs\` or `grader\debug_responses\`), it refuses and changes nothing, because those files refer to the current `Student_###` numbers.

### GitHub repositories linked in submissions

If students submit a link to a GitHub repository, for example in the Brightspace submission text, `prepare-submissions` also collects the repository as evidence. Links come from Brightspace's `submissions\index.html`. For a student with no repository link there, AI Grader also uses GitHub repository links found in that student's PDF or notebook text (read before redaction). Without any GitHub links, nothing changes.

A link taken from a PDF or notebook is labeled as such in the facts file, and the student is flagged for review: a report may cite someone else's repository, such as a library it used.

The owners of a student's GitHub links, whether from `index.html` or from their PDFs and notebooks, are redacted from all of that student's files: as `<repo_owner>` in text and extracted PDF text, and as `owner` in file names. Owners and URLs are kept only in the private `grader\repository_map.json` and `student_map.json`.

Links are recognized with or without `https://` or `www.` (for example `github.com/owner/repo`), with a `.git` ending or a path such as `/tree/main`, and in the SSH form `git@github.com:owner/repo.git`.

For each student, AI Grader:

1. matches the entries in `index.html` to students through the private student map. An entry that names no student, or more than one, is skipped and counted;
2. clones each linked repository **without credentials**: no saved Git credentials, no SSH keys, no Git configuration from your account, and no login prompts. Only public repositories can be fetched;
3. grades the repository **as it was at submission time**: the last commit at or before the student's Brightspace submission time (taken from the submission folder name, read in this computer's time zone, up to the end of that minute). Its tracked files are copied into the student's folder under `github_repo\`, with the same name redaction as other submission files plus the repository owner. Files inside tracked environment folders such as `.venv` are not copied, and very large files are skipped. If the submission time is unknown, the latest commit is used, and the facts file says so;
4. keeps the repository's **current state** for you, with the same redaction, in `grader\repository_current\Student_###\` when it differs from the graded commit. It is not graded;
5. writes a facts file for the student, for example `artifact_005_github_repo_facts.json`, with objective information:
   - whether the repository was publicly reachable: `cloned`, `public_but_empty` (reachable, but no commits), `not_public_or_not_found`, `network_error` or `timeout`;
   - the repository name (redacted) and the submission time used;
   - the evaluated commit (date and message), its tracked files, and whether `.venv` or other environment folders are tracked and how many files they contain;
   - the total number of commits, the commits up to submission, and **the commits after submission, listed separately**;
   - a summary of the current state (latest commit, tracked files, environment folders);
   - for students without a recognized repository link, the reason (see below).

The facts file and the graded repository files are passed to grading as evidence, like any other submission file. Whether a fact affects the grade is decided by the approved grading specification, not by AI Grader.

**Students without a recognized repository link.** `prepare-submissions` lists them by anonymous ID with a reason, without names or URLs:

| Reason | Meaning |
|---|---|
| GitHub profile link without a repository | The link points to a GitHub account, not a repository. |
| mentions github.com but no owner/repository link | The text mentions GitHub, but no repository address could be read. |
| links only to sites other than GitHub | For example a GitLab or cloud-drive link. |
| no link in the submission text | The submission has no link at all. |
| no entry in index.html | The student has no submission text in `index.html`. |
| no GitHub link in index.html or in the PDF or notebook text | `index.html` has no GitHub links, and the student's PDFs and notebooks have none either. |

**Possible repositories for instructor review.** If a linked repository cannot be fetched, or a student linked only their GitHub profile, AI Grader looks up the owner's public repositories with an anonymous request to GitHub (no account or token) and lists those whose names match the project. A name matches when it is the linked repository's name, the project folder's name or the specification's project ID, ignoring underscores, hyphens and capitalization, or when it uses two or more parts of those names in order (for example `MSPE49600-PR02` for `mspe_49600_fa26_pr02`). These candidates are written only to the private `grader\repository_map.json`; the facts file records just how many there are. **Candidates are never fetched or graded.** Check them yourself and decide.

`prepare-submissions` reports counts by status, the students without a recognized link, and how many candidates were found for each student. Repository owners, URLs and candidates are saved in `grader\repository_map.json`, which is private like `student_map.json`.

Limitations:

- Only public GitHub repositories on `github.com` are supported. Private repositories are recorded as not publicly reachable.
- Commit times are those recorded in the repository; they can be set by whoever made the commit. Only the default branch is considered.
- The candidate lookup lists at most the owner's first 100 public repositories, and GitHub limits anonymous lookups to about 60 per hour.
- AI Grader redacts known names, usernames and repository owners. A different GitHub username inside a repository name, file or commit message cannot be detected.
- Fetching needs Git to be installed and an internet connection.

**Text encodings.** Text files saved as UTF-16 with a byte-order mark, as Windows PowerShell's `>` produces, are decoded correctly in both submissions and repositories.

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

The grading model defaults to the model configured in `OPENAI_MODEL`. Like every command that calls the model (`prepare-project`, `generate-spec`, `grade-submissions`, `check-setup`), it first prints the model it will use and where that came from, for example `Model: gpt-5.4-mini (from OPENAI_MODEL)`. A model can also be selected for one run:

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

Every grading command creates a new run folder; earlier runs are never changed. With `--resume`, a saved model response is reused only if it was made with the same grading specification, model and inputs. After you approve a new specification, grading therefore requests new responses and writes a new run, and says so for each student whose saved responses no longer match.

### Set a grading run aside

An incomplete or invalid run can be set aside without deleting it:

```powershell
python -m ai_grader archive-grading .\projects\PROJECT_ID --run run_v001 --reason "incomplete run"
```

The run is moved to `grader\archive\grading_<date>_<time>\`, with an `archive_record.json` that lists what was moved and why. Without `--run`, all runs are moved. Once no run is left in `grader\grading_runs\`, the saved model responses in `grader\debug_responses\` are moved too, and `prepare-submissions` can run again. A run with finalized grades is never moved. New runs keep counting past archived ones, so a run number is never reused.

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
.\ai-grader\.venv\Scripts\Activate.ps1
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

Because AI Grader is installed in editable mode, normal source-code updates do not require reinstalling. If the package dependencies change, rerun `python install.py` from `ai-grader\`; it keeps your `.env`.

Do not use `git pull` blindly if you have made uncommitted source-code changes.

---

# Part VIII — Command Reference

## 27. Main Commands

```text
python -m ai_grader --help
```

Display the available commands.

```text
python -m ai_grader check-setup
```

Check the virtual environment, `.env`, API key (never shown), model, `projects\` folder and API connection. Add `--skip-api` to skip the test request.

```text
python -m ai_grader init-project PROJECT_ID
```

Create a new local grading project, or complete an existing project folder that holds a Brightspace ZIP or its extracted folders. Add `--dry-run` to preview the changes without making them.

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

Parse, select, anonymize, and inventory student submissions. Can be rerun until grading starts; it then replaces the earlier preparation.

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
python -m ai_grader archive-grading PROJECT_PATH [--run RUN_ID] [--reason TEXT]
```

Move grading runs (all of them by default) to `grader\archive\` without deleting them; once none is left, the saved responses go too, so `prepare-submissions` can run again. Runs with finalized grades are never moved.

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

Run `.\ai-grader\.venv\Scripts\python.exe -m ai_grader check-setup` from the AI Grader workspace. Confirm that the terminal prompt begins with `(.venv)` and the terminal is running from the workspace. If AI Grader is not installed, rerun `python install.py` from `ai-grader\`.

Then try:

```powershell
python -m ai_grader --help
```

---

## 29. The API Configuration Is Not Found

Run:

```powershell
python -m ai_grader check-setup
```

`.env` belongs in the repository folder:

```text
C:\Users\<username>\code\ai_grader\ai-grader\.env
```

If `check-setup` reports it at the old location (the workspace root), move it into `ai-grader\`. If it is missing, rerun `python install.py` from `ai-grader\`. It should contain:

```text
OPENAI_API_KEY=...
OPENAI_MODEL=...
GRADER_INSTRUCTOR_NAME=...
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

## 32. `prepare-submissions` Refuses to Replace Prepared Submissions

This is a safety feature.

Before grading, rerunning `prepare-submissions` replaces the earlier preparation. Once grading has started, meaning `grader\grading_runs\` or `grader\debug_responses\` holds anything, it refuses and changes nothing: grading runs and saved model responses refer to the current `Student_###` numbers, and a new preparation could number students differently.

To grade again with the current preparation, run `grade-submissions`. To prepare again anyway, set the runs and saved responses aside first, then rerun the command:

```powershell
python -m ai_grader archive-grading .\projects\PROJECT_ID
```

They are moved to `grader\archive\`, not deleted. Runs with finalized grades are never moved (see section 20).

Do not delete the original Brightspace submissions unless they have been backed up elsewhere.

---

## 33. A PDF Contains No Extractable Text

AI Grader's PDF processing relies on extractable embedded text.

A scanned image-only PDF may require OCR before AI Grader can use it as project instructions. A student's PDF without extractable text is checked for presence only, and the student is flagged for manual review (section 18).

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

Keep it local and private. The same applies to `repository_map.json`, which AI Grader creates when submissions link to GitHub repositories; it lists each student's repository owner and URL, profile links, and candidate repositories for review.

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
