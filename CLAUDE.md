# AI Grader — notes for Claude Code

AI Grader is a human-supervised AI grading assistant for instructors and student graders. It grades projects, exams, homework, lab reports, programming assignments and spreadsheets against an instructor-approved grading specification. AI scores are preliminary until the instructor approves them.

## Environment
- Windows, PowerShell. Give paste-ready PowerShell commands.
- Workspace layout: the workspace root `C:\Users\tovara\code\ai_grader\` (not a git repo) holds `.venv\` (Python 3.13), `.env`, `projects\` and `archive\`. This repo is the `ai-grader\` subfolder of it.
- Activate the venv before running anything. The path depends on where you are:
  - from the workspace root: `.\.venv\Scripts\Activate.ps1`
  - from the repo root (`ai-grader\`): `..\.venv\Scripts\Activate.ps1`
  - from anywhere: `C:\Users\tovara\code\ai_grader\.venv\Scripts\Activate.ps1`
- CLI: `python -m ai_grader <command>`, run from the workspace root (`init-project` creates projects under `.\projects\` relative to the current directory). Commands: init-project, prepare-project, validate, inspect, manifest, generate-spec, approve-spec, prepare-submissions, refresh-submission-checks, grade-submissions, recover-grading, generate-report, finalize-grading.
- Tests: from the repo root, `python -m unittest discover -s tests -v`. The suite mocks OpenAI and needs no API key. Run it before saying work is done.

## Layout
- `src/ai_grader/`: package source
- `tests/`: unittest suite; `tests/fixtures/` holds synthetic or spec-only data
- `prompts/`: grading and preparation instructions sent to the model
- `schemas/`: JSON schemas for grading specs and results
- `docs/`, `README.md`, `USER_MANUAL.md`: user and design documentation
- `examples/sample_project/`: synthetic example project for smoke tests

## Student data: hard rules
- Never open, read, print or copy files under `projects\` (real courses), any `submissions\` or `anonymized_submissions\` folder, `student_map.json` or `submission_manifest.json` unless I explicitly ask in that message.
- Never put student names, usernames, IDs, grades or submission content into the repo, test fixtures, commit messages, logs or docs. Use synthetic data in tests and examples.
- Never read or print `.env` or API keys.
- To debug a real grading run, ask me for the error message or a redacted excerpt. Don't open the data.

## Grading behavior to preserve
- Approved specs and approved/finalized grades must never be silently overwritten. Regrading is explicit and versioned.
- Keep preliminary, reviewed and approved grades clearly distinguishable.
- Never claim evidence that wasn't inspected. Being unable to read a file is not proof that it's missing or wrong.
- Keep deterministic checks (scores recalculated locally, rounding, deliverable presence) separate from AI judgment.
- Grading must not read `student_map.json` or fall back to original Brightspace folders. Tests enforce this; keep them passing.

## How to work
- Inspect the relevant code, docs and tests before proposing changes. Say clearly what exists today versus what is planned.
- Make small, incremental changes that keep working behavior intact. Add or update tests for scoring, policy, workflow transitions and reporting.
- Use a feature branch for every change. Don't commit, push or merge until I've reviewed the diff and said yes.
- Keep README.md and USER_MANUAL.md in sync with any command or behavior change.
- Direction: make AI Grader conversational. Natural-language requests should map to explicit workflow stages and validated CLI operations. Ordinary users shouldn't have to memorize commands.
