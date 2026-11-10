"""Itemized deductions in the student's comment. Synthetic data only; the model is mocked."""

import csv
import io
import json
import random
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_grader.brightspace import FINAL_CSV_FILENAME
from ai_grader.cli import main
from ai_grader.itemized_feedback import (
    LABEL_CACHE_FILENAME, deduction_items, deduction_labels, deductions_cell,
    display_points, fallback_label, itemized_comment, label_problem, parse_deductions_cell,
)
from ai_grader.reporting import generate_review_report
from ai_grader.rounding import apply_rounding


def parenthesized(text):
    """Every signed number in parentheses in a comment."""
    return [float(value.replace("−", "-"))
            for value in re.findall(r"\(([+\-−]?\d+(?:\.\d+)?)\)", text)]


class DisplayAndParsingTests(unittest.TestCase):
    def test_points_are_shown_in_half_point_steps(self):
        # The instructor's examples: 0.75 -> 1, 0.625 -> 0.5, 0.25 -> 0.5.
        cases = {0.75: 1.0, 0.625: 0.5, 0.25: 0.5, 0.125: 0.5, 0.375: 0.5, 1.25: 1.5,
                 1.125: 1.0, 2: 2.0, 0.5: 0.5, 3.75: 4.0, 10: 10.0}
        for points, shown in cases.items():
            with self.subTest(points=points):
                self.assertEqual(display_points(points), shown)

    def test_cell_round_trip(self):
        items = [{"points": 0.625}, {"points": 0.75}, {"points": 1}]
        cell = deductions_cell(items, ["No legend", "Brief discussion", "CSV missing"])
        self.assertEqual(cell, "No legend (-0.5); Brief discussion (-1); CSV missing (-1)")
        parsed, readable = parse_deductions_cell(cell)
        self.assertTrue(readable)
        self.assertEqual(parsed, [("No legend", -0.5), ("Brief discussion", -1.0),
                                  ("CSV missing", -1.0)])

    def test_parsing_accepts_hand_edits(self):
        parsed, readable = parse_deductions_cell(
            "Late plots (−2) ; bonus figure (+1);README thin ( 0.5 )")
        self.assertTrue(readable)
        self.assertEqual(parsed, [("Late plots", -2.0), ("bonus figure", 1.0),
                                  ("README thin", -0.5)])
        parsed, readable = parse_deductions_cell("Several small issues")
        self.assertFalse(readable)
        self.assertEqual(parsed, [("Several small issues", None)])


class CommentTests(unittest.TestCase):
    CELL = "Validation plot has no 45-degree line (-0.5); penalty table lacks violation (-1); README has no recommendation (-0.5)"

    def test_numbers_add_up_with_rounding_in_the_students_favor(self):
        # Raw deductions 0.625 + 0.75 + 0.25 = 1.625 -> 98.375, rounded up to 99.
        text, problem = itemized_comment(self.CELL, 99, 99, 99, 100, "Strong work overall.")
        self.assertIsNone(problem)
        self.assertEqual(text, (
            "Points lost: Validation plot has no 45-degree line (-0.5); penalty table lacks "
            "violation (-1); README has no recommendation (-0.5). Rounded up (+1). "
            "Grade: 99/100. Strong work overall."))
        self.assertAlmostEqual(100 + sum(parenthesized(text)), 99)

    def test_late_penalty_with_a_reason(self):
        text, _ = itemized_comment(self.CELL, 99, 79, 79, 100, "Good work.",
                                   adjustment_reason="Late submission.")
        self.assertIn("Rounded up (+1). Late submission (-20). Grade: 79/100. Good work.", text)
        self.assertAlmostEqual(100 + sum(parenthesized(text)), 79)

    def test_instructor_change_without_a_reason(self):
        text, _ = itemized_comment(self.CELL, 99, 100, 100, 100, "")
        self.assertIn("Instructor adjustment (+1). Grade: 100/100.", text)
        self.assertTrue(text.endswith("Grade: 100/100."))
        self.assertAlmostEqual(100 + sum(parenthesized(text)), 100)

    def test_curve_after_the_instructor_grade(self):
        text, _ = itemized_comment(self.CELL, 99, 99, 100, 100, "Nice.")
        self.assertIn("Rounded up (+1). Final adjustment (+1). Grade: 100/100.", text)

    def test_full_marks_keep_the_comment(self):
        self.assertEqual(itemized_comment("", 100, 100, 100, 100, "Excellent work."),
                         ("Excellent work.", None))
        text, _ = itemized_comment("", 100, 80, 80, 100, "Excellent work.",
                                   adjustment_reason="Late submission")
        self.assertEqual(text, "Late submission (-20). Grade: 80/100. Excellent work.")

    def test_points_lost_but_nothing_itemized_keeps_the_comment(self):
        self.assertEqual(itemized_comment("", 82, 82, 82, 100, "Solid model."),
                         ("Solid model.", None))

    def test_unreadable_item_is_kept_without_the_balancing_line(self):
        text, problem = itemized_comment("Several small issues", 97, 97, 97, 100, "Ok.")
        self.assertEqual(text, "Points lost: Several small issues. Grade: 97/100. Ok.")
        self.assertIsNotNone(problem)

    def test_random_runs_always_add_up(self):
        generator = random.Random(7)
        steps = [0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 1, 1.25, 1.5, 2, 3]
        for _ in range(500):
            task_deductions = [[generator.choice(steps) for _ in range(generator.randint(0, 4))]
                               for _ in range(5)]
            raw = [20 - sum(points) for points in task_deductions]
            rounded = apply_rounding(raw, [20] * 5, 100, "generous-v1")["final_rounded_grade"]
            items = [{"points": p} for points in task_deductions for p in points]
            cell = deductions_cell(items, ["Issue"] * len(items))
            late = generator.choice([0, 0, -20, -5])
            approved = max(0, rounded + late)
            text, problem = itemized_comment(cell, rounded, approved, approved, 100, "C.",
                                             "Late submission" if late else "")
            self.assertIsNone(problem)
            if items or late:
                self.assertAlmostEqual(100 + sum(parenthesized(text)), approved, places=6,
                                       msg=text)
                self.assertTrue(all(abs(v * 2 - round(v * 2)) < 1e-9
                                    for v in parenthesized(text)[:len(items)]), text)


class LabelTests(unittest.TestCase):
    def test_label_checks(self):
        self.assertIsNone(label_problem("README has no final recommendation"))
        for bad in ["", "a\nb", "word " * 16, "Rubric item T2 missing", "artifact_003 missing",
                    "Needs instructor review", "No legend (plot)", "a; b"]:
            with self.subTest(bad=bad):
                self.assertIsNotNone(label_problem(bad))

    def test_fallback_uses_the_rule_feedback(self):
        item = {"task": "Task II", "feedback": "Include a 45-degree reference line. Label axes.",
                "reason": "artifact_004 lacks the line"}
        self.assertEqual(fallback_label(item), "Include a 45-degree reference line")
        self.assertEqual(fallback_label({"task": "Task II", "feedback": "", "reason": ""}),
                         "Task II incomplete")


SPEC = {
    "schema_version": "1.0", "spec_version": "1", "status": "approved",
    "project": {"project_id": "demo", "title": "Demo Project", "total_points": 100},
    "sources": [{"source_id": "published", "source_type": "project_instructions",
                 "path": "project/instructions.md"}],
    "deliverables": [], "known_ambiguities": [],
    "tasks": [
        {"task_id": "T1", "title": "Model", "max_points": 60, "criteria": [
            {"criterion_id": "C1", "max_points": 60, "description": "Model",
             "requirement_type": "technical_correctness", "full_credit_condition": "Done.",
             "deduction_rules": [
                 {"rule_id": "R1", "condition": "Plot lacks the reference line.",
                  "deduction_points": 0.625, "basis": "technical",
                  "feedback_template": "Add a 45-degree reference line to the validation plot."},
                 {"rule_id": "R2", "condition": "Discussion is brief.",
                  "deduction_points": 0.75, "basis": "technical",
                  "feedback_template": "Discuss the validation results."}]}]},
        {"task_id": "T2", "title": "Report", "max_points": 40, "criteria": [
            {"criterion_id": "C2", "max_points": 40, "description": "Report",
             "requirement_type": "technical_correctness", "full_credit_condition": "Done.",
             "deduction_rules": [
                 {"rule_id": "R3", "condition": "README lacks recommendation.",
                  "deduction_points": 0.25, "basis": "technical",
                  "feedback_template": "State the final recommendation in the README."}]}]},
    ],
}
NAMES = {"Student_001": ("Avery Sample", "asample"), "Student_002": ("Blake Example", "bexample")}


def make_project(root):
    grader = root / "grader"
    run_path = grader / "grading_runs" / "run_v001"
    run_path.mkdir(parents=True)
    (grader / "grading_spec_v001.json").write_text(json.dumps(SPEC), encoding="utf-8")
    (grader / "student_map.json").write_text(json.dumps({
        sid: {"full_name": name, "username": user} for sid, (name, user) in NAMES.items()}),
        encoding="utf-8")
    deductions_one = [
        {"rule_id": "R1", "points": 0.625, "reason": "artifact_004 lacks the 45-degree line.",
         "deduction_type": "demonstrated_technical_error", "cause_id": "c1",
         "independent_requirement": False},
        {"rule_id": "R2", "points": 0.75, "reason": "The discussion is two sentences.",
         "deduction_type": "inadequate_required_evidence", "cause_id": "c2",
         "independent_requirement": False}]
    deductions_two = [
        {"rule_id": "R3", "points": 0.25, "reason": "No recommendation in the README.",
         "deduction_type": "missing_required_documentation", "cause_id": "c3",
         "independent_requirement": False}]
    results = [
        {"student_id": "Student_001", "total_agent_score": 99, "tasks": [
            {"task_id": "T1", "title": "Model", "agent_score": 59, "max_points": 60,
             "feedback": "Good model.", "criteria": [
                 {"criterion_id": "C1", "agent_score": 58.625, "deductions": deductions_one}]},
            {"task_id": "T2", "title": "Report", "agent_score": 40, "max_points": 40,
             "feedback": "Clear report.", "criteria": [
                 {"criterion_id": "C2", "agent_score": 40, "deductions": []}]}]},
        {"student_id": "Student_002", "total_agent_score": 100, "tasks": [
            {"task_id": "T1", "title": "Model", "agent_score": 60, "max_points": 60,
             "feedback": "Good model.", "criteria": [
                 {"criterion_id": "C1", "agent_score": 60, "deductions": []}]},
            {"task_id": "T2", "title": "Report", "agent_score": 40, "max_points": 40,
             "feedback": "Clear report.", "criteria": [
                 {"criterion_id": "C2", "agent_score": 39.75, "deductions": deductions_two}]}]},
    ]
    run = {"results": results, "rounding_policy": {"identifier": "generous-v1"},
           "grading_spec": "grading_spec_v001.json"}
    (run_path / "grading_results.json").write_text(json.dumps(run), encoding="utf-8")
    rows = []
    for result in results:
        rows.append({"student_id": result["student_id"],
                     "total_agent_score": result["total_agent_score"], "maximum_score": 100,
                     "total_instructor_score": "", "validation_status": "validated",
                     "task_1_score": result["tasks"][0]["agent_score"],
                     "task_1_feedback": "Good model.",
                     "task_2_score": result["tasks"][1]["agent_score"],
                     "task_2_feedback": "Clear report.",
                     "review_required": "false", "review_reasons": ""})
    with (run_path / "preliminary_grading_report.csv").open("w", encoding="utf-8",
                                                            newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return run_path


class FakeModel:
    def __init__(self, labels=None):
        self.labels = labels or {}
        self.calls = []
        self.responses = SimpleNamespace(create=self.create)

    def create(self, **request):
        name = request["text"]["format"]["name"]
        payload = json.loads(request["input"])
        self.calls.append((name, payload))
        if name == "deduction_labels":
            text = json.dumps({"students": [
                {"student_id": student["student_id"], "labels": [
                    {"index": item["index"],
                     "label": self.labels.get((student["student_id"], item["index"]),
                                              f"Issue in {item['task']}")}
                    for item in student["deductions"]]}
                for student in payload["students"]]})
        else:
            text = json.dumps({"comments": [
                {"student_id": item["student_id"], "comment": "Solid work; keep it up."}
                for item in payload["students"]]})
        return SimpleNamespace(output_text=text,
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def read_rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_rows(path, rows):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


class ReviewSheetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.run_path = make_project(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_labels_are_anonymous_cached_and_checked(self):
        run = json.loads((self.run_path / "grading_results.json").read_text())
        items = {r["student_id"]: deduction_items(r, SPEC) for r in run["results"]}
        model = FakeModel(labels={("Student_001", 0): "Validation plot has no 45-degree line",
                                  ("Student_001", 1): "Rubric R2 not met"})
        labels, fallbacks, _ = deduction_labels(self.run_path, items, model, "m")
        self.assertEqual(labels["Student_001"], [
            "Validation plot has no 45-degree line", "Discuss the validation results"])
        self.assertEqual(fallbacks, {"mentions rubric": 1})
        sent = json.dumps(model.calls)
        self.assertNotIn("Avery", sent)
        self.assertNotIn("points", sent)        # The model never sees the points.
        self.assertTrue((self.run_path / LABEL_CACHE_FILENAME).is_file())
        again, _, _ = deduction_labels(self.run_path, items,
                                       SimpleNamespace(responses=None), "m")
        self.assertEqual(again, labels)          # Cached: no model call.

    def test_review_file_has_deductions_and_keeps_earlier_entries(self):
        model = FakeModel(labels={("Student_001", 0): "Validation plot has no 45-degree line",
                                  ("Student_001", 1): "Validation discussion is brief",
                                  ("Student_002", 0): "README has no recommendation"})
        first = generate_review_report(self.root, "run_v001", use_model_comments=True,
                                       client=model, model="m")
        rows = read_rows(first)
        self.assertEqual(list(rows[0])[:8], [
            "student_name", "student_id", "total_agent_score", "total_instructor_score",
            "flag", "final_comment", "deductions", "adjustment_reason"])
        self.assertEqual(rows[0]["deductions"],
                         "Validation plot has no 45-degree line (-0.5); "
                         "Validation discussion is brief (-1)")
        self.assertEqual(rows[1]["deductions"], "README has no recommendation (-0.5)")

        rows[0]["total_instructor_score"] = "79"
        rows[0]["adjustment_reason"] = "Late submission"
        rows[1]["total_instructor_score"] = "100"
        rows[1]["final_comment"] = "Edited by the instructor."
        write_rows(first, rows)
        stats = {}
        second = generate_review_report(self.root, "run_v001", use_model_comments=True,
                                        client=model, model="m", comment_stats=stats)
        kept = read_rows(second)
        self.assertEqual(second.name, "instructor_review_v002.csv")
        self.assertEqual([row["total_instructor_score"] for row in kept], ["79", "100"])
        self.assertEqual(kept[0]["adjustment_reason"], "Late submission")
        self.assertEqual(kept[0]["final_comment"], "Solid work; keep it up.")
        self.assertEqual(kept[1]["final_comment"], "Edited by the instructor.")
        self.assertEqual((stats["carried_over"], stats["carried_from"]),
                         (2, "instructor_review_v001.csv"))

    def test_finalized_comment_lists_every_point(self):
        model = FakeModel(labels={("Student_001", 0): "Validation plot has no 45-degree line",
                                  ("Student_001", 1): "Validation discussion is brief",
                                  ("Student_002", 0): "README has no recommendation"})
        path = generate_review_report(self.root, "run_v001", use_model_comments=True,
                                      client=model, model="m")
        rows = read_rows(path)
        rows[0]["total_instructor_score"] = "79"
        rows[0]["adjustment_reason"] = "Late submission"
        rows[1]["total_instructor_score"] = "100"
        write_rows(path, rows)
        output = io.StringIO()
        with patch("sys.argv", ["ai_grader", "finalize-grading", str(self.root), "--run",
                                "run_v001", "--scores-file", str(path)]), \
             patch("sys.stdout", output):
            main()
        final_dir = self.root / "grader" / "finalizations" / "run_v001" / "finalization_v001"
        final = {row["Username"]: row for row in read_rows(final_dir / FINAL_CSV_FILENAME)}
        self.assertEqual(final["asample"]["Comment"], (
            "Points lost: Validation plot has no 45-degree line (-0.5); Validation discussion "
            "is brief (-1). Rounded up (+0.5). Late submission (-20). Grade: 79/100. "
            "Solid work; keep it up."))
        self.assertEqual(final["bexample"]["Comment"], (
            "Points lost: README has no recommendation (-0.5). Rounded up (+0.5). "
            "Grade: 100/100. Solid work; keep it up."))
        for row in final.values():
            self.assertAlmostEqual(100 + sum(parenthesized(row["Comment"])),
                                   float(row[next(k for k in row if "Grade" in k)]))
        self.assertNotIn("couldn't read", output.getvalue())


if __name__ == "__main__":
    unittest.main()
