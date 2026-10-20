"""
Brightspace-ready outputs from a finalization: a grades import CSV and the
student feedback in one place for pasting.

The grades CSV is built from a grades export the instructor downloads from
Brightspace for the grade item (Grades > Export, key field Username, Points
grade). The export already has Brightspace's exact column names, the
"#username" format and the item's maximum points, so nothing is guessed.
Only the points column is filled in. Files here contain names and grades:
they stay in the project's grader\\finalizations folder and are never sent to
a model.
"""

import codecs
import csv
import io
import json
import re
from pathlib import Path


FINAL_GRADES_FILENAME = "final_grades.json"
FEEDBACK_CSV_FILENAME = "brightspace_feedback.csv"
FEEDBACK_TEXT_FILENAME = "brightspace_feedback_to_paste.txt"
GRADES_IMPORT_FILENAME = "brightspace_grades_import.csv"

KEY_COLUMN = "Username"
IDENTIFIER_COLUMNS = ("OrgDefinedId", "Username")
END_OF_LINE_COLUMN = "End-of-Line Indicator"
POINTS_HEADER = re.compile(r"^(?P<item>.+?) Points Grade(?: <(?P<meta>[^>]*)>)?\s*$")
MAX_POINTS = re.compile(r"MaxPoints:\s*([0-9]+(?:\.[0-9]+)?)")


class BrightspaceError(ValueError):
    pass


def normalize_username(value):
    return (value or "").strip().lstrip("#").strip().casefold()


def sort_key(record):
    """Brightspace lists students by last name, then first name."""
    words = record["name"].split()
    return ([words[-1].casefold()] if words else []) + [record["name"].casefold()]


def feedback_body(rendered):
    """Student feedback text without the first line (the student's name)."""
    lines = rendered.splitlines()
    return "\n".join(lines[1:]).strip() + "\n"


def write_student_outputs(output_path, records):
    """
    Write final_grades.json (private record for exports), the feedback CSV
    and one text file with every student's feedback, ordered by last name.
    records: student_id, username, name, final_grade, max_points, feedback.
    """
    output_path = Path(output_path)
    records = sorted(records, key=sort_key)
    (output_path / FINAL_GRADES_FILENAME).write_text(
        json.dumps({"students": records}, indent=2) + "\n", encoding="utf-8")

    with (output_path / FEEDBACK_CSV_FILENAME).open(
            "w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["Username", "Student", "Grade", "Out of", "Feedback"])
        for record in records:
            writer.writerow([record["username"], record["name"],
                             f"{record['final_grade']:g}", f"{record['max_points']:g}",
                             record["feedback"].strip()])

    sections = []
    for record in records:
        sections.append(
            f"===== {record['name']} ({record['username']}) - "
            f"{record['final_grade']:g}/{record['max_points']:g} =====\n\n"
            + record["feedback"].strip() + "\n")
    (output_path / FEEDBACK_TEXT_FILENAME).write_text("\n".join(sections), encoding="utf-8")
    return output_path / FEEDBACK_CSV_FILENAME, output_path / FEEDBACK_TEXT_FILENAME


def read_template(template_path, grade_item=None):
    """
    Read a Brightspace grades export. Returns (header, rows, points column
    index, item maximum or None, whether the file had a UTF-8 BOM).
    """
    data = Path(template_path).read_bytes()
    has_bom = data.startswith(codecs.BOM_UTF8)
    text = data.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows:
        raise BrightspaceError("The Brightspace export is empty.")
    header, body = rows[0], [row for row in rows[1:] if any(cell.strip() for cell in row)]
    if KEY_COLUMN not in header:
        raise BrightspaceError(
            "The Brightspace export has no Username column. Export again with "
            "Username (or Both) as the key field.")

    points = [(index, POINTS_HEADER.match(name)) for index, name in enumerate(header)]
    points = [(index, match) for index, match in points if match]
    if grade_item:
        wanted = grade_item.strip().casefold()
        points = [(index, match) for index, match in points
                  if match.group("item").strip().casefold() == wanted]
    if len(points) != 1:
        items = [match.group("item") for _, match in
                 [(i, POINTS_HEADER.match(name)) for i, name in enumerate(header)] if match]
        if not items:
            raise BrightspaceError(
                "The Brightspace export has no 'Points Grade' column. Export again "
                "with Points grade selected.")
        raise BrightspaceError(
            "Choose one grade item with --grade-item. Items in the export: "
            + "; ".join(items))
    index, match = points[0]
    found = MAX_POINTS.search(match.group("meta") or "")
    maximum = float(found.group(1)) if found else None
    return header, body, index, maximum, has_bom


def export_grades(template_path, records, output_path, project_maximum, grade_item=None):
    """
    Write the grades import CSV for the finalized students. Returns a summary
    with counts and anonymous IDs only.
    """
    header, body, points_index, maximum, has_bom = read_template(template_path, grade_item)
    if maximum is not None and abs(maximum - project_maximum) > 1e-9:
        raise BrightspaceError(
            f"The Brightspace grade item is out of {maximum:g} points, but the "
            f"project is graded out of {project_maximum:g}. AI Grader does not "
            "rescale grades; change the grade item's maximum or tell me how to "
            "convert.")

    key_index = header.index(KEY_COLUMN)
    without = sorted(record["student_id"] for record in records
                     if not normalize_username(record["username"]))
    if without:
        raise BrightspaceError("No Brightspace username is recorded for: "
                               + ", ".join(without) + ".")
    by_username = {normalize_username(record["username"]): record for record in records}

    seen = {}
    for row in body:
        username = normalize_username(row[key_index] if key_index < len(row) else "")
        if not username:
            continue
        if username in seen:
            raise BrightspaceError("The Brightspace export lists a student twice.")
        seen[username] = row

    missing = sorted(record["student_id"] for key, record in by_username.items()
                     if key not in seen)
    if missing:
        raise BrightspaceError(
            "These students are not in the Brightspace export: " + ", ".join(missing)
            + ". Check that the export is for this course section.")

    columns = [index for index, name in enumerate(header) if name in IDENTIFIER_COLUMNS]
    columns.append(points_index)
    end_index = header.index(END_OF_LINE_COLUMN) if END_OF_LINE_COLUMN in header else None

    output = Path(output_path) / GRADES_IMPORT_FILENAME
    with output.open("w", encoding="utf-8-sig" if has_bom else "utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([header[index] for index in columns] + [END_OF_LINE_COLUMN])
        written = 0
        for row in body:
            username = normalize_username(row[key_index] if key_index < len(row) else "")
            record = by_username.get(username)
            if record is None:
                continue
            cells = [row[index] if index < len(row) else "" for index in columns[:-1]]
            end = row[end_index] if end_index is not None and end_index < len(row) else "#"
            writer.writerow(cells + [f"{record['final_grade']:g}", end or "#"])
            written += 1

    return {
        "path": output,
        "grade_column": header[points_index],
        "students_written": written,
        "export_rows_without_final_grade": len(seen) - written,
    }


def latest_finalization(grader_path, run_version):
    root = Path(grader_path) / "finalizations" / run_version
    versions = sorted(path for path in root.glob("finalization_v[0-9][0-9][0-9]")
                      if path.is_dir()) if root.is_dir() else []
    if not versions:
        raise BrightspaceError(
            f"No finalization for {run_version}. Run finalize-grading first.")
    return versions[-1]


def export_brightspace(project_path, run_version, template_path,
                       finalization=None, grade_item=None):
    """Write the grades import CSV into a finalization folder (latest by default)."""
    grader = Path(project_path).resolve() / "grader"
    if finalization:
        folder = grader / "finalizations" / run_version / finalization
    else:
        folder = latest_finalization(grader, run_version)
    final_path = folder / FINAL_GRADES_FILENAME
    if not final_path.is_file():
        raise BrightspaceError(
            f"{folder.name} has no {FINAL_GRADES_FILENAME}; it was made before "
            "Brightspace export existed. Run finalize-grading again.")
    records = json.loads(final_path.read_text(encoding="utf-8"))["students"]
    if not records:
        raise BrightspaceError("The finalization has no students.")
    maxima = {record["max_points"] for record in records}
    return export_grades(template_path, records, folder, maxima.pop(), grade_item)
