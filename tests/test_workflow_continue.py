"""continue: the 5-stage workflow end to end. Synthetic data; every AI step is mocked."""

import csv
import io
import json
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from ai_grader.cli import main
from ai_grader.course_policy import (
    course_name, enforce_attempt_minimum, install_policy, policy_settings,
)
from ai_grader.project_setup import sort_loose_files
from ai_grader.review_package import write_review_package
from ai_grader.workflow_continue import ContinueStopped, continue_project
from ai_grader.workflow_status import project_state


ROOT = Path(__file__).resolve().parents[1]
VALID_SPEC = json.loads((ROOT / "tests" / "fixtures" / "mspe_49600_pr01_corrected_draft.json")
                        .read_text(encoding="utf-8"))
STUDENTS = [("100001-2000", "astudent", "Avery Student"),
            ("100002-2000", "jexample", "Jordan Example")]
POLICY = """# Grading policy: demo_fa26

minimum_attempted_fraction: 0.5
point_step: 0.5

Grade generously: every attempted item keeps at least half of its points.
"""


def brightspace_zip(path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("index.html", "<html></html>")
        for prefix, username, name in STUDENTS:
            folder = f"{prefix} - {username} {name} - Oct 3, 2026 745 PM"
            archive.writestr(f"{folder}/{username}_pr03_notebook.ipynb", "{}")


class Fakes:
    """Mocked AI steps that write what the real ones would."""

    def __init__(self, project, invalid_first=0, spec_failures=0):
        self.project = project
        self.invalid_first = invalid_first
        self.spec_failures = spec_failures
        self.calls = []

    def prepare_project(self, project_path):
        self.calls.append("prepare_project")
        for relative in ("project/project_instructions.md", "rubric/instructor_rubric.md",
                         "reference/reference_solution.md"):
            path = Path(project_path) / relative
            path.write_text("# Draft\n", encoding="utf-8")
            old = time.time() - 100
            os.utime(path, (old, old))

    def generate_grading_spec(self, project_path, schema_path, response_path=None):
        from ai_grader.course_policy import apply_policy, policy_settings, policy_text
        from ai_grader.spec_generation import InvalidSpecResponseError
        self.calls.append("generate_grading_spec" + (":saved" if response_path else ""))
        grader = Path(project_path) / "grader"
        if self.spec_failures:
            self.spec_failures -= 1
            saved = grader / f"invalid_spec_response_{self.spec_failures}.json"
            saved.write_text("{}", encoding="utf-8")
            raise InvalidSpecResponseError(["tasks: is a required property"], saved)
        spec = json.loads(json.dumps(VALID_SPEC))
        spec["status"] = "draft"
        spec.pop("approval", None)
        spec, _ = apply_policy(spec, policy_settings(policy_text(project_path)))
        version = len(list(grader.glob("grading_spec_v*.json"))) + 1
        path = grader / f"grading_spec_v{version:03d}.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path, spec

    def write_submission_manifest(self, project_path):
        from ai_grader.submission_checks import spec_fingerprint
        self.calls.append("write_submission_manifest")
        grader = Path(project_path) / "grader"
        spec_path = sorted(grader.glob("grading_spec_v*.json"))[-1]
        sha = spec_fingerprint(json.loads(spec_path.read_text()))
        manifest = {"submission_count": 2, "unparsed_folder_count": 0, "submissions": [
            {"student_id": f"Student_{i:03d}", "deliverable_checks": {"spec_sha256": sha}}
            for i in (1, 2)]}
        (grader / "submission_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (grader / "student_map.json").write_text("{}", encoding="utf-8")
        (grader / "anonymized_submissions").mkdir(exist_ok=True)
        return grader / "submission_manifest.json", grader / "student_map.json", manifest

    def grade_submissions(self, project_path, model=None, resume=False, retry_failed=False):
        self.calls.append("grade_submissions" + (":retry" if retry_failed else ""))
        grader = Path(project_path) / "grader"
        runs = grader / "grading_runs"
        version = len(list(runs.glob("run_v*"))) + 1 if runs.is_dir() else 1
        run_path = runs / f"run_v{version:03d}"
        run_path.mkdir(parents=True)
        invalid = 0 if retry_failed else self.invalid_first
        spec_name = sorted(grader.glob("grading_spec_v*.json"))[-1].name
        run = {"model": "test-model", "grading_spec": spec_name, "submission_count": 2,
               "unscored_submission_count": invalid, "results": [
                   {"student_id": f"Student_{i:03d}",
                    "validation_status": "invalid_model_response" if i <= invalid
                    else "validated"} for i in (1, 2)]}
        (run_path / "grading_results.json").write_text(json.dumps(run), encoding="utf-8")
        return run_path, run_path / "grading_results.json", run_path / "report.csv", run

    def generate_review_report(self, project_path, run_version, use_model_comments=False,
                               model=None, comment_stats=None):
        self.calls.append("generate_review_report")
        path = (Path(project_path) / "grader" / "grading_runs" / run_version
                / "instructor_review_v001.csv")
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["student_name", "student_id", "total_agent_score",
                             "total_instructor_score", "flag", "final_comment", "deductions",
                             "adjustment_reason"])
            for (_, _, name), number in zip(STUDENTS, (1, 2)):
                writer.writerow([name, f"Student_{number:03d}", "97", "", "", "Good.",
                                 "Plot has no legend (-0.5)", ""])
        return path

    def finalize_grading(self, project_path, run, approved, overrides, decisions, accept,
                         edits):
        self.calls.append("finalize_grading")
        final = Path(project_path) / "grader" / "finalizations" / run / "finalization_v001"
        final.mkdir(parents=True)
        (final / "final_grades_for_brightspace.csv").write_text("x\n", encoding="utf-8")
        return final, final / "summary.csv", [final / "a.txt", final / "b.txt"], {}

    def patches(self):
        return [
            patch("ai_grader.project_preparation.prepare_project", self.prepare_project),
            patch("ai_grader.spec_generation.generate_grading_spec", self.generate_grading_spec),
            patch("ai_grader.submission_processing.write_submission_manifest",
                  self.write_submission_manifest),
            patch("ai_grader.grading.grade_submissions", self.grade_submissions),
            patch("ai_grader.reporting.generate_review_report", self.generate_review_report),
            patch("ai_grader.finalization.finalize_grading", self.finalize_grading),
            patch("ai_grader.finalization.feedback_edits_from_review", lambda *a: {}),
            patch.dict("os.environ", {"GRADER_INSTRUCTOR_NAME": "Test Instructor"}),
        ]


class ContinueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name).resolve()
        self.projects = self.workspace / "projects"
        self.project = self.projects / "demo_fa26_prj3"
        self.project.mkdir(parents=True)
        policies = self.projects / "_policies"
        policies.mkdir()
        (policies / "demo_fa26.md").write_text(POLICY, encoding="utf-8")
        brightspace_zip(self.project / "Project 3 Download Nov 9, 2026 112 PM.zip")
        (self.project / "Project_03_Assignment.pdf").write_bytes(b"%PDF")
        (self.project / "ims_data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        (self.project / "Solution.docx").write_bytes(b"PK")
        self.lines = []
        self.previous = Path.cwd()
        os.chdir(self.workspace)

    def tearDown(self):
        os.chdir(self.previous)
        self.tmp.cleanup()

    def run_continue(self, fakes, approve=False):
        patches = fakes.patches()
        for item in patches:
            item.start()
        try:
            return continue_project(self.project, approve=approve, say=self.lines.append)
        finally:
            for item in reversed(patches):
                item.stop()

    def text(self):
        return "\n".join(self.lines)

    def test_the_whole_workflow_stops_only_for_the_instructor(self):
        fakes = Fakes(self.project)

        # Stages 1-2: sort, draft, specification, review package; stop for approval.
        state = self.run_continue(fakes)
        self.assertEqual(fakes.calls, ["prepare_project", "generate_grading_spec"])
        self.assertTrue((self.project / "submissions" / "index.html").is_file())
        self.assertTrue((self.project / "project" / "Project_03_Assignment.pdf").is_file())
        self.assertTrue((self.project / "datasets" / "ims_data.csv").is_file())
        self.assertTrue((self.project / "rubric" / "grading_policy.md").is_file())
        self.assertEqual((state["current_stage"], state["next"]["who"]), ("rubric", "you"))
        self.assertIn("--approve", state["next"]["command"])
        self.assertIn("Solution.docx", self.text())     # Moved but not read: reported.
        package = self.project / "grader" / "review_package_v001.zip"
        with zipfile.ZipFile(package) as archive:
            names = archive.namelist()
        self.assertIn("grader/grading_spec_v001.json", names)
        self.assertIn("rubric/grading_policy.md", names)
        self.assertIn("datasets/ims_data.csv", names)
        self.assertFalse(any(name.startswith("submissions") for name in names))
        spec = json.loads((self.project / "grader" / "grading_spec_v001.json").read_text())
        self.assertIn("course policy", spec["notes"])
        for secret in ("Avery", "Jordan", "astudent", "jexample"):
            self.assertNotIn(secret, self.text())
            self.assertNotIn(secret, json.dumps(state))

        # Approve: grading follows in the same run; stop for the instructor's grades.
        state = self.run_continue(fakes, approve=True)
        self.assertEqual(fakes.calls[2:], ["write_submission_manifest", "grade_submissions",
                                           "generate_review_report"])
        self.assertIn("Rubric approved by Test Instructor", self.text())
        self.assertEqual((state["current_stage"], state["next"]["who"]), ("review", "you"))

        # Nothing to do until the grades are entered.
        state = self.run_continue(fakes)
        self.assertEqual(len(fakes.calls), 5)
        review = self.project / "grader" / "grading_runs" / "run_v001" / "instructor_review_v001.csv"
        with review.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        for row in rows:
            row["total_instructor_score"] = "97"
        with review.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

        # Stage 5.
        state = self.run_continue(fakes)
        self.assertEqual(fakes.calls[-1], "finalize_grading")
        self.assertIsNone(state["current_stage"])
        self.assertIn("Brightspace", state["next"]["text"])

    def test_failed_students_are_asked_again_once(self):
        fakes = Fakes(self.project, invalid_first=1)
        self.run_continue(fakes)
        state = self.run_continue(fakes, approve=True)
        self.assertIn("grade_submissions:retry", fakes.calls)
        self.assertEqual(state["current_stage"], "review")
        self.assertIn("all 2 students scored", self.text())

    def test_a_failing_specification_is_asked_for_twice_then_stops(self):
        fakes = Fakes(self.project, spec_failures=2)
        with self.assertRaises(ContinueStopped) as caught:
            self.run_continue(fakes)
        self.assertIn("failed AI Grader's checks", str(caught.exception))
        self.assertIn("upload it to Claude", str(caught.exception))
        self.assertEqual(fakes.calls.count("generate_grading_spec"), 2)
        fakes.spec_failures = 1
        state = self.run_continue(fakes)       # Drafts kept; one more failure, then success.
        self.assertEqual(fakes.calls.count("prepare_project"), 1)
        self.assertEqual(state["next"]["who"], "you")

    def test_a_reviewed_specification_is_installed_then_approved(self):
        fakes = Fakes(self.project)
        self.run_continue(fakes)
        reviewed = self.project / "grader" / "grading_spec_reviewed.json"
        reviewed.write_text(json.dumps(VALID_SPEC), encoding="utf-8")
        state = self.run_continue(fakes, approve=True)
        self.assertIn("generate_grading_spec:saved", fakes.calls)
        approved = json.loads((self.project / "grader" / "grading_spec_v002.json").read_text())
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(state["current_stage"], "review")

    def test_cli_continue_prints_steps_and_the_next_turn(self):
        fakes = Fakes(self.project)
        output = io.StringIO()
        patches = fakes.patches()
        for item in patches:
            item.start()
        try:
            with patch("sys.argv", ["ai_grader", "continue", "demo_fa26_prj3"]), \
                 patch("sys.stdout", output):
                main()
        finally:
            for item in reversed(patches):
                item.stop()
        text = output.getvalue()
        self.assertIn("Stage 1, set up:", text)
        self.assertIn("Review package for Claude (no student data)", text)
        self.assertIn("Your turn: Check the rubric", text)
        self.assertIn("python -m ai_grader continue demo_fa26_prj3 --approve", text)
        for secret in ("Avery", "Jordan", "astudent", "jexample"):
            self.assertNotIn(secret, text)


class PolicyTests(unittest.TestCase):
    def test_course_name_and_settings(self):
        self.assertEqual(course_name("mspe_49600_fa26_prj3"), "mspe_49600")
        self.assertEqual(policy_settings(POLICY), {"minimum_attempted_fraction": 0.5,
                                                   "point_step": 0.5})
        self.assertEqual(policy_settings("minimum_attempted_fraction: 7"), {})

    def test_install_copies_once(self):
        with tempfile.TemporaryDirectory() as directory:
            projects = Path(directory) / "projects"
            project = projects / "demo_fa26_prj3"
            (project / "rubric").mkdir(parents=True)
            self.assertIsNone(install_policy(project))
            (projects / "_policies").mkdir()
            (projects / "_policies" / "demo_fa26.md").write_text(POLICY, encoding="utf-8")
            path = install_policy(project)
            self.assertEqual(path.read_text(encoding="utf-8"), POLICY)
            path.write_text("edited", encoding="utf-8")
            install_policy(project)
            self.assertEqual(path.read_text(encoding="utf-8"), "edited")

    def test_attempt_minimum_splits_each_item_once(self):
        spec = {"notes": "", "tasks": [{"task_id": "T1", "criteria": [
            {"criterion_id": "C1", "max_points": 5, "deduction_rules": [
                {"rule_id": "C1_none", "condition": "No attempt.", "deduction_points": 5,
                 "basis": "technical"},
                {"rule_id": "C1_plot", "condition": "Plot missing labels.",
                 "deduction_points": 1.5, "basis": "technical"},
                {"rule_id": "C1_file", "condition": "CSV missing.", "deduction_points": 0.5,
                 "basis": "file_presence", "deliverable_id": "csv"}]},
            {"criterion_id": "C2", "max_points": 1, "deduction_rules": [
                {"rule_id": "C2_all", "condition": "README missing.", "deduction_points": 1,
                 "basis": "technical"}]}]}]}
        new, split = enforce_attempt_minimum(spec, 0.5)
        self.assertEqual(split, 2)
        rules = {r["rule_id"]: r for r in new["tasks"][0]["criteria"][0]["deduction_rules"]}
        self.assertEqual(set(rules), {"C1_none", "C1_plot_partial", "C1_plot_absent", "C1_file"})
        self.assertEqual(rules["C1_plot_partial"]["deduction_points"], 0.75)
        self.assertEqual(rules["C1_plot_absent"]["deduction_points"], 1.5)
        self.assertIn("attempted but incomplete", rules["C1_plot_partial"]["condition"])
        second = {r["rule_id"]: r for r in new["tasks"][0]["criteria"][1]["deduction_rules"]}
        self.assertEqual(second["C2_all_partial"]["deduction_points"], 0.5)
        again, split_again = enforce_attempt_minimum(new, 0.5)
        self.assertEqual((split_again, again), (0, new))
        self.assertIn("course policy", new["notes"])


class SetupAndPackageTests(unittest.TestCase):
    def test_sorting_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "project").mkdir()
            (project / "project" / "a.pdf").write_bytes(b"old")
            for name in ("a.pdf", "b.md", "c.CSV", "d.ipynb", "e.png", "f.zip"):
                (project / name).write_bytes(b"new")
            summary = sort_loose_files(project)
            self.assertEqual(summary["already_there"], ["a.pdf"])
            self.assertEqual(summary["project"], ["b.md"])
            self.assertEqual(summary["datasets"], ["c.CSV"])
            self.assertEqual(summary["unread"], ["d.ipynb"])
            self.assertEqual(summary["left"], ["e.png"])
            self.assertEqual((project / "project" / "a.pdf").read_bytes(), b"old")
            self.assertTrue((project / "f.zip").is_file())

    def test_package_has_no_student_data(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "demo"
            for folder in ("project", "rubric", "reference", "datasets", "grader",
                           "submissions/1-2 - astudent Avery Student - Oct 1, 2026 718 PM"):
                (project / folder).mkdir(parents=True)
            (project / "project" / "assignment.pdf").write_bytes(b"%PDF")
            (project / "submissions/1-2 - astudent Avery Student - Oct 1, 2026 718 PM/x.py"
             ).write_text("x", encoding="utf-8")
            (project / "grader" / "student_map.json").write_text("{}", encoding="utf-8")
            spec = project / "grader" / "grading_spec_v001.json"
            spec.write_text("{}", encoding="utf-8")
            first = write_review_package(project, spec)
            second = write_review_package(project, spec)
            self.assertEqual((first.name, second.name),
                             ("review_package_v001.zip", "review_package_v002.zip"))
            with zipfile.ZipFile(first) as archive:
                names = archive.namelist()
                readme = archive.read("README.txt").decode()
            self.assertEqual(sorted(names), ["README.txt", "grader/grading_spec_v001.json",
                                             "project/assignment.pdf"])
            self.assertIn("contains no student work", readme)


if __name__ == "__main__":
    unittest.main()
