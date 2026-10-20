# AI Grader — notes for Claude Code

AI Grader is a human-supervised AI grading assistant for instructors and student graders. It grades projects, exams, homework, lab reports, programming assignments and spreadsheets against an instructor-approved grading specification. AI scores are preliminary until the instructor approves them.

## Environment
- Windows, PowerShell. Give paste-ready PowerShell commands.
- Layout (paths relative to the repo root, so they hold on any machine):
  - `.venv\` and `.env` live inside the repo; git ignores both. `.env.example` is the tracked template.
  - `..\projects\` (real course data) lives in the parent folder, outside the repo. The parent folder is the "workspace"; it is not a git repo.
  - First-run setup: `python install.py` from the repo root creates all three. It is safe to rerun and never overwrites `.env`.
- Activate the venv before running anything:
  - from the repo root: `.\.venv\Scripts\Activate.ps1`
  - from the workspace (`..`): `.\ai-grader\.venv\Scripts\Activate.ps1`
- CLI: `python -m ai_grader <command>`, run from the workspace (`init-project` creates projects under `.\projects\` relative to the current directory, so running it from the repo root would put them inside the repo). Commands: check-setup, init-project, prepare-project, validate, inspect, manifest, generate-spec, approve-spec, prepare-submissions, refresh-submission-checks, grade-submissions, recover-grading, archive-grading, generate-report, finalize-grading.
- Model: `OPENAI_MODEL` from `.env`; the single fallback is `DEFAULT_MODEL` in `ai_client.py` (use `resolve_model()`, don't add new defaults). Commands that call the model print `describe_model()` first.
- Tests: from the repo root, `python -m unittest discover -s tests -v`. The suite mocks OpenAI and needs no API key. Run it before saying work is done.

## Layout
- `install.py`: first-run setup (standard library only; runs before the venv exists)
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
