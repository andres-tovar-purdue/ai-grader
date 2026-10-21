import csv
import json
import math
import re
from pathlib import Path

from ai_grader.brightspace import feedback_body, write_student_outputs
from ai_grader.grade_adjustment import template_comment
from ai_grader.spec_validation import validate_spec_data, load_json
from ai_grader.submission_checks import spec_fingerprint
from ai_grader.rounding import apply_rounding
from ai_grader.student_language import internal_terms, remove_instructor_notes


def load_approved_scores(csv_path):
    """Read explicitly filled instructor scores; never auto-approve model scores."""
    approved = {}
    with Path(csv_path).open('r', encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if not {'student_id', 'total_instructor_score'} <= set(reader.fieldnames or []):
            raise ValueError('Approval CSV requires student_id and total_instructor_score columns.')
        for row in reader:
            student_id = (row.get('student_id') or '').strip()
            value = (row.get('total_instructor_score') or '').strip()
            if not re.fullmatch(r'Student_[0-9]{3}', student_id):
                raise ValueError('Invalid student_id in approval CSV.')
            if student_id in approved:
                raise ValueError('Duplicate student_id in approval CSV.')
            if not value:
                raise ValueError(f'Instructor score is blank for {student_id}; no score was auto-approved.')
            score = float(value)
            if not math.isfinite(score) or score < 0:
                raise ValueError('Instructor score must be finite and nonnegative.')
            approved[student_id] = score
    if not approved:
        raise ValueError('Approval CSV has no students.')
    return approved


def feedback_edits_from_review(csv_path, project_path, run_version):
    """
    Task feedback the instructor changed in a review CSV (task_N_feedback
    columns), compared with the grading run ignoring whitespace differences.
    Returns {student_id: {task number: text}}.
    """
    run_path = Path(project_path).resolve() / "grader" / "grading_runs" / run_version
    run = json.loads((run_path / "grading_results.json").read_text(encoding="utf-8"))
    original = {result["student_id"]: [task.get("feedback", "") for task in result["tasks"]]
                for result in run["results"]}
    edits = {}
    with Path(csv_path).open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            student_id = (row.get("student_id") or "").strip()
            for number, text in enumerate(original.get(student_id, []), start=1):
                edited = row.get(f"task_{number}_feedback")
                if edited is None or not edited.strip():
                    continue
                if " ".join(edited.split()) != " ".join((text or "").split()):
                    edits.setdefault(student_id, {})[number] = edited.strip()
    return edits


def _safe_filename(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")


def _next_finalization_path(grader_path, run_version):
    root = grader_path / "finalizations" / run_version
    versions = []
    if root.is_dir():
        for path in root.iterdir():
            match = re.fullmatch(r"finalization_v(\d{3})", path.name)
            if path.is_dir() and match:
                versions.append(int(match.group(1)))
    return root / f"finalization_v{max(versions, default=0) + 1:03d}"


def _overall_feedback(tasks):
    scored = [task for task in tasks if task["max_points"] > 0]
    if all(task["score"] >= task["max_points"] for task in scored):
        return ("Every part of the assignment met the requirements. The final grade "
                "reflects the completed technical work, required documentation, "
                "and submitted deliverables.")
    strongest = max(scored, key=lambda task: task["score"] / task["max_points"])
    focus = min(scored, key=lambda task: task["score"] / task["max_points"])
    return (
        f"Your strongest work was in {strongest['title']}. "
        f"Continue developing the requirements described under {focus['title']}. "
        "The final grade reflects the completed technical work, required "
        "documentation, and submitted deliverables."
    )


def _validate_student_language(text):
    found = internal_terms(text)
    if found:
        raise ValueError(
            "Student-facing feedback contains internal grading language: "
            + ", ".join(found)
        )


def finalize_grading(
    project_path,
    run_version,
    approved_scores,
    criterion_overrides=None,
    decisions=None,
    accept_instructor_totals=False,
    feedback_edits=None,
):
    """
    Create named final outputs without modifying an anonymous grading run.
    approved_scores are the instructor-approved totals of the itemized
    scores. decisions (from an adjusted review file) give each student's
    final grade and comment after the final adjustment; without them the
    final grade is the approved total and the comment is generated from the
    task scores. An approved total that differs from the sum of the task
    scores is refused unless accept_instructor_totals is True; then the
    difference is recorded and shown as an instructor adjustment to the task
    total, so the task scores still add up. feedback_edits replaces task
    feedback the instructor edited in the review file:
    {student_id: {task number (1-based): text}}.
    """
    feedback_edits = feedback_edits or {}
    language_problems = []
    project_path = Path(project_path).resolve()
    grader = project_path / "grader"
    if not re.fullmatch(r"run_v[0-9]{3}", run_version):
        raise ValueError("Invalid grading run version.")
    run_path = grader / "grading_runs" / run_version
    run = json.loads((run_path / "grading_results.json").read_text(encoding="utf-8"))
    snapshot_path = run_path / "grading_spec_snapshot.json"
    spec_name = run.get("grading_spec", "grading_spec_v001.json")
    if not re.fullmatch(r"grading_spec_v[0-9]{3}\.json", spec_name):
        raise ValueError("Invalid grading specification reference in run.")
    if run.get("grading_spec_sha256") and not snapshot_path.is_file():
        raise ValueError("Grading run specification snapshot is missing.")
    spec = load_json(snapshot_path if snapshot_path.is_file() else grader / spec_name)
    validate_spec_data(spec, load_json(Path(__file__).resolve().parents[2] /
                                      "schemas" / "grading_spec.schema.json"))
    if spec.get("status") != "approved":
        raise ValueError("Finalization requires an approved run specification.")
    if run.get("grading_spec_sha256") and spec_fingerprint(spec) != run["grading_spec_sha256"]:
        raise ValueError("Grading run specification snapshot failed its integrity check.")
    project_maximum = float(spec["project"]["total_points"])
    file_prefix = _safe_filename(spec["project"]["project_id"])
    expected_tasks = {task["task_id"]: task for task in spec["tasks"]}
    if not run.get("results"):
        raise ValueError("Grading run has no results to finalize.")
    invalid_ids = [result['student_id'] for result in run['results']
                   if result.get('validation_status') == 'invalid_model_response']
    if invalid_ids:
        raise ValueError("Cannot finalize unscored invalid model responses: "
                         + ', '.join(invalid_ids)
                         + '. Correct and revalidate the saved responses first.')
    student_map = json.loads((grader / "student_map.json").read_text(encoding="utf-8"))
    criterion_overrides = criterion_overrides or {}

    result_ids = {result["student_id"] for result in run["results"]}
    if result_ids != set(student_map) or result_ids != set(approved_scores):
        raise ValueError("Run results, student map, and approved scores do not align.")
    if decisions is not None and set(decisions) != result_ids:
        raise ValueError("Run results and adjusted grades do not align.")

    valid_overrides = {(result["student_id"], criterion["criterion_id"])
                       for result in run["results"] for task in spec["tasks"]
                       for criterion in task["criteria"]}
    if set(criterion_overrides) - valid_overrides:
        raise ValueError("Unknown student or criterion override.")
    if len(result_ids) != len(run["results"]):
        raise ValueError("Duplicate student results in grading run.")
    rows = []
    text_outputs = {}
    brightspace_records = []
    for result in run["results"]:
        student_id = result["student_id"]
        full_name = student_map[student_id]["full_name"]
        if ({task["task_id"] for task in result["tasks"]} != set(expected_tasks)
                or len(result["tasks"]) != len(expected_tasks)):
            raise ValueError("Run tasks do not match the run specification.")
        tasks = []
        raw_subtotals = []
        for task_number, task in enumerate(result["tasks"], start=1):
            expected = expected_tasks[task["task_id"]]
            expected_criteria = {c["criterion_id"]: c for c in expected["criteria"]}
            if (task["max_points"] != expected["max_points"]
                    or len(task["criteria"]) != len(expected_criteria)
                    or {c["criterion_id"] for c in task["criteria"]} != set(expected_criteria)):
                raise ValueError("Run criteria/maxima do not match the run specification.")
            score = 0.0
            for criterion in task["criteria"]:
                key = (student_id, criterion["criterion_id"])
                value = float(criterion_overrides.get(key, criterion["agent_score"]))
                maximum = expected_criteria[criterion["criterion_id"]]["max_points"]
                if not math.isfinite(value) or not 0 <= value <= maximum:
                    raise ValueError("Criterion override or score is outside its point range.")
                score += value
            raw_subtotals.append(score)
            tasks.append({"task_id": task["task_id"], "title": expected["title"],
                          "score": score, "max_points": expected["max_points"],
                          "feedback": feedback_edits.get(student_id, {}).get(
                              task_number, task["feedback"])})
        policy = run.get("rounding_policy", {}).get("identifier", "exact-v1")
        rounded = apply_rounding(raw_subtotals, [t["max_points"] for t in tasks],
                                 project_maximum, policy)
        for task, score in zip(tasks, rounded["rounded_task_subtotals"]):
            task["score"] = score
        approved_total = float(approved_scores[student_id])
        if not math.isfinite(approved_total) or not 0 <= approved_total <= project_maximum:
            raise ValueError("Approved score is outside the project point range.")
        instructor_change = round(approved_total - rounded["final_rounded_grade"], 6)
        if abs(instructor_change) > 1e-6 and not accept_instructor_totals:
            raise ValueError(
                f"Calculated final score for {student_id} is "
                f"{rounded['final_rounded_grade']:g}, not approved score {approved_total:g}. "
                "Change the task scores (--criterion-score), or keep your total as an "
                "instructor adjustment to the task total (--accept-instructor-totals).")

        if decisions is not None:
            final_grade = float(decisions[student_id]["final_grade"])
            final_comment = (decisions[student_id]["comment"]
                             or template_comment(tasks))
            edited = decisions[student_id].get("grade_edited_by_hand", False)
        else:
            final_grade, final_comment, edited = approved_total, template_comment(tasks), False
        if not math.isfinite(final_grade) or not 0 <= final_grade <= project_maximum:
            raise ValueError(f"Final grade for {student_id} is outside the project point range.")
        adjustment = round(final_grade - approved_total, 6)

        overall = _overall_feedback(tasks)
        # Notes meant for the instructor ("needs instructor review") never reach
        # the student: they are removed here and kept as a flag for the instructor.
        flags = []
        for task in tasks:
            task["feedback"], removed = remove_instructor_notes(task["feedback"])
            flags.extend(f"{task['title']}: {note}" for note in removed)
        final_comment, removed = remove_instructor_notes(final_comment)
        flags.extend(f"Comment: {note}" for note in removed)
        if not final_comment:
            final_comment = template_comment(tasks)
        found = [(f"task_{number}_feedback", internal_terms(task["feedback"]))
                 for number, task in enumerate(tasks, start=1)]
        found += [("final_comment", internal_terms(final_comment))]
        found = [(where, terms) for where, terms in found if terms]
        if found:
            language_problems.extend(
                f"{student_id} {where} ('{', '.join(terms)}')" for where, terms in found)
            continue
        student_text = [
            full_name,
            spec["project"]["title"],
            f"Final Grade: {final_grade:g}/{project_maximum:g}",
        ]
        if adjustment:
            student_text.append(f"Grade before adjustment: {approved_total:g}/"
                                f"{project_maximum:g} (adjustment: {adjustment:+g})")
        student_text.extend(["", final_comment, ""])
        for task in tasks:
            student_text.extend([
                f"{task['title']}: {task['score']:g}/{task['max_points']:g}",
                task["feedback"],
                "",
            ])
        if abs(instructor_change) > 1e-6:
            student_text.extend([
                f"Instructor adjustment to the task total: {instructor_change:+g}", ""])
        student_text.extend([
            "Overall Feedback",
            overall,
            "",
            f"Final Grade: {final_grade:g}/{project_maximum:g}",
            "",
        ])
        rendered = "\n".join(student_text)
        _validate_student_language(rendered)
        filename = (
            f"{_safe_filename(full_name)}_{file_prefix}_feedback.txt"
        )
        text_outputs[student_id] = (filename, rendered)
        brightspace_records.append({
            "student_id": student_id,
            "username": student_map[student_id].get("username", ""),
            "name": full_name,
            "final_grade": final_grade,
            "max_points": project_maximum,
            "feedback": feedback_body(rendered),
            "comment": final_comment,
        })

        row = {
            "actual_student_name": full_name,
            "anonymous_student_id": student_id,
            "preliminary_baseline_score": result["total_agent_score"],
            "final_instructor_score": approved_total,
            "overall_feedback": overall,
            "final_rounding_adjustment": round(rounded["final_rounded_grade"]
                                               - sum(t["score"] for t in tasks), 6),
            "instructor_total_adjustment": instructor_change,
            "final_adjustment": adjustment,
            "final_grade": final_grade,
            "final_grade_edited_by_hand": str(edited).lower(),
            "final_comment": final_comment,
            "flag": " | ".join(flags),
        }
        for index, task in enumerate(tasks, start=1):
            row[f"task_{index}_score"] = task["score"]
            row[f"task_{index}_feedback"] = task["feedback"]
        rows.append(row)

    if language_problems:
        raise ValueError(
            "Student-facing text contains internal grading language (often a note "
            "meant for you, such as a request to check something). Edit these cells in "
            "the review file and finalize again: " + "; ".join(language_problems) + ".")

    output_path = _next_finalization_path(grader, run_version)
    output_path.mkdir(parents=True, exist_ok=False)
    csv_path = output_path / f"{file_prefix}_instructor_summary.csv"
    task_count = len(run["results"][0]["tasks"])
    fieldnames = [
        "actual_student_name",
        "anonymous_student_id",
        "preliminary_baseline_score",
        "final_instructor_score",
        *[
            item
            for index in range(1, task_count + 1)
            for item in (f"task_{index}_score", f"task_{index}_feedback")
        ],
        "overall_feedback",
        "final_rounding_adjustment",
        "instructor_total_adjustment",
        "final_adjustment",
        "final_grade",
        "final_grade_edited_by_hand",
        "final_comment",
        "flag",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    txt_paths = []
    for student_id, (filename, rendered) in text_outputs.items():
        path = output_path / filename
        path.write_text(rendered, encoding="utf-8")
        txt_paths.append(path)

    write_student_outputs(output_path, brightspace_records)

    validation = validate_finalization(
        output_path,
        rows,
        text_outputs,
        approved_scores,
        csv_path=csv_path,
        project_maximum=project_maximum,
    )
    return output_path, csv_path, txt_paths, validation


def validate_finalization(output_path, expected_rows, text_outputs, approved_scores,
                          csv_path=None, project_maximum=100):
    """Validate name mapping, arithmetic, file cardinality, and student language."""
    output_path = Path(output_path)
    csv_path = Path(csv_path) if csv_path is not None else output_path / "Project_2_instructor_summary.csv"
    with csv_path.open("r", encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    if len(rows) != len(expected_rows):
        raise ValueError("Instructor summary row count is incorrect.")

    expected_by_id = {
        row["anonymous_student_id"]: row for row in expected_rows
    }
    for row in rows:
        student_id = row["anonymous_student_id"]
        expected = expected_by_id[student_id]
        if row["actual_student_name"] != expected["actual_student_name"]:
            raise ValueError(f"Name mapping failed for {student_id}.")
        task_scores = [
            float(value)
            for key, value in row.items()
            if re.fullmatch(r"task_\d+_score", key)
        ]
        final_score = float(row["final_instructor_score"])
        adjustment = (float(row.get("final_rounding_adjustment", 0))
                      + float(row.get("instructor_total_adjustment") or 0))
        if abs(sum(task_scores) + adjustment - final_score) > 1e-6:
            raise ValueError(f"Task arithmetic failed for {student_id}.")
        if abs(final_score - float(approved_scores[student_id])) > 1e-6:
            raise ValueError(f"CSV grade mismatch for {student_id}.")
        if "final_grade" in row:
            if abs(final_score + float(row["final_adjustment"]) - float(row["final_grade"])) > 1e-6 \
                    or abs(float(row["final_grade"]) - float(expected["final_grade"])) > 1e-6:
                raise ValueError(f"Final adjustment arithmetic failed for {student_id}.")

    txt_paths = sorted(output_path.glob("*_feedback.txt"))
    if len(txt_paths) != len(expected_rows):
        raise ValueError("Expected exactly one TXT file per student.")
    for student_id, (filename, _) in text_outputs.items():
        text = (output_path / filename).read_text(encoding="utf-8")
        _validate_student_language(text)
        final = float(expected_by_id[student_id].get("final_grade", approved_scores[student_id]))
        marker = f"Final Grade: {final:g}/{project_maximum:g}"
        if text.count(marker) != 2:
            raise ValueError(f"TXT grade mismatch for {student_id}.")

    return {
        "names_mapped": True,
        "task_totals_match_final_scores": True,
        "one_txt_per_student": True,
        "csv_txt_grades_agree": True,
        "student_feedback_has_no_internal_language": True,
    }
