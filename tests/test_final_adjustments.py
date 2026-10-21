"""Final grade adjustments: instructions -> plan -> review rounds -> final CSV.
Synthetic data only; the model is mocked."""

import copy
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_grader.adjustments import (
    AdjustmentError, anonymize_instructions, apply_plan, compose_comment, empty_plan,
    validate_plan,
)
from ai_grader.brightspace import FINAL_CSV_FILENAME, export_brightspace
from ai_grader.cli import main
from ai_grader.finalization import finalize_grading
from ai_grader.grade_adjustment import (
    COMMENT_CACHE_FILENAME, adjust_grades, adjusted_review_decisions, comment_problem,
    sidecar_path, template_comment,
)
from ai_grader.reporting import generate_review_report


SCORES = [68, 75, 80, 82, 85, 88, 90, 91, 92, 99, 100]
FIRST = ["Avery", "Blake", "Dana", "Emery", "Finley", "Gray", "Harper", "Indy",
         "Jules", "Kai", "Lane"]
STUDENTS = {
    f"Student_{index:03d}": (f"{first} Sample{chr(64 + index)}", f"user{index:02d}", score)
    for index, (first, score) in enumerate(zip(FIRST, SCORES), start=1)
}
DANA = "Student_003"
INSTRUCTIONS = (
    "Curve the grades. The lowest grade should be 75. The average should be at least 90. "
    "Dana's grade should be at least 85, and curve the rest accordingly. Students with 99 "
    "should get 100 and 'very good' in the comment. Students with 100 get 'excellent!'.")


def split(score):
    first = min(60, round(score * 0.6))
    return first, score - first


def make_project(root):
    grader = root / "grader"
    run_path = grader / "grading_runs" / "run_v001"
    run_path.mkdir(parents=True)
    criterion = {"description": "Work", "requirement_type": "technical_correctness",
                 "full_credit_condition": "Complete work."}
    spec = {
        "schema_version": "1.0", "spec_version": "1", "status": "approved",
        "project": {"project_id": "demo", "title": "Demo Project", "total_points": 100},
        "sources": [{"source_id": "published", "source_type": "project_instructions",
                     "path": "project/instructions.md"}],
        "deliverables": [], "known_ambiguities": [],
        "tasks": [
            {"task_id": "T1", "title": "Beam Model", "max_points": 60,
             "criteria": [{"criterion_id": "C1", "max_points": 60, **criterion}]},
            {"task_id": "T2", "title": "Written Report", "max_points": 40,
             "criteria": [{"criterion_id": "C2", "max_points": 40, **criterion}]},
        ],
    }
    (grader / "grading_spec_v001.json").write_text(json.dumps(spec), encoding="utf-8")
    (grader / "student_map.json").write_text(json.dumps({
        sid: {"full_name": name, "username": username}
        for sid, (name, username, _) in STUDENTS.items()}), encoding="utf-8")
    results, rows = [], []
    for sid, (_, _, score) in STUDENTS.items():
        one, two = split(score)
        results.append({"student_id": sid, "total_agent_score": score, "tasks": [
            {"task_id": "T1", "title": "Beam Model", "agent_score": one, "max_points": 60,
             "feedback": "Model feedback.", "criteria": [{"criterion_id": "C1", "agent_score": one}]},
            {"task_id": "T2", "title": "Written Report", "agent_score": two, "max_points": 40,
             "feedback": "Report feedback.", "criteria": [{"criterion_id": "C2", "agent_score": two}]},
        ]})
        rows.append({"student_id": sid, "total_agent_score": score, "maximum_score": 100,
                     "raw_total_before_rounding": score, "total_rounding_adjustment": 0,
                     "total_instructor_score": "", "validation_status": "validated",
                     "task_1_score": one, "task_1_max_points": 60,
                     "task_1_feedback": "Model feedback.",
                     "task_2_score": two, "task_2_max_points": 40,
                     "task_2_feedback": "Report feedback.",
                     "review_required": "false", "review_reasons": ""})
    (run_path / "grading_results.json").write_text(json.dumps({"results": results}),
                                                   encoding="utf-8")
    with (run_path / "preliminary_grading_report.csv").open("w", encoding="utf-8",
                                                            newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return run_path


def target_plan():
    plan = empty_plan()
    plan["summary"] = ["Curve so the lowest is 75, the mean 90 and Student_003 at least 85.",
                       "99 becomes 100.", "Openers for 99 and 100."]
    plan["curve"].update(method="scale", solve_for_targets=True)
    plan["targets"].update(min_grade=75, mean=90,
                           student_floors=[{"student_id": DANA, "at_least": 85}])
    plan["score_mappings"] = [{"from_min": 99, "from_max": 99, "to": 100}]
    plan["comment_rules"] = [
        {"from_min": 100, "from_max": 100, "based_on": "before", "opener": "Excellent!"},
        {"from_min": 99, "from_max": 99, "based_on": "before", "opener": "Very good work!"}]
    return plan


class FakeModel:
    """Mocked Responses API: plans come from a queue; comments are generated."""

    def __init__(self, plans=(), comment=None):
        self.plans = list(plans)
        self.comment = comment or (lambda sid: f"You did solid work; focus on clearer reports.")
        self.calls = []
        self.responses = SimpleNamespace(create=self.create)

    def create(self, **request):
        name = request["text"]["format"]["name"]
        payload = json.loads(request["input"])
        self.calls.append((name, payload))
        if name == "grade_adjustment_plan":
            text = json.dumps(self.plans.pop(0))
        else:
            text = json.dumps({"comments": [
                {"student_id": item["student_id"], "comment": self.comment(item["student_id"])}
                for item in payload["students"]]})
        return SimpleNamespace(output_text=text,
                               usage=SimpleNamespace(input_tokens=100, output_tokens=20))

    def names(self, kind):
        return [payload for name, payload in self.calls if name == kind]


def read_rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def write_rows(path, rows):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


class EngineTests(unittest.TestCase):
    def test_targets_solved_with_the_smallest_order_preserving_curve(self):
        base = {f"Student_{i:03d}": float(s) for i, s in enumerate(SCORES, start=1)}
        plan = validate_plan(target_plan(), base, 100)
        result = apply_plan(base, plan, 100)
        self.assertTrue(all(check["met"] for check in result["checks"]))
        grades = {s["student_id"]: s["adjusted_grade"] for s in result["students"]}
        self.assertEqual((grades["Student_001"], grades[DANA], grades["Student_011"]),
                         (76, 85, 100))
        self.assertEqual(result["after"]["mean"], 90)
        self.assertTrue(result["equation"].startswith("new = 0.7778 x old + 22.22"))
        ordered = sorted(base, key=base.get)
        self.assertEqual([grades[s] for s in ordered], sorted(grades.values()))
        smaller = copy.deepcopy(plan)
        smaller["curve"].update(solve_for_targets=False, fraction=result["parameter"] - 1e-4)
        self.assertFalse(all(c["met"] for c in apply_plan(base, smaller, 100)["checks"]))
        openers = {s["student_id"]: s["opener"] for s in result["students"]}
        self.assertEqual((openers["Student_010"], openers["Student_011"], openers[DANA]),
                         ("Very good work!", "Excellent!", ""))

    def test_add_linear_overrides_and_median(self):
        base = {"Student_001": 60.0, "Student_002": 80.0, "Student_003": 97.0}
        plan = empty_plan()
        plan["curve"].update(method="add", solve_for_targets=True)
        plan["targets"]["median"] = 85
        result = apply_plan(base, validate_plan(plan, base, 100), 100)
        self.assertEqual(result["parameter"], 5)
        self.assertEqual([s["adjusted_grade"] for s in result["students"]], [65, 85, 100])
        plan = empty_plan()
        plan["curve"].update(method="linear", slope=0.5, intercept=50)
        plan["student_overrides"] = [{"student_id": "Student_002", "set_grade": 70,
                                      "add_points": None, "relative_to": "after", "reason": "Academic integrity case"}]
        result = apply_plan(base, validate_plan(plan, base, 100), 100)
        self.assertEqual([(s["adjusted_grade"], s["set_by"]) for s in result["students"]],
                         [(80, "curve"), (70, "override"), (99, "curve")])

    def test_outlier_handled_separately(self):
        values = [93, 77, 80, 96, 99, 95, 100, 89, 38, 99, 68]
        base = {f"Student_{i:03d}": float(v) for i, v in enumerate(values, start=1)}
        plan = empty_plan()
        plan["curve"].update(method="scale", solve_for_targets=True)
        plan["targets"].update(min_grade=75, mean=90, exclude=["Student_009"],
                               student_floors=[{"student_id": "Student_003", "at_least": 85}])
        plan["student_overrides"] = [{"student_id": "Student_009", "set_grade": None,
                                      "add_points": 10, "relative_to": "before",
                                      "reason": "Outlier; proportional raise"}]
        result = apply_plan(base, validate_plan(plan, base, 100), 100)
        grades = {s["student_id"]: s["adjusted_grade"] for s in result["students"]}
        self.assertEqual(grades["Student_009"], 48)              # 38 + 10, not curved.
        self.assertGreaterEqual(grades["Student_011"], 75)
        labels = [c["target"] for c in result["checks"]]
        self.assertIn("lowest grade (without Student_009)", labels)
        self.assertIn("class mean", labels)                      # Mean still counts everyone.
        self.assertGreaterEqual(sum(grades.values()) / 11, 90)
        plan["targets"]["exclude_from_mean"] = True
        smaller = apply_plan(base, validate_plan(plan, base, 100), 100)
        self.assertLess(smaller["parameter"], result["parameter"])
        self.assertIn("class mean (without Student_009)",
                      [c["target"] for c in smaller["checks"]])
        # "after" adds on top of the curve instead.
        plan["student_overrides"][0]["relative_to"] = "after"
        after = apply_plan(base, validate_plan(plan, base, 100), 100)
        self.assertGreater(after["students"][8]["adjusted_grade"], 48)

    def test_large_changes_are_flagged(self):
        values = [93, 77, 80, 96, 99, 95, 100, 89, 38, 99, 68]
        base = {f"Student_{i:03d}": float(v) for i, v in enumerate(values, start=1)}
        plan = empty_plan()
        plan["curve"].update(method="scale", fraction=0.2)
        plan["student_overrides"] = [{"student_id": "Student_009", "set_grade": 75,
                                      "add_points": None, "relative_to": "after",
                                      "reason": "Class minimum"},
                                     {"student_id": "Student_004", "set_grade": 90,
                                      "add_points": None, "relative_to": "after",
                                      "reason": "Lowered for a policy reason"}]
        flagged = apply_plan(base, validate_plan(plan, base, 100), 100)["attention"]
        self.assertEqual([(f["student_id"], f["change"], f["reason"]) for f in flagged],
                         [("Student_004", -6, "grade lowered"),
                          ("Student_009", 37, "typical raise is +3")])

    def test_plans_saved_before_new_fields_still_work(self):
        from ai_grader.adjustments import upgrade_plan
        plan = empty_plan()
        del plan["targets"]["exclude"], plan["targets"]["exclude_from_mean"]
        plan["student_overrides"] = [{"student_id": "Student_001", "set_grade": None,
                                      "add_points": 2, "reason": "Old plan"}]
        plan = upgrade_plan(plan)
        validate_plan(plan, {"Student_001": 50.0}, 100)
        self.assertEqual(plan["student_overrides"][0]["relative_to"], "after")

    def test_invalid_plans_are_refused(self):
        base = {"Student_001": 60.0}
        for change, message in [
                (lambda p: p["curve"].update(method="linear", solve_for_targets=True),
                 "Only an add or scale"),
                (lambda p: p["targets"]["student_floors"].append(
                    {"student_id": "Student_099", "at_least": 80}), "unknown student"),
                (lambda p: p["score_mappings"].append({"from_min": 90, "from_max": 95, "to": 80}),
                 "lower grades"),
                (lambda p: p["targets"].update(mean=120), "outside 0-100"),
                (lambda p: p["student_overrides"].append(
                    {"student_id": "Student_001", "set_grade": 70, "add_points": None, "relative_to": "after",
                     "reason": " "}), "needs a reason"),
                (lambda p: p.pop("rounding"), "malformed")]:
            plan = empty_plan()
            change(plan)
            with self.assertRaisesRegex(AdjustmentError, message):
                validate_plan(plan, base, 100)
        plan = empty_plan()
        plan["curve"].update(method="scale", solve_for_targets=True)
        plan["targets"]["min_grade"] = 75
        plan["student_overrides"] = [{"student_id": "Student_001", "set_grade": 50,
                                      "add_points": None, "relative_to": "after", "reason": "Instructor decision"}]
        with self.assertRaisesRegex(AdjustmentError, "No curve of this kind"):
            apply_plan(base, validate_plan(plan, base, 100), 100)

    def test_names_become_ids_before_the_model(self):
        students = [("Student_001", "Dana Sample", "dsample"),
                    ("Student_002", "Will Gray", "wgray"),
                    ("Student_003", "Ana Lee", "alee"), ("Student_004", "Ana Park", "apark")]
        text, readings = anonymize_instructions(
            "Danna should get 85. Students will get more. WGRAY +2. ana park too.", students)
        self.assertEqual(text, "Student_001 should get 85. Students will get more. "
                               "Student_002 +2. Student_004 too.")
        self.assertIn({"student_id": "Student_001", "match": "closest"}, readings)
        with self.assertRaisesRegex(AdjustmentError, "matches 2 students"):
            anonymize_instructions("Ana should be 90.", students)

    def test_comments(self):
        tasks = [{"title": "Beam Model", "score": 50, "max_points": 60},
                 {"title": "Written Report", "score": 30, "max_points": 40}]
        self.assertEqual(template_comment(tasks), "Your strongest work was in Beam Model. "
                                                  "Most points were lost in Written Report.")
        tasks[0]["score"], tasks[1]["score"] = 60, 40
        self.assertEqual(template_comment(tasks),
                         "Every part of the assignment met the requirements.")
        self.assertEqual(compose_comment("Good.", "Excellent!", [
            {"text": "See me.", "replace": False}]), "Excellent! Good. See me.")
        self.assertEqual(compose_comment("Good.", "Excellent!", [
            {"text": "Mine.", "replace": True}]), "Mine.")
        self.assertIsNone(comment_problem("You modeled the beam well."))
        # Assignments about AI agents or statistics may use these words.
        self.assertIsNone(comment_problem(
            "Your agent loop rejected invalid proposals; report a confidence interval."))
        self.assertEqual(comment_problem("The grading agent found gaps."),
                         "mentions grading agent")
        self.assertEqual(comment_problem("Student_004 did well."), "mentions student_")
        self.assertEqual(comment_problem("word " * 61), "too long")


class RoundTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.run_path = make_project(self.root)
        self.v001 = generate_review_report(self.root, "run_v001")

    def adjust(self, model, instructions=INSTRUCTIONS, **options):
        return adjust_grades(self.root, "run_v001", instructions=instructions,
                             client=model, model="test-model", **options)

    def test_round_one_writes_v002_with_grades_and_comments(self):
        model = FakeModel([target_plan()])
        summary = self.adjust(model)
        self.assertEqual(summary["path"].name, "instructor_review_v002.csv")
        sent = json.dumps(model.calls)
        for name in FIRST + ["Sample", "user0"]:
            self.assertNotIn(name, sent)                       # Anonymous input only.
        request = model.names("grade_adjustment_plan")[0]
        self.assertIn(DANA, request["new_instructions"])
        self.assertIsNone(request["previous_plan"])
        rows = {row["student_id"]: row for row in read_rows(summary["path"])}
        self.assertEqual(list(read_rows(summary["path"])[0])[:8], [
            "student_name", "student_id", "total_agent_score", "total_instructor_score",
            "adjusted_grade", "adjustment", "adjusted_by", "final_comment"])
        self.assertEqual((rows["Student_001"]["total_agent_score"],
                          rows["Student_001"]["adjusted_grade"],
                          rows["Student_001"]["adjustment"]), ("68", "76", "+8"))
        self.assertEqual(rows[DANA]["adjusted_grade"], "85")
        self.assertTrue(rows["Student_011"]["final_comment"].startswith("Excellent! You did"))
        self.assertTrue(rows["Student_010"]["final_comment"].startswith("Very good work! "))
        self.assertEqual(rows["Student_001"]["total_instructor_score"], "")   # Not approved.
        record = json.loads(sidecar_path(summary["path"]).read_text(encoding="utf-8"))
        self.assertEqual(record["instruction_history"][0]["instructions"], INSTRUCTIONS)
        self.assertEqual(record["comments"]["ai"], 11)
        self.assertEqual(len(read_rows(self.v001)[0]), len(read_rows(self.v001)[0]))
        self.assertNotIn("adjusted_grade", read_rows(self.v001)[0])     # v001 untouched.
        for rounding_detail in ("raw_total_before_rounding", "total_rounding_adjustment"):
            self.assertNotIn(rounding_detail, read_rows(self.v001)[0])
            self.assertNotIn(rounding_detail, read_rows(summary["path"])[0])

    def test_later_rounds_revise_the_plan_and_keep_hand_edits(self):
        first = self.adjust(FakeModel([target_plan()]))
        rows = read_rows(first["path"])
        for row in rows:
            if row["student_id"] == "Student_005":
                row["adjusted_grade"] = "92"
            if row["student_id"] == "Student_006":
                row["final_comment"] = "Great teamwork on the report."
            if row["student_id"] == "Student_002":
                row["total_instructor_score"] = "78"        # A changed itemized total.
        write_rows(first["path"], rows)

        revised = target_plan()
        revised["targets"]["mean"] = 91
        model = FakeModel([revised])
        second = self.adjust(model, "Make the average at least 91.")
        self.assertEqual(second["path"].name, "instructor_review_v003.csv")
        request = model.names("grade_adjustment_plan")[0]
        self.assertIn(f"{DANA}'s grade should be at least 85",
                      request["previous_instructions"][0])
        overrides = request["previous_plan"]["student_overrides"]
        self.assertEqual([(o["student_id"], o["set_grade"]) for o in overrides],
                         [("Student_005", 92)])
        self.assertEqual(model.names("short_comments"), [])     # Cached from round one.
        # The model's revised plan left the hand edits out; they are kept anyway.
        rows = {row["student_id"]: row for row in read_rows(second["path"])}
        self.assertEqual((rows["Student_005"]["adjusted_grade"],
                          rows["Student_005"]["adjusted_by"]), ("92", "override"))
        self.assertEqual(rows["Student_006"]["final_comment"], "Great teamwork on the report.")
        self.assertEqual((rows["Student_002"]["total_agent_score"],
                          rows["Student_002"]["total_instructor_score"]), ("75", "78"))
        record = json.loads(sidecar_path(second["path"]).read_text(encoding="utf-8"))
        written = {item["student_id"]: item for item in record["written"]}
        self.assertEqual((written["Student_002"]["grade_before_adjustment"],
                          written["Student_002"]["grade_source"]), (78, "instructor"))
        self.assertGreaterEqual(second["after"]["mean"], 91)
        self.assertEqual(second["hand_edits"], {"grades": ["Student_005"],
                                                "comments": ["Student_006"]})

    def test_model_comments_failing_checks_use_the_template(self):
        model = FakeModel([target_plan()],
                          comment=lambda sid: "Student_001 is great" if sid == "Student_001"
                          else "You did well.")
        summary = self.adjust(model)
        self.assertEqual(summary["comments"]["fallbacks"], 1)
        self.assertEqual(summary["comments"]["fallback_reasons"], {"mentions student_": 1})
        rows = {row["student_id"]: row for row in read_rows(summary["path"])}
        self.assertIn("Your strongest work was in", rows["Student_001"]["final_comment"])
        cache = json.loads((self.run_path / COMMENT_CACHE_FILENAME).read_text(encoding="utf-8"))
        self.assertNotIn("Student_001", cache["comments"])     # Retried next round.

    def test_refusals(self):
        with self.assertRaisesRegex(AdjustmentError, "not valid JSON"):
            model = FakeModel()
            model.responses = SimpleNamespace(create=lambda **_: SimpleNamespace(
                output_text="not json", usage=None))
            self.adjust(model)
        bad = target_plan()
        bad["targets"]["student_floors"][0]["student_id"] = "Student_099"
        with self.assertRaisesRegex(AdjustmentError, "unknown student"):
            self.adjust(FakeModel([bad]))
        rows = read_rows(self.v001)
        rows[0]["validation_status"] = "invalid_model_response"
        write_rows(self.v001, rows)
        with self.assertRaisesRegex(AdjustmentError, "Student_001 has no valid AI score"):
            self.adjust(FakeModel([target_plan()]))
        self.assertEqual(len(list(self.run_path.glob("instructor_review_v*.csv"))), 1)

    def test_finalize_from_the_adjusted_review(self):
        summary = self.adjust(FakeModel([target_plan()]))
        rows = read_rows(summary["path"])
        for row in rows:
            if row["student_id"] == "Student_004":
                row["final_comment"] = "Solid model; tighten the report."
        write_rows(summary["path"], rows)
        approved, decisions = adjusted_review_decisions(summary["path"])
        self.assertEqual(approved["Student_001"], 68)
        self.assertTrue(decisions["Student_004"]["comment_edited_by_hand"])
        output, csv_path, _, validation = finalize_grading(
            self.root, "run_v001", approved, decisions=decisions)
        self.assertTrue(validation["csv_txt_grades_agree"])
        final = {row["Username"]: row for row in read_rows(output / FINAL_CSV_FILENAME)}
        self.assertEqual(list(read_rows(output / FINAL_CSV_FILENAME)[0]),
                         ["Student Name", "Username", "Grade", "Comment"])
        self.assertEqual(final["user01"]["Student Name"], "Avery SampleA")
        self.assertEqual((final["user01"]["Grade"], final["user03"]["Grade"],
                          final["user10"]["Grade"]), ("76", "85", "100"))
        self.assertEqual(final["user04"]["Comment"], "Solid model; tighten the report.")
        self.assertTrue(final["user11"]["Comment"].startswith("Excellent!"))
        summary_rows = {r["anonymous_student_id"]: r for r in read_rows(csv_path)}
        self.assertEqual((summary_rows["Student_001"]["final_instructor_score"],
                          summary_rows["Student_001"]["final_adjustment"],
                          summary_rows["Student_001"]["final_grade"]), ("68.0", "8.0", "76.0"))
        text = next(output.glob("Avery*_feedback.txt")).read_text(encoding="utf-8")
        self.assertIn("Final Grade: 76/100\nGrade before adjustment: 68/100 (adjustment: +8)",
                      text)
        self.assertEqual(text.count("Final Grade: 76/100"), 2)
        template = self.root / "export.csv"
        template.write_text("Username,Project Points Grade <Numeric MaxPoints:100>,"
                            "End-of-Line Indicator\n#user01,,#\n" + "".join(
                                f"#user{i:02d},,#\n" for i in range(2, 12)), encoding="utf-8")
        exported = export_brightspace(self.root, "run_v001", template)
        self.assertIn("#user01,76,#", exported["path"].read_text(encoding="utf-8"))

        rows = read_rows(summary["path"])
        rows[0]["total_instructor_score"] = "70"
        write_rows(summary["path"], rows)
        with self.assertRaisesRegex(AdjustmentError, "changed after the adjustment"):
            adjusted_review_decisions(summary["path"])

    def test_instructor_totals_that_differ_from_the_task_scores(self):
        rows = read_rows(self.v001)
        for row in rows:
            if row["student_id"] == "Student_002":
                row["total_instructor_score"] = "79"      # AI itemized total is 75.
        write_rows(self.v001, rows)
        summary = self.adjust(FakeModel([target_plan()]))
        self.assertEqual(summary["instructor_totals"], 1)
        approved, decisions = adjusted_review_decisions(summary["path"])
        self.assertEqual(approved["Student_002"], 79)
        with self.assertRaisesRegex(ValueError, "--accept-instructor-totals"):
            finalize_grading(self.root, "run_v001", approved, decisions=decisions)
        self.assertFalse((self.root / "grader" / "finalizations").exists())
        output, csv_path, _, validation = finalize_grading(
            self.root, "run_v001", approved, decisions=decisions,
            accept_instructor_totals=True)
        self.assertTrue(validation["task_totals_match_final_scores"])
        row = next(r for r in read_rows(csv_path) if r["anonymous_student_id"] == "Student_002")
        self.assertEqual((row["final_instructor_score"], row["instructor_total_adjustment"]),
                         ("79.0", "4.0"))
        text = next(output.glob("Blake*_feedback.txt")).read_text(encoding="utf-8")
        self.assertIn("Instructor adjustment to the task total: +4", text)
        self.assertIn("Grade before adjustment: 79/100", text)

    def test_internal_notes_in_feedback_are_located_and_editable(self):
        results_path = self.run_path / "grading_results.json"
        run = json.loads(results_path.read_text(encoding="utf-8"))
        run["results"][8]["tasks"][1]["feedback"] = ("Report incomplete; the notebook "
                                                     "could not be opened, needs instructor review.")
        results_path.write_text(json.dumps(run), encoding="utf-8")
        rows = read_rows(self.v001)             # The review file shows the same text.
        rows[8]["task_2_feedback"] = run["results"][8]["tasks"][1]["feedback"]
        write_rows(self.v001, rows)
        plan_file = self.root / "plan.json"
        plan_file.write_text(json.dumps(target_plan()), encoding="utf-8")
        with patch("sys.argv", ["ai_grader", "adjust-grades", str(self.root), "--run",
                                "run_v001", "--plan-file", str(plan_file),
                                "--template-comments"]), patch("sys.stdout", io.StringIO()):
            main()
        review = self.run_path / "instructor_review_v002.csv"
        argv = ["ai_grader", "finalize-grading", str(self.root), "--run", "run_v001",
                "--scores-file", str(review)]
        output = io.StringIO()
        with patch("sys.argv", argv), patch("sys.stdout", output), \
             self.assertRaises(SystemExit):
            main()
        self.assertIn("Student_009 task_2_feedback ('instructor review')", output.getvalue())
        rows = read_rows(review)
        for row in rows:
            if row["student_id"] == "Student_009":
                row["task_2_feedback"] = "Report incomplete; the notebook could not be opened."
        write_rows(review, rows)
        output = io.StringIO()
        with patch("sys.argv", argv), patch("sys.stdout", output):
            main()
        self.assertIn("Task feedback you edited in the review file: 1", output.getvalue())
        folder = next((self.root / "grader" / "finalizations" / "run_v001").iterdir())
        text = next(folder.glob("Jules*_feedback.txt")).read_text(encoding="utf-8")
        self.assertIn("Report incomplete; the notebook could not be opened.\n", text)

    def test_cli_round_and_finalize_without_names(self):
        plan_file = self.root / "plan.json"
        plan_file.write_text(json.dumps(target_plan()), encoding="utf-8")
        output = io.StringIO()
        with patch("sys.argv", ["ai_grader", "adjust-grades", str(self.root), "--run",
                                "run_v001", "--plan-file", str(plan_file),
                                "--template-comments"]), patch("sys.stdout", output):
            main()
        text = output.getvalue()
        self.assertIn("[met] class mean at least 90: 90", text)
        self.assertIn("Equation: new = 0.7778 x old + 22.22", text)
        self.assertIn("instructor_review_v002.csv", text)
        self.assertIn("finalize-grading", text)
        output = io.StringIO()
        review = self.run_path / "instructor_review_v002.csv"
        with patch("sys.argv", ["ai_grader", "finalize-grading", str(self.root), "--run",
                                "run_v001", "--scores-file", str(review)]), \
             patch("sys.stdout", output):
            main()
        text += output.getvalue()
        self.assertIn(FINAL_CSV_FILENAME, output.getvalue())
        self.assertIn("Students with an adjusted grade (curve or other adjustment): 10 of 11",
                      output.getvalue())
        self.assertIn("Your grade differs from the AI grade for 0 of 11", output.getvalue())
        for name in FIRST + ["Sample", "user01"]:
            self.assertNotIn(name, text)

        output = io.StringIO()
        with patch("sys.argv", ["ai_grader", "finalize-grading", str(self.root), "--run",
                                "run_v001", "--scores-file", str(self.root / "missing.csv")]), \
             patch("sys.stdout", output), self.assertRaises(SystemExit):
            main()
        self.assertIn("Not finalized: File not found", output.getvalue())
        output = io.StringIO()
        with patch("sys.argv", ["ai_grader", "finalize-grading", str(self.root), "--run",
                                "run_v001", "--scores-file", str(self.v001)]), \
             patch("sys.stdout", output), self.assertRaises(SystemExit):
            main()
        self.assertIn("total_instructor_score is blank for 11 student(s) (Student_001,",
                      output.getvalue())

    def test_review_file_comments_written_by_the_model(self):
        model = FakeModel(comment=lambda sid: "You built a sound model; explain the report.")
        stats = {}
        path = generate_review_report(self.root, "run_v001", use_model_comments=True,
                                      client=model, model="test-model", comment_stats=stats)
        self.assertEqual(stats["ai"], 11)
        self.assertEqual({row["final_comment"] for row in read_rows(path)},
                         {"You built a sound model; explain the report."})
        self.assertNotIn("Sample", json.dumps(model.calls))

    def test_finalize_directly_from_a_filled_review_file(self):
        rows = read_rows(self.v001)
        self.assertEqual(list(rows[0])[:5], ["student_name", "student_id", "total_agent_score",
                                             "total_instructor_score", "final_comment"])
        for row in rows:
            row["total_instructor_score"] = row["total_agent_score"]
            if row["student_id"] == "Student_002":
                row["total_instructor_score"] = "79"            # A correction (AI 75).
            if row["student_id"] == "Student_004":
                row["final_comment"] = "Clear model; cite your sources."
        write_rows(self.v001, rows)
        output = io.StringIO()
        with patch("sys.argv", ["ai_grader", "finalize-grading", str(self.root), "--run",
                                "run_v001", "--scores-file", str(self.v001)]), \
             patch("sys.stdout", output):
            main()
        text = output.getvalue()
        self.assertIn("Your grade differs from the AI grade for 1 of 11 student(s):", text)
        self.assertIn("Student_002: AI 75 -> yours 79 (+4)", text)
        self.assertNotIn("adjusted grade", text)
        folder = next((self.root / "grader" / "finalizations" / "run_v001").iterdir())
        final = {row["Username"]: row for row in read_rows(folder / FINAL_CSV_FILENAME)}
        self.assertEqual(list(next(iter(final.values()))),
                         ["Student Name", "Username", "Grade", "Comment"])
        self.assertEqual((final["user02"]["Grade"], final["user01"]["Grade"]), ("79", "68"))
        self.assertEqual(final["user04"]["Comment"], "Clear model; cite your sources.")
        self.assertTrue(final["user01"]["Comment"].startswith("Your strongest work was in"))


    def test_cli_explains_api_refusals(self):
        class FakeRateLimitError(Exception):        # Shaped like openai.RateLimitError.
            status_code = 429
            body = {"error": {"code": "credit_balance_exhausted", "type": "insufficient_quota"}}

        error = FakeRateLimitError("Error code: 429")
        output = io.StringIO()
        with patch("ai_grader.grade_adjustment.get_client",
                   return_value=SimpleNamespace(responses=SimpleNamespace(
                       create=lambda **_: (_ for _ in ()).throw(error)))), \
             patch("sys.argv", ["ai_grader", "adjust-grades", str(self.root), "--run",
                                "run_v001", "--instructions", "Curve to a mean of 90."]), \
             patch("sys.stdout", output), self.assertRaises(SystemExit):
            main()
        self.assertIn("no API credits left", output.getvalue())
        self.assertIn("--plan-file PLAN.json --template-comments", output.getvalue())
        self.assertEqual(len(list(self.run_path.glob("instructor_review_v*.csv"))), 1)


if __name__ == "__main__":
    unittest.main()
