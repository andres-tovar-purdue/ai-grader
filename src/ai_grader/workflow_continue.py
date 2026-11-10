"""
continue: run every automatic step of the 5-stage workflow until the
instructor is needed, then stop and say what is needed.

    1 Set up         sort the dropped files; unpack the Brightspace ZIP;
                     install the course policy
    2 Rubric         draft the rubric (AI), generate the specification (AI;
                     course policy enforced), write the review package; the
                     instructor checks it with Claude and approves (--approve)
    3 Grade          anonymize, grade (AI), retry failed students once, write
                     the review sheet (AI comments and deduction labels)
    4 Review grades  the instructor enters the grades
    5 Finalize       final grades, comments and Brightspace files

Every step is one of the existing commands, run in order and versioned as
before; nothing is overwritten. Each step prints counts and file names that
AI Grader creates, never student names.
"""

import os
from pathlib import Path

from ai_grader.workflow_status import project_state


REPOSITORY = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPOSITORY / "schemas" / "grading_spec.schema.json"
MAX_STEPS = 20


class ContinueStopped(Exception):
    """A step could not finish; the message says why and what to do."""


def _setup(project_path, say):
    from ai_grader.course_policy import install_policy
    from ai_grader.project_initialization import (
        ProjectInitializationError, count_items, describe_move_counts, init_project,
    )
    from ai_grader.project_setup import sort_loose_files

    try:
        plan = init_project(str(project_path))
    except ProjectInitializationError as error:
        raise ContinueStopped(str(error)) from None
    extracted = count_items(plan, "extract")
    moved = count_items(plan, "move")
    if any(extracted):
        say(f"Unpacked the Brightspace download into submissions\\: "
            f"{describe_move_counts(*extracted)}.")
    if any(moved):
        say(f"Moved into submissions\\: {describe_move_counts(*moved)}.")
    sorted_files = sort_loose_files(project_path)
    if sorted_files["project"]:
        say("Assignment files moved to project\\: " + ", ".join(sorted_files["project"]))
    if sorted_files["datasets"]:
        say("Datasets moved to datasets\\: " + ", ".join(sorted_files["datasets"]))
    if sorted_files["unread"]:
        say("Moved to project\\ but not read (save as PDF if the rubric needs them): "
            + ", ".join(sorted_files["unread"]))
    if sorted_files["already_there"]:
        say("Left in place because a file with the same name is already sorted: "
            + ", ".join(sorted_files["already_there"]))
    if sorted_files["left"]:
        say("Not sorted (unknown type): " + ", ".join(sorted_files["left"]))
    policy = install_policy(project_path)
    if policy:
        say(f"Course grading policy copied to rubric\\{policy.name}.")


def _write_package(project_path, state, say):
    from ai_grader.review_package import write_review_package
    spec_path = Path(project_path) / "grader" / state["facts"]["spec"]
    package = write_review_package(project_path, spec_path)
    say(f"Review package for Claude (no student data): grader\\{package.name}")


def _generate_spec(project_path, say, response_path=None):
    from ai_grader.spec_generation import (
        InvalidSpecResponseError, derived_presence_deliverables, generate_grading_spec,
    )
    attempts = 1 if response_path else 2
    for attempt in range(1, attempts + 1):
        try:
            output_path, spec = generate_grading_spec(project_path, SCHEMA_PATH,
                                                      response_path=response_path)
            break
        except InvalidSpecResponseError as error:
            problems = "; ".join(error.messages[:5])
            if attempt == attempts:
                where = (f" The response was saved as grader\\{Path(error.response_path).name};"
                         " upload it to Claude to have it corrected."
                         if error.saved_again else "")
                raise ContinueStopped(
                    f"The grading specification failed AI Grader's checks: {problems}."
                    + where) from None
            say(f"The specification failed AI Grader's checks ({problems}); asking again.")
    filled = derived_presence_deliverables(spec)
    split = "attempted but incomplete" in spec.get("notes", "")
    say(f"Grading specification written: grader\\{output_path.name} "
        f"({len(spec['tasks'])} tasks, {spec['project']['total_points']:g} points"
        + (", course policy applied" if split else "")
        + (f", file-presence patterns filled for {len(filled)}" if filled else "") + ").")
    return output_path


def _approve(project_path, state, say):
    from ai_grader.spec_approval import approve_grading_spec
    approved_by = os.getenv("GRADER_INSTRUCTOR_NAME")
    if not approved_by:
        raise ContinueStopped("GRADER_INSTRUCTOR_NAME is not set in ai-grader\\.env; add the "
                              "instructor's name there, then approve again.")
    spec_path = Path(project_path) / "grader" / state["facts"]["spec"]
    try:
        approve_grading_spec(spec_path, SCHEMA_PATH, approved_by=approved_by)
    except RuntimeError as error:
        if "unresolved" not in str(error):
            raise
        raise ContinueStopped(
            f"Not approved: {error} Settle these questions with Claude (it can return a "
            "corrected specification as grader\\grading_spec_reviewed.json), then approve "
            "again.") from None
    say(f"Rubric approved by {approved_by}: grader\\{spec_path.name}")


def _grade(project_path, state, say, model=None):
    from ai_grader.grading import grade_submissions
    from ai_grader.reporting import generate_review_report
    from ai_grader.submission_checks import refresh_submission_checks
    from ai_grader.submission_processing import write_submission_manifest

    facts = state["facts"]
    if not facts["prepared"] or (facts["prepared"]
                                 and facts["prepared_students"] != facts["submissions"]["students"]
                                 and not facts["run"]):
        say("Anonymizing the submissions (no AI request)...")
        _, _, manifest = write_submission_manifest(project_path)
        say(f"  {manifest['submission_count']} students anonymized"
            + (f", {manifest['unparsed_folder_count']} folders skipped"
               if manifest.get("unparsed_folder_count") else "") + ".")
        repositories = manifest.get("repository_evidence") or {}
        statuses = repositories.get("fetch_status_counts") or {}
        if statuses:
            say("  GitHub repositories: " + ", ".join(
                f"{count} {status.replace('_', ' ')}" for status, count in statuses.items()))
        if repositories.get("temporary_folders_not_removed"):
            say("  Some temporary download folders were in use and were left in grader\\; "
                "the next run removes them.")
        state = project_state(project_path)
        facts = state["facts"]
    elif not facts["checks_current"]:
        refresh_submission_checks(project_path)
        say("File-name checks updated for the approved rubric.")
        state = project_state(project_path)
        facts = state["facts"]

    if not facts["run"] or facts["stale_run"]:
        say(f"Grading {facts['prepared_students']} students (one AI request each; this can "
            "take a few minutes per student)...")
        run_path, _, _, run = grade_submissions(project_path, model=model)
        invalid = run.get("unscored_submission_count", 0)
        say(f"  {run_path.name}: {run['submission_count']} graded"
            + (f", {invalid} not scored" if invalid else "") + ".")
        state = project_state(project_path)
        facts = state["facts"]
    if facts["not_scored"]:
        say(f"Asking again for the {facts['not_scored']} student(s) not scored "
            "(the others are reused)...")
        run_path, _, _, run = grade_submissions(project_path, model=model, resume=True,
                                                retry_failed=True)
        state = project_state(project_path)
        facts = state["facts"]
        if facts["not_scored"]:
            raise ContinueStopped(
                f"{facts['not_scored']} student(s) still could not be scored in {facts['run']}. "
                "Run continue once more; if it keeps failing, share the grading output (it "
                "names students only by anonymous ID) with Claude.")
        say(f"  {facts['run']}: all {facts['graded']} students scored.")
    if not facts["review_file"]:
        say("Writing your review sheet (short comments and deduction labels; one small AI "
            "request)...")
        stats = {}
        path = generate_review_report(project_path, facts["run"], use_model_comments=True,
                                      model=model, comment_stats=stats)
        say(f"  {path.name} in grader\\grading_runs\\{facts['run']}\\"
            + (f" (kept your entries from {stats['carried_from']})"
               if stats.get("carried_over") else ""))


def _finalize(project_path, state, say):
    from ai_grader.finalization import feedback_edits_from_review, finalize_grading
    from ai_grader.grade_adjustment import AdjustmentError, review_decisions

    facts = state["facts"]
    run = facts["run"]
    review_path = Path(project_path) / "grader" / "grading_runs" / run / facts["review_file"]
    try:
        approved, decisions = review_decisions(review_path)
    except AdjustmentError as error:
        raise ContinueStopped(str(error)) from None
    edits = feedback_edits_from_review(review_path, project_path, run)
    try:
        output_path, _, txt_paths, validation = finalize_grading(
            project_path, run, approved, None, decisions, True, edits)
    except ValueError as error:
        raise ContinueStopped(f"Not finalized: {error}") from None
    differ = sum(1 for sid, decision in decisions.items()
                 if decision.get("ai_grade") is not None
                 and abs(decision["ai_grade"] - approved[sid]) > 1e-9)
    say(f"Final grades written: grader\\finalizations\\{run}\\{output_path.name} "
        f"({len(txt_paths)} students; your grade differs from the AI's for {differ}).")
    unreadable = validation.get("comments_with_unreadable_deductions")
    if unreadable:
        say(f"  Deductions for {', '.join(unreadable)} aren't written as 'label (-points)'; "
            "their comments list them as written.")


def continue_project(project_path, approve=False, say=print, model=None):
    """
    Run the automatic steps until the instructor is needed. Returns the final
    project state. Raises ContinueStopped when a step cannot finish.
    """
    project_path = Path(project_path).resolve()
    done_keys = []
    for _ in range(MAX_STEPS):
        state = project_state(project_path)
        action = state["next"]
        if action["who"] == "you":
            current = state["current_stage"]
            if approve and current == "rubric" and state["facts"]["spec_status"] == "draft" \
                    and "approve" not in done_keys:
                _approve(project_path, state, say)
                done_keys.append("approve")
                continue
            return state
        key = action["key"]
        if done_keys.count(key) >= 2:
            raise ContinueStopped(f"The step '{key}' did not finish after two tries.")
        done_keys.append(key)
        if key == "setup":
            say("Stage 1, set up:")
            _setup(project_path, say)
        elif key == "draft":
            from ai_grader.project_preparation import prepare_project
            say("Stage 2, rubric: drafting the rubric, instructions and reference solution "
                "(one AI request)...")
            prepare_project(project_path)
            say("  Drafts written: project\\project_instructions.md, "
                "rubric\\instructor_rubric.md, reference\\reference_solution.md")
            say("Generating the grading specification (one AI request)...")
            _generate_spec(project_path, say)
            _write_package(project_path, project_state(project_path), say)
        elif key == "spec":
            say("Stage 2, rubric: generating the grading specification (one AI request)...")
            _generate_spec(project_path, say)
            _write_package(project_path, project_state(project_path), say)
        elif key == "install_reviewed":
            reviewed = project_path / "grader" / "grading_spec_reviewed.json"
            say("Stage 2, rubric: checking the reviewed specification (no AI request)...")
            _generate_spec(project_path, say, response_path=reviewed)
            if not approve:
                _write_package(project_path, project_state(project_path), say)
        elif key == "grade":
            say("Stage 3, grade:")
            _grade(project_path, state, say, model=model)
        elif key == "finalize":
            say("Stage 5, finalize:")
            _finalize(project_path, state, say)
        else:
            raise ContinueStopped(f"Unknown step: {key}")
    raise ContinueStopped("Stopped after too many steps; run status to see where things stand.")
