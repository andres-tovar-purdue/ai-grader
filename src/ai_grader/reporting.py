"""Create named, local instructor review reports without approving grades."""

import csv
import json
import os
import re
import subprocess
import sys
from pathlib import Path


# Rounding detail stays in preliminary_grading_report.csv and grading_results.json;
# the instructor's review file shows the rounded totals only.
REVIEW_HIDDEN_COLUMNS = ("raw_total_before_rounding", "total_rounding_adjustment")


def generate_review_report(project_path, run_version=None):
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

    # Exclusive creation preserves any existing instructor edits on repeated runs.
    version = 1
    while True:
        output = run_path / f"instructor_review_v{version:03d}.csv"
        try:
            stream = output.open("x", encoding="utf-8-sig", newline="")
            break
        except FileExistsError:
            version += 1
    shown = [name for name in fields if name not in REVIEW_HIDDEN_COLUMNS]
    with stream:
        writer = csv.DictWriter(stream, fieldnames=["student_name", *shown],
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
