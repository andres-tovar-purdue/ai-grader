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

# The 5 stages (key, title, the instructor's part).
STAGES = [
    ("setup", "Set up", "drop the files in the project folder"),
    ("rubric", "Rubric", "check it with Claude, approve once"),
    ("grade", "Grade", "none"),
    ("review", "Review grades", "enter or confirm each grade"),
    ("finalize", "Finalize", "enter the grades in Brightspace"),
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
         if path.is_dir() and not path.name.startswith((".", "_"))),
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


def _warnings_for_submissions(subs):
    warnings = []
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
    return warnings


def _mtime(path):
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0.0


def _checks_current(manifest, spec):
    """Whether the prepared submissions' file-name checks used this specification."""
    from ai_grader.submission_checks import spec_fingerprint
    if not manifest or not spec:
        return False
    fingerprint = spec_fingerprint(spec)
    submissions = manifest.get("submissions", [])
    return bool(submissions) and all(
        (item.get("deliverable_checks") or {}).get("spec_sha256") == fingerprint
        for item in submissions)


def project_state(project_path, cwd=None):
    """
    Everything status and continue need to know about a project: the facts,
    the 5 stages (done / next / todo with a short detail), warnings, and the
    next action, either automatic (continue does it) or the instructor's.
    Never contains student names or submission folder names.
    """
    from ai_grader.course_policy import course_policy_path, project_policy_path
    from ai_grader.project_setup import loose_instructor_files

    project_path = Path(project_path).resolve()
    cwd = Path(cwd or Path.cwd()).resolve()
    if not project_path.is_dir():
        raise StatusError(f"Project folder does not exist: {project_path}")
    name = project_path.name
    shown = quoted(windows_path(project_path, cwd))
    grader = project_path / "grader"
    facts = {}
    warnings = []

    # Stage 1: set up.
    missing = [folder for folder in PROJECT_SUBFOLDERS if not (project_path / folder).is_dir()]
    subs = submissions_summary(project_path / "submissions")
    top_zips = [path.name for path in project_path.glob("*.zip") if path.is_file()]
    loose = loose_instructor_files(project_path)
    drafts = {label: project_path / relative for label, relative in DRAFTS.items()}
    readable = count_files(project_path / "project", SUPPORTED_PROJECT_EXTENSIONS,
                           exclude={drafts["project instructions"]})
    ignored = count_files(project_path / "project",
                          exclude={drafts["project instructions"]}) - readable
    datasets = count_files(project_path / "datasets")
    warnings += _warnings_for_submissions(subs)
    if ignored:
        warnings.append(f"{counted(ignored, 'file')} in project\\ {verb(ignored, 'is', 'are')} "
                        "not read (only PDF, .md and .txt are): save Word documents and "
                        "notebooks as PDF if the rubric needs them.")
    policy_installed = project_policy_path(project_path).is_file()
    facts.update(submissions=subs, assignment_files=readable, datasets=datasets,
                 loose_files=loose, top_zips=top_zips, missing_folders=missing,
                 policy=policy_installed, course_policy=course_policy_path(project_path).is_file())
    has_spec = grader.is_dir() and any(grader.glob("grading_spec_v*.json"))
    setup_pending = bool(missing or loose or (top_zips and not subs["attempt_folders"])
                         or (facts["course_policy"] and not policy_installed and not has_spec))
    setup_done = not setup_pending and subs["attempt_folders"] > 0 and readable > 0
    setup_detail = (f"{counted(subs['students'], 'student')}, "
                    f"{counted(readable, 'assignment file')}, "
                    f"{counted(datasets, 'dataset')}"
                    + (", course policy" if policy_installed else ""))

    # Stage 2: rubric.
    present = [label for label, path in drafts.items() if path.is_file()]
    specs = list(grader.glob("grading_spec_v*.json")) if grader.is_dir() else []
    spec_path = latest(specs, SPEC_PATTERN)
    spec = read_json(spec_path) if spec_path else None
    spec_status = (spec or {}).get("status")
    unresolved = [item for item in (spec or {}).get("known_ambiguities", [])
                  if item.get("status") == "unresolved"]
    reviewed = grader / "grading_spec_reviewed.json"
    reviewed_pending = reviewed.is_file() and _mtime(reviewed) > _mtime(spec_path)
    rubric_sources = [path for path in drafts.values() if path.is_file()]
    if policy_installed:
        rubric_sources.append(project_policy_path(project_path))
    rubric_changed = bool(spec_path) and any(_mtime(path) > _mtime(spec_path) + 1
                                             for path in rubric_sources)
    packages = sorted(grader.glob("review_package_v*.zip")) if grader.is_dir() else []
    package = packages[-1] if packages else None
    facts.update(drafts=len(present), spec=spec_path.name if spec_path else None,
                 spec_status=spec_status, unresolved=len(unresolved),
                 reviewed_spec_waiting=reviewed_pending, rubric_changed=rubric_changed,
                 review_package=package.name if package else None)
    rubric_done = spec_status == "approved" and not reviewed_pending and not rubric_changed
    rubric_detail = (f"{spec_path.name}: {spec_status}" if spec_path
                     else f"{len(present)} of 3 drafts")
    if unresolved and spec_status != "approved":
        rubric_detail += f", {counted(len(unresolved), 'open question')}"

    # Stage 3: grade.
    manifest = read_json(grader / "submission_manifest.json")
    prepared = (manifest is not None and (grader / "student_map.json").is_file()
                and (grader / "anonymized_submissions").is_dir())
    runs_path = grader / "grading_runs"
    runs = [path for path in runs_path.iterdir() if path.is_dir()] if runs_path.is_dir() else []
    run_path = latest(runs, RUN_PATTERN)
    run = read_json(run_path / "grading_results.json") if run_path else None
    prepared_count = (manifest or {}).get("submission_count", 0)
    added_after = prepared and subs["students"] != prepared_count
    checks_current = prepared and _checks_current(manifest, spec if rubric_done else None)
    results = (run or {}).get("results", [])
    invalid = sum(1 for item in results
                  if item.get("validation_status") == "invalid_model_response")
    stale_run = bool(run) and rubric_done and run.get("grading_spec") not in (None, spec_path.name)
    reviews = list(run_path.glob("instructor_review_v*.csv")) if run_path else []
    review_path = latest(reviews, REVIEW_PATTERN)
    facts.update(prepared=prepared, prepared_students=prepared_count, checks_current=checks_current,
                 run=run_path.name if run_path else None, model=(run or {}).get("model"),
                 graded=len(results), not_scored=invalid, stale_run=stale_run,
                 review_file=review_path.name if review_path else None)
    if added_after:
        warnings.append(
            f"submissions\\ now has {counted(subs['students'], 'student')}, but "
            f"{prepared_count} {verb(prepared_count, 'was', 'were')} anonymized."
            + (" continue prepares them again before grading." if not run_path else
               " Grading has started: to include the change, set the runs aside with "
               f"python -m ai_grader archive-grading {shown} (ask the instructor), then continue."))
    grade_done = (rubric_done and prepared and not added_after and bool(run) and not invalid
                  and not stale_run and review_path is not None)
    grade_detail = (f"{run_path.name}: {counted(len(results), 'student')}, model "
                    f"{(run or {}).get('model', 'unknown')}"
                    + (f", {invalid} not scored" if invalid else "")
                    + ("" if review_path else ", review sheet not written yet")
                    if run_path and run is not None else
                    (f"{counted(prepared_count, 'student')} anonymized" if prepared else ""))

    # Stage 4: review grades.
    counts = review_counts(review_path) if review_path else None
    if review_path and counts is None:
        warnings.append(f"{review_path.name} can't be read. Save it in Excel with "
                        "File > Save As > CSV UTF-8.")
    total, filled = counts or (0, 0)
    review_done = grade_done and counts is not None and total > 0 and filled == total
    review_detail = (f"{review_path.name}: {filled} of {total} grades entered"
                     if counts else (f"{review_path.name}: can't be read" if review_path else ""))

    # Stage 5: finalize.
    finals_path = grader / "finalizations" / run_path.name if run_path else None
    finals = ([path for path in finals_path.iterdir() if path.is_dir()]
              if finals_path and finals_path.is_dir() else [])
    final_path = latest(finals, FINALIZATION_PATTERN)
    final_csv = final_path / FINAL_CSV_FILENAME if final_path else None
    final_stale = bool(final_csv and review_path and final_csv.is_file()
                       and _mtime(review_path) > _mtime(final_csv))
    finalize_done = review_done and final_path is not None and not final_stale
    facts.update(finalization=final_path.name if final_path else None, final_stale=final_stale)
    finalize_detail = (final_path.name + (" (review sheet changed since)" if final_stale else "")
                       if final_path else "")

    # The next action.
    def you(text, command=None):
        return {"who": "you", "text": text, "command": command}

    def auto(key, text):
        return {"who": "continue", "key": key, "text": text,
                "command": f"python -m ai_grader continue {quoted(name)}"}

    if not setup_done:
        if setup_pending:
            action = auto("setup", "Sort the files you added into the project folders.")
        elif not subs["attempt_folders"]:
            action = you(f"Put the Brightspace download ZIP in {shown} (top level, not in a "
                         "subfolder), then run continue.",
                         f"python -m ai_grader continue {quoted(name)}")
        else:
            action = you(f"Put the assignment PDF (and any datasets and your solution) in "
                         f"{shown}, then run continue.",
                         f"python -m ai_grader continue {quoted(name)}")
    elif not rubric_done:
        if not present:
            action = auto("draft", "Draft the rubric and the grading specification "
                                   "(two AI requests), then write the review package.")
        elif len(present) < 3:
            action = you(f"Only {len(present)} of the 3 rubric drafts exist. Remove the "
                         "incomplete drafts (ask the instructor) and run continue to draft "
                         "them again.")
        elif reviewed_pending:
            action = auto("install_reviewed", "Check and install the reviewed grading "
                                              "specification (no AI request).")
        elif not spec_path or rubric_changed:
            action = auto("spec", "Generate the grading specification from the rubric "
                                  "(one AI request) and write the review package.")
        else:
            package_text = (f"grader\\{package.name}" if package
                            else "the review package")
            action = you(
                f"Check the rubric: upload {package_text} (no student data) to Claude and "
                "save any corrected files it returns as it says. When you are satisfied, "
                "approve it:" + (f" ({counted(len(unresolved), 'open question')} to settle "
                                 "first)" if unresolved else ""),
                f"python -m ai_grader continue {quoted(name)} --approve")
    elif subs["unrecognized_folders"]:
        action = you("Rename the submission folders listed under Needs attention, then "
                     "run continue.", f"python -m ai_grader continue {quoted(name)}")
    elif added_after and run_path:
        action = you("A student was added after grading started; see Needs attention.")
    elif not grade_done:
        action = auto("grade", "Anonymize the submissions, grade them (one AI request per "
                               "student, failed ones retried once) and write your review "
                               "sheet.")
    elif not review_done:
        review_shown = quoted(windows_path(review_path, cwd))
        action = you(f"Open {review_shown} and enter your grade in total_instructor_score "
                     "for every student (copy the AI grade where you agree; put a late "
                     "penalty in the grade and 'Late submission' in adjustment_reason). Check "
                     "deductions and final_comment, save as CSV UTF-8, then run continue.",
                     f"python -m ai_grader continue {quoted(name)}")
    elif not finalize_done:
        action = auto("finalize", "Write the final grades, comments and Brightspace files.")
    else:
        final_shown = windows_path(final_path, cwd)
        action = you(f"Enter the grades in Brightspace from {final_shown}\\"
                     f"{FINAL_CSV_FILENAME} (Grade and Comment columns) and publish when you "
                     "are ready. AI Grader never uploads or releases grades.")

    done = [setup_done, rubric_done, grade_done, review_done, finalize_done]
    details = [setup_detail, rubric_detail, grade_detail, review_detail, finalize_detail]
    stages, current = [], None
    for (key, title, role), is_done, detail in zip(STAGES, done, details):
        if is_done:
            state = DONE
        elif current is None:
            state, current = NEXT, key
        else:
            state = TODO
        stages.append({"key": key, "title": title, "your_part": role, "state": state,
                       "detail": detail})
    return {"project": name, "project_path": str(project_path), "current_stage": current,
            "stages": stages, "warnings": warnings, "next": action, "facts": facts}


def project_status(project_path, cwd=None):
    """Earlier name of project_state."""
    return project_state(project_path, cwd)


def projects_overview(projects_path, cwd=None):
    """One line per project: its current stage and step number."""

    overview = []
    for path in find_projects(projects_path):
        status = project_state(path, cwd)
        number = next((index for index, stage in enumerate(status["stages"], start=1)
                       if stage["key"] == status["current_stage"]), None)
        title = next((stage["title"] for stage in status["stages"]
                      if stage["key"] == status["current_stage"]), "")
        overview.append({"project": path.name, "step": number, "of": len(STAGES),
                         "title": title, "waiting_for": status["next"]["who"],
                         "warnings": len(status["warnings"])})
    return overview


MARKS = {DONE: "[done]", NEXT: "[next]", TODO: "[    ]"}


def format_project_status(status):
    lines = ["", f"Project: {status['project']}", ""]
    for number, stage in enumerate(status["stages"], start=1):
        detail = f": {stage['detail']}" if stage["detail"] else ""
        lines.append(f"  {MARKS[stage['state']]} {number}. {stage['title']}{detail}")
    if status["warnings"]:
        lines += ["", "Needs attention:"]
        lines += [f"  - {warning}" for warning in status["warnings"]]
    action = status["next"]
    if action["who"] == "continue":
        lines += ["", f"Next: AI Grader will {action['text'][0].lower()}{action['text'][1:]}",
                  "", "  " + action["command"]]
    else:
        lines += ["", "Your turn: " + action["text"]]
        if action.get("command"):
            lines += ["", "  " + action["command"]]
    return "\n".join(lines)


def format_overview(projects_path, overview):
    if not overview:
        return (f"\nNo projects in {projects_path} yet. Create a folder for one in "
                "projects\\, drop the Brightspace ZIP and the assignment PDF in it, and run:"
                "\n\n  python -m ai_grader continue PROJECT_ID")
    width = max(len(item["project"]) for item in overview)
    lines = ["", f"Projects in {projects_path}:", ""]
    for item in overview:
        if item["step"] is None:
            where = "done: enter the grades in Brightspace"
        else:
            whose = "your turn" if item["waiting_for"] == "you" else "run continue"
            where = f"stage {item['step']} of {item['of']}: {item['title']} ({whose})"
        attention = (f"  ({counted(item['warnings'], 'item')} "
                     f"{verb(item['warnings'], 'needs', 'need')} attention)"
                     if item["warnings"] else "")
        lines.append(f"  {item['project']:<{width}}  {where}{attention}")
    lines += ["", "Details and the next step for one project:", "",
              "  python -m ai_grader status PROJECT_ID"]
    return "\n".join(lines)
