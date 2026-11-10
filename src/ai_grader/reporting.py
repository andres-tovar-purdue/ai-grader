"""Create named, local instructor review reports without approving grades."""

import csv
import json
import os
import re
import subprocess
import sys
from pathlib import Path


from ai_grader.ai_client import resolve_model
from ai_grader.grade_adjustment import (
    DEDUCTION_COLUMNS, _load_spec, comments_for_run, review_fieldnames, review_flag,
    review_versions,
)
from ai_grader.itemized_feedback import deduction_items, deduction_labels, deductions_cell


# What the instructor typed in an earlier review file of the same run; a new
# review file keeps it, so regenerating (e.g. to add the deductions column)
# never loses entered grades or edited comments.
CARRIED_COLUMNS = ("total_instructor_score", "final_comment", "adjustment_reason")


def earlier_entries(run_path):
    """
    ({student_id: {column: value}}, file name, was an adjusted file) from the
    run's latest review file, or ({}, None, False) if there is none or it
    can't be read.
    """
    versions = review_versions(run_path)
    if not versions:
        return {}, None, False
    latest = versions[-1]
    try:
        with latest.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields = reader.fieldnames or []
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error):
        return {}, None, False
    if "student_id" not in fields:
        return {}, None, False
    entries = {}
    for row in rows:
        kept = {column: (row.get(column) or "").strip() for column in CARRIED_COLUMNS
                if (row.get(column) or "").strip()}
        if kept and row.get("student_id"):
            entries[row["student_id"]] = kept
    return entries, latest.name, "adjusted_grade" in fields


def generate_review_report(project_path, run_version=None, use_model_comments=False,
                           client=None, model=None, comment_stats=None):
    """
    Write the next instructor_review_v###.csv: student, the AI grade, a
    blank total_instructor_score for your grade, a short final_comment
    (model-written when use_model_comments, else built from the task
    scores), then the itemized details. comment_stats, if given, is filled
    with counts.
    """
    grader = Path(project_path).resolve() / "grader"
    runs = grader / "grading_runs"
    if run_version is None:
        available = sorted(path for path in runs.glob("run_v*")
                           if path.is_dir() and re.fullmatch(r"run_v[0-9]{3}", path.name))
        if not available:
            raise ValueError("No grading runs found. Grade or recover submissions first.")
        run_version = available[-1].name
    if not re.fullmatch(r"run_v[0-9]{3}", run_version):
        raise ValueError("Invalid grading run version.")
    run_path = runs / run_version
    run = json.loads((run_path / "grading_results.json").read_text(encoding="utf-8-sig"))
    student_map = json.loads((grader / "student_map.json").read_text(encoding="utf-8-sig"))
    result_ids = [result["student_id"] for result in run.get("results", [])]
    if not result_ids or len(set(result_ids)) != len(result_ids):
        raise ValueError("Grading run has missing or duplicate student results.")
    if set(result_ids) != set(student_map):
        raise ValueError("Grading run and private student map do not align.")
    for student_id in result_ids:
        name = student_map[student_id].get("full_name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Student name is missing for {student_id}.")

    with (run_path / "preliminary_grading_report.csv").open(
            encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        if not {"student_id", "total_agent_score", "total_instructor_score"} <= set(fields):
            raise ValueError("Preliminary report is missing required columns.")
        if len(set(fields)) != len(fields) or "student_name" in fields:
            raise ValueError("Preliminary report has duplicate or conflicting columns.")
        rows = list(reader)
    csv_ids = [row["student_id"] for row in rows]
    if len(set(csv_ids)) != len(csv_ids) or set(csv_ids) != set(result_ids):
        raise ValueError("Preliminary report and grading run do not align.")
    for row in rows:
        if None in row:
            raise ValueError("Preliminary report has malformed rows.")
        row["student_name"] = student_map[row["student_id"]]["full_name"]
        row["total_instructor_score"] = ""
    comments, fallbacks = comments_for_run(project_path, run_version, client, model,
                                           use_model_comments)
    try:
        spec = _load_spec(grader, run_path, run)
    except (OSError, ValueError):
        spec = {"tasks": []}
    items = {result["student_id"]: deduction_items(result, spec)
             for result in run.get("results", [])}
    labels, label_fallbacks, _ = deduction_labels(
        run_path, items, client, resolve_model(model), use_model_comments)
    carried, carried_from, carried_adjusted = earlier_entries(run_path)
    templates = (comments_for_run(project_path, run_version, use_model=False)[0]
                 if carried else {})
    for row in rows:
        student_id = row["student_id"]
        row["final_comment"] = comments.get(student_id, ("", "template"))[0]
        row["flag"] = review_flag(row)
        row["deductions"] = deductions_cell(items.get(student_id, []),
                                            labels.get(student_id, []))
        row["adjustment_reason"] = ""
        kept = dict(carried.get(student_id, {}))
        earlier_comment = kept.get("final_comment")
        if earlier_comment in (row["final_comment"],
                               templates.get(student_id, ("", ""))[0]):
            kept.pop("final_comment")       # Not edited by hand: use the new comment.
        row.update(kept)
    if comment_stats is not None:
        comment_stats.update({
            "ai": sum(1 for _, source in comments.values() if source == "ai"),
            "template": sum(1 for _, source in comments.values() if source == "template"),
            "fallbacks": sum(fallbacks.values()), "fallback_reasons": fallbacks,
            "deductions": sum(len(value) for value in items.values()),
            "label_fallbacks": sum(label_fallbacks.values()),
            "label_fallback_reasons": label_fallbacks,
            "carried_over": len(carried), "carried_from": carried_from,
            "carried_from_adjusted": carried_adjusted})

    # Exclusive creation preserves any existing instructor edits on repeated runs.
    version = 1
    while True:
        output = run_path / f"instructor_review_v{version:03d}.csv"
        try:
            stream = output.open("x", encoding="utf-8-sig", newline="")
            break
        except FileExistsError:
            version += 1
    with stream:
        writer = csv.DictWriter(stream,
                                fieldnames=review_fieldnames(fields + DEDUCTION_COLUMNS,
                                                             adjusted=False),
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return output


def open_report(path):
    """Open a report using the computer's default application."""
    if sys.platform == "win32":
        os.startfile(str(path))
    else:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", str(path)],
                       check=True)
