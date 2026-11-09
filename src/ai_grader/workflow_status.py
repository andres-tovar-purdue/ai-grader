"""
status: where a grading project stands and the next step, in plain words.

Read-only and local: no model calls, nothing written. Safe to paste into a
chat: it prints counts, versions and file names that AI Grader creates, never
student names, usernames or submission folder names. student_map.json is
only checked for existence; review files are read for two columns
(student_id, total_instructor_score) and reported as counts.
"""

import csv
import json
import re
from pathlib import Path

from ai_grader.brightspace import (
    FEEDBACK_TEXT_FILENAME, FINAL_CSV_FILENAME, GRADES_IMPORT_FILENAME,
)
from ai_grader.project_initialization import PROJECT_SUBFOLDERS
from ai_grader.source_extraction import SUPPORTED_PROJECT_EXTENSIONS
from ai_grader.submission_processing import parse_brightspace_folder_name


DONE, NEXT, TODO = "done", "next", "todo"

STAGES = [
    ("setup", "Set up the project folder"),
    ("files", "Add the assignment files"),
    ("draft", "Draft the rubric"),
    ("spec", "Review the drafts and generate the grading spec"),
    ("approve", "Approve the grading spec"),
    ("anonymize", "Anonymize the submissions"),
    ("grade", "Grade the submissions"),
    ("review", "Review the grades"),
    ("finalize", "Finalize the grades"),
    ("brightspace", "Enter the grades in Brightspace"),
]

DRAFTS = {
    "project instructions": Path("project") / "project_instructions.md",
    "instructor rubric": Path("rubric") / "instructor_rubric.md",
    "reference solution": Path("reference") / "reference_solution.md",
}

RUN_PATTERN = re.compile(r"run_v[0-9]{3}")
FINALIZATION_PATTERN = re.compile(r"finalization_v[0-9]{3}")
REVIEW_PATTERN = re.compile(r"instructor_review_v[0-9]{3}\.csv")
SPEC_PATTERN = re.compile(r"grading_spec_v[0-9]{3}\.json")


class StatusError(Exception):
    """The project to report on could not be found."""


def windows_path(path, cwd):
    """
    The path as the user would type it in PowerShell from cwd: .\\relative
    when it is under cwd, otherwise absolute.
    """

    path, cwd = Path(path).resolve(), Path(cwd).resolve()
    try:
        relative = path.relative_to(cwd)
    except ValueError:
        return str(path)
    return ".\\" + "\\".join(relative.parts) if relative.parts else "."


def counted(number, noun, plural=None):
    """'1 folder', '2 folders'."""
    return f"{number} {noun if number == 1 else plural or noun + 's'}"


def verb(number, singular, plural):
    return singular if number == 1 else plural


def quoted(text, always=False):
    return f'"{text}"' if always or " " in text else text


def is_project_folder(path):
    path = Path(path)
    return path.is_dir() and path.parent.name.lower() == "projects"


def find_projects(projects_path):
    """Project folders directly inside projects\\, sorted by name."""

    projects_path = Path(projects_path)
    if not projects_path.is_dir():
        return []
    return sorted(
        (path for path in projects_path.iterdir()
         if path.is_dir() and not path.name.startswith(".")),
        key=lambda path: path.name.lower(),
    )


def resolve_status_target(project, cwd):
    """
    What status reports on: ("project", path) or ("list", projects folder).

    PROJECT may be a name (projects\\NAME from the workspace, NAME from inside
    projects\\) or a path. Without it: the project folder you are in, else
    every project in projects\\ (or in the current folder if it is projects\\).
    """

    cwd = Path(cwd).resolve()

    if not project:
        if is_project_folder(cwd):
            return "project", cwd
        if cwd.name.lower() == "projects":
            return "list", cwd
        if (cwd / "projects").is_dir():
            return "list", cwd / "projects"
        raise StatusError(
            "No projects folder here. Run status from the AI Grader workspace "
            "(the folder that holds ai-grader\\ and projects\\).")

    given = Path(project)
    candidates = [cwd / given]
    if len(given.parts) == 1:
        candidates = [cwd / "projects" / project, cwd / project]
        if cwd.name == project:
            candidates.append(cwd)
    for candidate in candidates:
        if candidate.is_dir():
            return "project", candidate.resolve()

    projects_path = cwd / "projects" if (cwd / "projects").is_dir() else cwd
    names = [path.name for path in find_projects(projects_path)]
    message = f"Project not found: {project}."
    if names:
        message += " Projects here: " + ", ".join(names) + "."
    raise StatusError(message)


def count_files(folder, suffixes=None, exclude=()):
    if not folder.is_dir():
        return 0
    return sum(
        1 for path in folder.rglob("*")
        if path.is_file()
        and path not in exclude
        and (suffixes is None or path.suffix.lower() in suffixes)
    )


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def latest(paths, pattern):
    matching = sorted(path for path in paths if pattern.fullmatch(path.name))
    return matching[-1] if matching else None


def submissions_summary(submissions_path):
    """Counts only: recognized attempt folders, students, problems."""

    summary = {"attempt_folders": 0, "students": 0, "unrecognized_folders": 0,
               "unreadable_dates": 0, "loose_files": 0, "index_html": False}
    if not submissions_path.is_dir():
        return summary
    usernames = set()
    for entry in submissions_path.iterdir():
        if entry.is_dir():
            parsed = parse_brightspace_folder_name(entry.name)
            if parsed is None:
                summary["unrecognized_folders"] += 1
                continue
            summary["attempt_folders"] += 1
            usernames.add(parsed["username"])
            if parsed["submitted_at"] is None:
                summary["unreadable_dates"] += 1
        elif entry.name.lower() == "index.html":
            summary["index_html"] = True
        else:
            summary["loose_files"] += 1
    summary["students"] = len(usernames)
    return summary


def review_counts(review_path):
    """(students, grades entered) in a review CSV, or None if unreadable."""

    try:
        with review_path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
    except (OSError, UnicodeDecodeError, csv.Error):
        return None
    if rows and "total_instructor_score" not in rows[0]:
        return None
    filled = sum(1 for row in rows if (row.get("total_instructor_score") or "").strip())
    return len(rows), filled


def project_status(project_path, cwd=None):
    """
    The project's workflow stages (done / next / todo, each with a short
    detail), warnings, and the next step with its command. Never contains
    student names or submission folder names.
    """

    project_path = Path(project_path).resolve()
    cwd = Path(cwd or Path.cwd()).resolve()
    if not project_path.is_dir():
        raise StatusError(f"Project folder does not exist: {project_path}")

    name = project_path.name
    shown = quoted(windows_path(project_path, cwd))
    grader = project_path / "grader"
    details = {key: "" for key, _ in STAGES}
    done = {key: False for key, _ in STAGES}
    warnings = []
    next_step = None

    def command(text):
        return f"python -m ai_grader {text}"

    # 1. Set up: standard folders and Brightspace submission folders.
    missing = [folder for folder in PROJECT_SUBFOLDERS
               if not (project_path / folder).is_dir()]
    subs = submissions_summary(project_path / "submissions")
    top_zips = [path for path in project_path.glob("*.zip") if path.is_file()]
    details["setup"] = (
        f"{counted(subs['attempt_folders'], 'submission folder')}, "
        f"{counted(subs['students'], 'student')}"
        + (", index.html" if subs["index_html"] else ", no index.html"))
    done["setup"] = not missing and subs["attempt_folders"] > 0
    if subs["unrecognized_folders"]:
        number = subs["unrecognized_folders"]
        warnings.append(
            f"{counted(number, 'folder')} in submissions\\ {verb(number, 'is', 'are')} "
            "not named like a Brightspace submission and will be skipped. Rename "
            f"{verb(number, 'it', 'each')} to "
            "'999999-999999 - USERNAME First Last - Oct 12, 2026 900 PM' (see "
            "'Late or emailed submissions' in the user manual).")
    if subs["unreadable_dates"]:
        warnings.append(
            f"{counted(subs['unreadable_dates'], 'submission folder')} "
            f"{verb(subs['unreadable_dates'], 'has', 'have')} a date AI Grader can't "
            "read. Write it like 'Oct 12, 2026 900 PM' (month name, no colon).")
    if subs["loose_files"]:
        warnings.append(
            f"{counted(subs['loose_files'], 'file')} "
            f"{verb(subs['loose_files'], 'sits', 'sit')} directly in submissions\\. Only folders "
            "(and index.html) are read; put each student's files in their folder.")
    if not done["setup"]:
        if top_zips or missing:
            next_step = ("Unpack the Brightspace download into the project folders.",
                         command(f"init-project {quoted(name)}"))
        else:
            next_step = (f"Download the submissions from Brightspace and put the ZIP in "
                         f"{shown} itself (not in a subfolder), then:",
                         command(f"init-project {quoted(name)}"))

    # 2. Assignment files.
    drafts = {label: project_path / relative for label, relative in DRAFTS.items()}
    readable = count_files(project_path / "project", SUPPORTED_PROJECT_EXTENSIONS,
                           exclude={drafts["project instructions"]})
    all_files = count_files(project_path / "project",
                            exclude={drafts["project instructions"]})
    ignored = all_files - readable
    datasets = count_files(project_path / "datasets", {".csv"})
    details["files"] = (
        f"{counted(readable, 'assignment file')} in project\\"
        + (f" ({ignored} ignored: only PDF, .md and .txt are read)" if ignored else "")
        + f", {counted(datasets, 'CSV dataset')}")
    done["files"] = readable > 0
    if next_step is None and not done["files"]:
        next_step = (f"Put the assignment PDF in {shown}\\project\\ and any datasets "
                     "in datasets\\, then:",
                     command(f"prepare-project {shown}"))

    # 3-5. Drafts, spec, approval.
    present = [label for label, path in drafts.items() if path.is_file()]
    done["draft"] = len(present) == len(drafts)
    details["draft"] = f"{len(present)} of {len(drafts)} drafts"
    specs = list(grader.glob("grading_spec_v*.json")) if grader.is_dir() else []
    spec_path = latest(specs, SPEC_PATTERN)
    spec = read_json(spec_path) if spec_path else None
    spec_status = (spec or {}).get("status")
    done["spec"] = spec_path is not None
    done["approve"] = spec_status == "approved"
    if spec_path:
        unresolved = [item for item in (spec or {}).get("known_ambiguities", [])
                      if item.get("status") == "unresolved"]
        details["spec"] = spec_path.name
        details["approve"] = f"{spec_path.name}: {spec_status or 'unreadable'}" + (
            f", {counted(len(unresolved), 'unresolved question')}"
            if unresolved and spec_status != "approved" else "")
    if next_step is None and not done["draft"]:
        if present:
            warnings.append(
                f"Only {len(present)} of the 3 rubric drafts exist; prepare-project "
                "refuses to overwrite drafts. Ask the instructor before removing them.")
        next_step = ("Draft the rubric (one AI request; no student work is read).",
                     command(f"prepare-project {shown}"))
    if next_step is None and not done["spec"]:
        next_step = ("The instructor reviews and edits the three drafts "
                     "(project\\project_instructions.md, rubric\\instructor_rubric.md, "
                     "reference\\reference_solution.md); then generate the grading spec:",
                     command(f"generate-spec {shown}"))
    if next_step is None and not done["approve"]:
        next_step = ("After the instructor says the rubric is final, approve the spec:",
                     command("approve-spec " + quoted(windows_path(spec_path, cwd), True)))

    # 6. Anonymized submissions.
    manifest = read_json(grader / "submission_manifest.json")
    prepared = (manifest is not None
                and (grader / "student_map.json").is_file()
                and (grader / "anonymized_submissions").is_dir())
    done["anonymize"] = prepared
    runs_path = grader / "grading_runs"
    runs = [path for path in runs_path.iterdir() if path.is_dir()] if runs_path.is_dir() else []
    run_path = latest(runs, RUN_PATTERN)
    if prepared:
        prepared_count = manifest.get("submission_count", 0)
        details["anonymize"] = counted(prepared_count, "student")
        if manifest.get("unparsed_folder_count"):
            details["anonymize"] += (
                f", {counted(manifest['unparsed_folder_count'], 'folder')} skipped")
        if subs["students"] != prepared_count:
            redo = (command(f"prepare-submissions {shown}") if run_path is None else
                    "archive-grading first (ask the instructor), then prepare-submissions")
            warnings.append(
                f"submissions\\ now has {counted(subs['students'], 'student')}, but "
                f"{prepared_count} {verb(prepared_count, 'was', 'were')} anonymized. "
                f"To include the change: {redo}.")
    if next_step is None and not done["anonymize"]:
        next_step = ("Anonymize the submissions (names removed; no AI request).",
                     command(f"prepare-submissions {shown}"))

    # 7. Grading runs.
    run = read_json(run_path / "grading_results.json") if run_path else None
    if run_path and run is not None:
        results = run.get("results", [])
        invalid = sum(1 for item in results
                      if item.get("validation_status") == "invalid_model_response")
        details["grade"] = (f"{run_path.name}: {counted(len(results), 'student')}, "
                            f"model {run.get('model', 'unknown')}"
                            + (f", {invalid} not scored" if invalid else ""))
        stale_spec = (spec_path is not None and done["approve"]
                      and run.get("grading_spec") not in (None, spec_path.name))
        done["grade"] = not invalid and not stale_spec
        if next_step is None and stale_spec:
            next_step = (f"{run_path.name} used {run.get('grading_spec')}, but "
                         f"{spec_path.name} is now approved. Grade again (a new run is "
                         "written; earlier runs are kept):",
                         command(f"grade-submissions {shown}"))
        elif next_step is None and invalid:
            next_step = (f"{counted(invalid, 'student')} {verb(invalid, 'was', 'were')} "
                         "not scored. Request only those again:",
                         command(f"grade-submissions {shown} --resume --retry-failed"))
    elif run_path:
        details["grade"] = f"{run_path.name}: grading_results.json unreadable"
    if next_step is None and not done["grade"]:
        next_step = ("Grade the submissions (one AI request per student).",
                     command(f"grade-submissions {shown}"))

    # 8. Review file for the latest run.
    reviews = list(run_path.glob("instructor_review_v*.csv")) if run_path else []
    review_path = latest(reviews, REVIEW_PATTERN)
    if review_path:
        counts = review_counts(review_path)
        if counts is None:
            details["review"] = f"{review_path.name}: can't be read"
            warnings.append(
                f"{review_path.name} can't be read. Save it in Excel with "
                "File > Save As > CSV UTF-8.")
        else:
            total, filled = counts
            details["review"] = f"{review_path.name}: {filled} of {total} grades entered"
            done["review"] = total > 0 and filled == total
    review_shown = windows_path(review_path, cwd) if review_path else None
    if next_step is None and not done["review"]:
        if review_path is None:
            next_step = ("Create the review spreadsheet (the AI grade next to a blank "
                         "column for the instructor's grade):",
                         command(f"generate-report {shown} --run {run_path.name} --open"))
        else:
            next_step = (f"Open {quoted(review_shown)}, enter total_instructor_score for every "
                         "student (copy the AI grade where you agree), and save as CSV "
                         "UTF-8. Apply any late penalty there. Then run status again.",
                         None)

    # 9. Finalization of the latest run.
    finals_path = grader / "finalizations" / run_path.name if run_path else None
    finals = ([path for path in finals_path.iterdir() if path.is_dir()]
              if finals_path and finals_path.is_dir() else [])
    final_path = latest(finals, FINALIZATION_PATTERN)
    done["finalize"] = final_path is not None
    if final_path:
        details["finalize"] = final_path.name
        final_csv = final_path / FINAL_CSV_FILENAME
        if (review_path and final_csv.is_file()
                and review_path.stat().st_mtime > final_csv.stat().st_mtime):
            warnings.append(
                f"{review_path.name} was changed after {final_path.name}. If grades or "
                "comments changed, finalize again (a new finalization is written).")
    if next_step is None and not done["finalize"]:
        next_step = ("Finalize: writes the final grades, comments and Brightspace files.",
                     command(f"finalize-grading {shown} --run {run_path.name} "
                             f"--scores-file {quoted(review_shown, True)}"))

    # 10. Brightspace: the instructor's step; AI Grader can't see it.
    if final_path:
        final_shown = windows_path(final_path, cwd)
        imported = (final_path / GRADES_IMPORT_FILENAME).is_file()
        details["brightspace"] = ("grades import file ready" if imported
                                  else "your step")
        if next_step is None:
            next_step = (f"Enter the grades in Brightspace from {final_shown}\\"
                         f"{FINAL_CSV_FILENAME}"
                         + (f" (or import {GRADES_IMPORT_FILENAME})" if imported else "")
                         + f" and paste each comment from {FEEDBACK_TEXT_FILENAME}. "
                         "The instructor decides when grades are released.", None)

    stages = []
    current = None
    for key, title in STAGES:
        if key == "brightspace":
            state = NEXT if final_path and current is None else TODO
        elif done[key]:
            state = DONE
        elif current is None:
            state = NEXT
        else:
            state = TODO
        if state == NEXT and current is None:
            current = key
        stages.append({"key": key, "title": title, "state": state,
                       "detail": details[key]})

    action, next_command = next_step
    return {
        "project": name,
        "project_path": str(project_path),
        "current_stage": current,
        "stages": stages,
        "warnings": warnings,
        "next": {"action": action, "command": next_command},
    }


def projects_overview(projects_path, cwd=None):
    """One line per project: its current stage and step number."""

    overview = []
    for path in find_projects(projects_path):
        status = project_status(path, cwd)
        number = next((index for index, stage in enumerate(status["stages"], start=1)
                       if stage["key"] == status["current_stage"]), None)
        title = next((stage["title"] for stage in status["stages"]
                      if stage["key"] == status["current_stage"]), "")
        overview.append({"project": path.name, "step": number, "of": len(STAGES),
                         "title": title, "warnings": len(status["warnings"])})
    return overview


MARKS = {DONE: "[done]", NEXT: "[next]", TODO: "[    ]"}


def format_project_status(status):
    lines = ["", f"Project: {status['project']}", ""]
    for number, stage in enumerate(status["stages"], start=1):
        detail = f": {stage['detail']}" if stage["detail"] else ""
        lines.append(f"  {MARKS[stage['state']]} {number:>2}. {stage['title']}{detail}")
    if status["warnings"]:
        lines += ["", "Needs attention:"]
        lines += [f"  - {warning}" for warning in status["warnings"]]
    lines += ["", "Next step: " + status["next"]["action"]]
    if status["next"]["command"]:
        lines += ["", "  " + status["next"]["command"]]
    return "\n".join(lines)


def format_overview(projects_path, overview):
    if not overview:
        return (f"\nNo projects in {projects_path} yet. Start one with:\n\n"
                "  python -m ai_grader init-project PROJECT_ID")
    width = max(len(item["project"]) for item in overview)
    lines = ["", f"Projects in {projects_path}:", ""]
    for item in overview:
        where = f"step {item['step']} of {item['of']}: {item['title']}"
        attention = (f"  ({counted(item['warnings'], 'item')} "
                     f"{verb(item['warnings'], 'needs', 'need')} attention)"
                     if item["warnings"] else "")
        lines.append(f"  {item['project']:<{width}}  {where}{attention}")
    lines += ["", "Details and the next command for one project:", "",
              "  python -m ai_grader status PROJECT_ID"]
    return "\n".join(lines)
