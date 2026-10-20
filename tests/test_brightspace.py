"""Brightspace grades import CSV and feedback outputs. Synthetic data only."""

import codecs
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_grader.brightspace import (
    FEEDBACK_CSV_FILENAME, FEEDBACK_TEXT_FILENAME, FINAL_GRADES_FILENAME,
    GRADES_IMPORT_FILENAME, BrightspaceError, export_brightspace,
)
from ai_grader.cli import main
from ai_grader.finalization import finalize_grading


STUDENTS = {
    "Student_001": ("Avery Student", "astudent", 8),
    "Student_002": ("Blake Example", "bexample", 10),
}
ITEM = "Project 2 Points Grade <Numeric MaxPoints:10 Weight:20 Category:Projects>"


def make_run(root):
    grader = root / "grader"
    run_path = grader / "grading_runs" / "run_v001"
    run_path.mkdir(parents=True)
    spec = {
        "schema_version": "1.0", "spec_version": "1", "status": "approved",
        "project": {"project_id": "demo", "title": "Demo Assignment", "total_points": 10},
        "sources": [{"source_id": "published", "source_type": "project_instructions",
                     "path": "project/instructions.md"}],
        "deliverables": [], "known_ambiguities": [],
        "tasks": [{"task_id": "T1", "title": "Task One", "max_points": 10,
                   "criteria": [{"criterion_id": "C1", "max_points": 10,
                                 "description": "Work", "requirement_type": "technical_correctness",
                                 "full_credit_condition": "Complete work."}]}],
    }
    (grader / "grading_spec_v001.json").write_text(json.dumps(spec), encoding="utf-8")
    (grader / "student_map.json").write_text(json.dumps({
        student_id: {"full_name": name, "username": username}
        for student_id, (name, username, _) in STUDENTS.items()
    }), encoding="utf-8")
    run = {"results": [{
        "student_id": student_id,
        "total_agent_score": score,
        "tasks": [{"task_id": "T1", "title": "Task One", "agent_score": score,
                   "max_points": 10, "feedback": f"Feedback for score {score}.",
                   "criteria": [{"criterion_id": "C1", "agent_score": score}]}],
    } for student_id, (_, _, score) in STUDENTS.items()]}
    (run_path / "grading_results.json").write_text(json.dumps(run), encoding="utf-8")
    return {student_id: score for student_id, (_, _, score) in STUDENTS.items()}


def write_template(path, rows, header=None, bom=True):
    header = header or ["OrgDefinedId", "Username", "Last Name", "First Name", "Email",
                        ITEM, "End-of-Line Indicator"]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    data = buffer.getvalue().encode("utf-8")
    path.write_bytes((codecs.BOM_UTF8 if bom else b"") + data)
    return path


TEMPLATE_ROWS = [
    ["#0001", "#astudent", "Student", "Avery", "a@example.edu", "", "#"],
    ["#0003", "#cnosubmit", "Nosubmit", "Casey", "c@example.edu", "", "#"],
    ["#0002", "#BExample", "Example", "Blake", "b@example.edu", "", "#"],
]


class BrightspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        scores = make_run(self.root)
        self.output, _, _, _ = finalize_grading(self.root, "run_v001", scores)

    def test_finalization_writes_feedback_by_last_name(self):
        with (self.output / FEEDBACK_CSV_FILENAME).open(encoding="utf-8-sig", newline="") as file:
            rows = list(csv.DictReader(file))
        self.assertEqual([row["Username"] for row in rows], ["bexample", "astudent"])
        self.assertEqual((rows[0]["Grade"], rows[0]["Out of"]), ("10", "10"))
        self.assertTrue(rows[0]["Feedback"].startswith("Demo Assignment\nFinal Grade: 10/10"))
        self.assertNotIn("Blake Example\n", rows[0]["Feedback"])
        text = (self.output / FEEDBACK_TEXT_FILENAME).read_text(encoding="utf-8")
        self.assertIn("===== Blake Example (bexample) - 10/10 =====", text)
        self.assertLess(text.index("Blake"), text.index("Avery"))
        final = json.loads((self.output / FINAL_GRADES_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(len(final["students"]), 2)

    def test_grades_import_fills_only_the_points_column(self):
        template = write_template(self.root / "export.csv", TEMPLATE_ROWS)
        summary = export_brightspace(self.root, "run_v001", template)
        self.assertEqual((summary["students_written"],
                          summary["export_rows_without_final_grade"]), (2, 1))
        data = (self.output / GRADES_IMPORT_FILENAME).read_bytes()
        self.assertTrue(data.startswith(codecs.BOM_UTF8))   # Mirrors the export.
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
        self.assertEqual(rows, [
            ["OrgDefinedId", "Username", ITEM, "End-of-Line Indicator"],
            ["#0001", "#astudent", "8", "#"],
            ["#0002", "#BExample", "10", "#"],
        ])

    def test_grade_item_choice_and_errors(self):
        header = ["Username", "Project 1 Points Grade", ITEM, "End-of-Line Indicator"]
        template = write_template(self.root / "two.csv",
                                  [["#astudent", "5", "", "#"], ["#bexample", "6", "", "#"]],
                                  header=header, bom=False)
        with self.assertRaisesRegex(BrightspaceError, "Choose one grade item"):
            export_brightspace(self.root, "run_v001", template)
        summary = export_brightspace(self.root, "run_v001", template, grade_item="project 2")
        rows = list(csv.reader((self.output / GRADES_IMPORT_FILENAME)
                               .read_text(encoding="utf-8").splitlines()))
        self.assertEqual(rows[0], ["Username", ITEM, "End-of-Line Indicator"])
        self.assertEqual(rows[1], ["#astudent", "8", "#"])
        self.assertEqual(summary["export_rows_without_final_grade"], 0)

    def test_refuses_unsafe_exports(self):
        no_username = write_template(self.root / "a.csv", [["#0001", "", "#"]],
                                     header=["OrgDefinedId", ITEM, "End-of-Line Indicator"])
        with self.assertRaisesRegex(BrightspaceError, "no Username column"):
            export_brightspace(self.root, "run_v001", no_username)
        missing = write_template(self.root / "b.csv", TEMPLATE_ROWS[:2])
        with self.assertRaisesRegex(BrightspaceError, "not in the Brightspace export: Student_002"):
            export_brightspace(self.root, "run_v001", missing)
        other_max = write_template(
            self.root / "c.csv", [["#astudent", "", "#"], ["#bexample", "", "#"]],
            header=["Username", "Project 2 Points Grade <Numeric MaxPoints:100>",
                    "End-of-Line Indicator"])
        with self.assertRaisesRegex(BrightspaceError, "out of 100 points.*out of 10"):
            export_brightspace(self.root, "run_v001", other_max)
        duplicate = write_template(self.root / "d.csv", TEMPLATE_ROWS + [TEMPLATE_ROWS[0]])
        with self.assertRaisesRegex(BrightspaceError, "twice"):
            export_brightspace(self.root, "run_v001", duplicate)
        self.assertFalse((self.output / GRADES_IMPORT_FILENAME).exists())

    def test_cli_prints_no_student_names(self):
        template = write_template(self.root / "export.csv", TEMPLATE_ROWS)
        output = io.StringIO()
        with patch("sys.argv", ["ai_grader", "export-brightspace", str(self.root),
                                "--run", "run_v001", "--template", str(template)]), \
             patch("sys.stdout", output):
            main()
        text = output.getvalue()
        self.assertIn("Students with a final grade: 2", text)
        self.assertIn("without a final grade here (left out): 1", text)
        for name in ("Avery", "Blake", "astudent", "bexample", "Casey"):
            self.assertNotIn(name, text)

    def test_finalize_cli_with_template(self):
        template = write_template(self.root / "export.csv", TEMPLATE_ROWS)
        scores = self.root / "scores.csv"
        scores.write_text("student_id,total_instructor_score\nStudent_001,8\nStudent_002,10\n",
                          encoding="utf-8")
        output = io.StringIO()
        with patch("sys.argv", ["ai_grader", "finalize-grading", str(self.root), "--run",
                                "run_v001", "--scores-file", str(scores),
                                "--brightspace-template", str(template)]), \
             patch("sys.stdout", output):
            main()
        text = output.getvalue()
        self.assertIn("finalization_v002", text)
        self.assertIn("Student feedback files: 2 (one per student)", text)
        self.assertIn("Brightspace grades import CSV:", text)
        for name in ("Avery", "Blake", "astudent"):
            self.assertNotIn(name, text)
        self.assertTrue((self.output.parent / "finalization_v002" / GRADES_IMPORT_FILENAME)
                        .is_file())


if __name__ == "__main__":
    unittest.main()
