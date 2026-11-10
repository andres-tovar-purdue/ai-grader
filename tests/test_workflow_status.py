import csv
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_grader.cli import main
from ai_grader.workflow_status import (
    StatusError, format_overview, format_project_status, project_state,
    projects_overview, resolve_status_target,
)


NAMES = ["Avery Student", "Jordan Example", "Riley Sample", "Casey Synthetic"]
USERNAMES = ["astudent", "jexample", "rsample", "csynthet"]


def stage(state, key):
    return next(item for item in state["stages"] if item["key"] == key)


class ProjectBuilder:
    """Synthetic projects at each point of the workflow (no real student data)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name).resolve()
        self.project = self.workspace / "projects" / "demo_fa26_prj3"
        self.project.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def set_up_folders(self, students=4, unrecognized=0):
        for folder in ("project", "datasets", "rubric", "reference", "submissions", "grader"):
            (self.project / folder).mkdir(exist_ok=True)
        submissions = self.project / "submissions"
        (submissions / "index.html").write_text("<html></html>", encoding="utf-8")
        for index in range(students):
            folder = submissions / (f"10000{index}-2000 - {USERNAMES[index]} "
                                    f"{NAMES[index]} - Oct {index + 1}, 2026 718 PM")
            folder.mkdir()
            (folder / f"{USERNAMES[index]}_notebook.ipynb").write_text("{}", encoding="utf-8")
        for index in range(unrecognized):
            (submissions / f"Project03_LateStudent{index}").mkdir()

    def add_files(self, docx=False):
        (self.project / "project" / "assignment.pdf").write_bytes(b"%PDF")
        if docx:
            (self.project / "project" / "assignment_v3.docx").write_bytes(b"PK")
        (self.project / "datasets" / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    def add_drafts(self, count=3):
        paths = ["project/project_instructions.md", "rubric/instructor_rubric.md",
                 "reference/reference_solution.md"]
        for relative in paths[:count]:
            path = self.project / relative
            path.write_text("# Draft\n", encoding="utf-8")
            old = time.time() - 100
            os.utime(path, (old, old))

    def add_spec(self, version=1, status="draft", unresolved=0):
        spec = {"status": status, "known_ambiguities": [
            {"ambiguity_id": f"A{index}", "status": "unresolved"}
            for index in range(unresolved)]}
        path = self.project / "grader" / f"grading_spec_v{version:03d}.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def prepare(self, students=4, spec_sha="current"):
        from ai_grader.submission_checks import spec_fingerprint
        grader = self.project / "grader"
        specs = sorted(grader.glob("grading_spec_v*.json"))
        sha = spec_fingerprint(json.loads(specs[-1].read_text())) if spec_sha == "current" else spec_sha
        (grader / "submission_manifest.json").write_text(json.dumps({
            "submission_count": students, "unparsed_folder_count": 0,
            "submissions": [{"student_id": f"Student_{i + 1:03d}",
                             "deliverable_checks": {"spec_sha256": sha}}
                            for i in range(students)]}), encoding="utf-8")
        (grader / "student_map.json").write_text("{}", encoding="utf-8")
        (grader / "anonymized_submissions").mkdir(exist_ok=True)

    def grade(self, run=1, students=4, invalid=0, spec="grading_spec_v001.json"):
        run_path = self.project / "grader" / "grading_runs" / f"run_v{run:03d}"
        run_path.mkdir(parents=True)
        results = [{"student_id": f"Student_{index + 1:03d}",
                    "validation_status": ("invalid_model_response" if index < invalid
                                          else "validated")}
                   for index in range(students)]
        (run_path / "grading_results.json").write_text(json.dumps(
            {"model": "gpt-5.5", "grading_spec": spec, "results": results}), encoding="utf-8")
        return run_path

    def review(self, run_path, filled, students=4, version=1):
        path = run_path / f"instructor_review_v{version:03d}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["student_name", "student_id", "total_agent_score",
                             "total_instructor_score", "final_comment"])
            for index in range(students):
                writer.writerow([NAMES[index], f"Student_{index + 1:03d}", "90",
                                 "92" if index < filled else "", "Good work."])
        return path

    def finalize(self, run_path):
        final = self.project / "grader" / "finalizations" / run_path.name / "finalization_v001"
        final.mkdir(parents=True)
        (final / "final_grades_for_brightspace.csv").write_text("x\n", encoding="utf-8")
        return final

    def ready_to_grade(self):
        self.set_up_folders()
        self.add_files()
        self.add_drafts()
        self.add_spec(status="approved")

    def state(self):
        return project_state(self.project, cwd=self.workspace)

    def assert_private(self, state):
        text = format_project_status(state) + json.dumps(state)
        for secret in NAMES + USERNAMES + ["LateStudent"]:
            self.assertNotIn(secret, text)


class StatusTests(ProjectBuilder, unittest.TestCase):
    CONTINUE = "python -m ai_grader continue demo_fa26_prj3"

    def test_five_stages(self):
        state = self.state()
        self.assertEqual([item["title"] for item in state["stages"]],
                         ["Set up", "Rubric", "Grade", "Review grades", "Finalize"])

    def test_dropped_files_are_sorted_by_continue(self):
        (self.project / "Project 3 Download Nov 9, 2026 112 PM.zip").write_bytes(b"PK")
        (self.project / "Project_03.pdf").write_bytes(b"%PDF")
        state = self.state()
        self.assertEqual(state["current_stage"], "setup")
        self.assertEqual(state["next"]["who"], "continue")
        self.assertEqual(state["next"]["key"], "setup")
        self.assertEqual(state["next"]["command"], self.CONTINUE)
        self.assertIn("Next: AI Grader will sort the files", format_project_status(state))

    def test_missing_download_or_assignment_is_your_turn(self):
        self.set_up_folders(students=0)
        state = self.state()
        self.assertEqual(state["next"]["who"], "you")
        self.assertIn("Brightspace download ZIP", state["next"]["text"])
        folder = self.project / "submissions" / "1-2 - auser A User - Oct 1, 2026 718 PM"
        folder.mkdir()
        state = self.state()
        self.assertIn("assignment PDF", state["next"]["text"])

    def test_rubric_is_drafted_then_waits_for_approval(self):
        self.set_up_folders(unrecognized=1)
        self.add_files(docx=True)
        state = self.state()
        self.assertEqual(stage(state, "setup")["state"], "done")
        self.assertEqual((state["current_stage"], state["next"]["key"]), ("rubric", "draft"))
        warnings = " ".join(state["warnings"])
        self.assertIn("1 folder in submissions\\ is not named like a Brightspace", warnings)
        self.assertIn("1 file in project\\ is not read", warnings)
        self.assert_private(state)

        self.add_drafts()
        self.assertEqual(self.state()["next"]["key"], "spec")
        self.add_spec(status="draft", unresolved=2)
        (self.project / "grader" / "review_package_v001.zip").write_bytes(b"PK")
        state = self.state()
        self.assertEqual(state["next"]["who"], "you")
        self.assertIn("upload grader\\review_package_v001.zip", state["next"]["text"])
        self.assertIn("2 open questions", state["next"]["text"])
        self.assertEqual(state["next"]["command"], self.CONTINUE + " --approve")
        self.assertIn("grading_spec_v001.json: draft, 2 open questions",
                      stage(state, "rubric")["detail"])

    def test_partial_drafts(self):
        self.set_up_folders()
        self.add_files()
        self.add_drafts(count=2)
        state = self.state()
        self.assertEqual(state["next"]["who"], "you")
        self.assertIn("Only 2 of the 3 rubric drafts exist", state["next"]["text"])

    def test_reviewed_spec_and_changed_rubric(self):
        self.set_up_folders()
        self.add_files()
        self.add_drafts()
        spec = self.add_spec(status="draft")
        old = time.time() - 50
        os.utime(spec, (old, old))
        reviewed = self.project / "grader" / "grading_spec_reviewed.json"
        reviewed.write_text("{}", encoding="utf-8")
        self.assertEqual(self.state()["next"]["key"], "install_reviewed")
        reviewed.unlink()
        rubric = self.project / "rubric" / "instructor_rubric.md"
        rubric.write_text("# Edited\n", encoding="utf-8")
        state = self.state()
        self.assertEqual(state["next"]["key"], "spec")
        self.assertTrue(state["facts"]["rubric_changed"])

    def test_course_policy_is_installed_before_the_rubric_only(self):
        self.set_up_folders()
        self.add_files()
        policies = self.workspace / "projects" / "_policies"
        policies.mkdir()
        (policies / "demo_fa26.md").write_text("minimum_attempted_fraction: 0.5\n",
                                               encoding="utf-8")
        state = self.state()
        self.assertEqual(state["next"]["key"], "setup")
        self.add_spec(status="approved")
        self.assertNotEqual(self.state()["current_stage"], "setup")

    def test_grading_is_automatic_after_approval(self):
        self.ready_to_grade()
        state = self.state()
        self.assertEqual((state["current_stage"], state["next"]["key"]), ("grade", "grade"))
        self.prepare()
        run_path = self.grade(invalid=1)
        state = self.state()
        self.assertEqual(state["next"]["key"], "grade")
        self.assertIn("1 not scored", stage(state, "grade")["detail"])
        run_path = self.grade(run=2)
        state = self.state()
        self.assertEqual(state["next"]["key"], "grade")
        self.assertIn("review sheet not written yet", stage(state, "grade")["detail"])
        self.review(run_path, filled=0)
        self.assertEqual(self.state()["current_stage"], "review")

    def test_unrecognized_folder_blocks_grading(self):
        self.ready_to_grade()
        (self.project / "submissions" / "Project03_LateStudent").mkdir()
        state = self.state()
        self.assertEqual(state["next"]["who"], "you")
        self.assertIn("Rename the submission folders", state["next"]["text"])
        self.assert_private(state)

    def test_student_added_after_anonymizing(self):
        self.ready_to_grade()
        self.prepare(students=3)
        state = self.state()
        self.assertEqual(state["next"]["key"], "grade")
        self.assertIn("continue prepares them again", " ".join(state["warnings"]))
        self.grade(students=3)
        state = self.state()
        self.assertEqual(state["next"]["who"], "you")
        self.assertIn("archive-grading", " ".join(state["warnings"]))

    def test_newer_spec_needs_a_new_run(self):
        self.ready_to_grade()
        self.prepare()
        run_path = self.grade()
        self.review(run_path, filled=4)
        self.add_spec(version=2, status="approved")
        state = self.state()
        self.assertEqual(state["next"]["key"], "grade")
        self.assertTrue(state["facts"]["stale_run"])

    def test_review_then_finalize_then_brightspace(self):
        self.ready_to_grade()
        self.prepare()
        run_path = self.grade()
        review = self.review(run_path, filled=3)
        state = self.state()
        self.assertEqual((state["current_stage"], state["next"]["who"]), ("review", "you"))
        self.assertIn("3 of 4 grades entered", stage(state, "review")["detail"])
        self.assertIn("instructor_review_v001.csv", state["next"]["text"])
        self.assert_private(state)

        review = self.review(run_path, filled=4, version=2)
        state = self.state()
        self.assertEqual((state["current_stage"], state["next"]["key"]), ("finalize", "finalize"))
        final = self.finalize(run_path)
        state = self.state()
        self.assertIsNone(state["current_stage"])
        self.assertEqual(state["next"]["who"], "you")
        self.assertIn("Brightspace", state["next"]["text"])
        later = time.time() + 60
        os.utime(review, (later, later))
        state = self.state()
        self.assertEqual(state["next"]["key"], "finalize")
        self.assertIn("review sheet changed since", stage(state, "finalize")["detail"])
        self.assertTrue(final.is_dir())

    def test_unreadable_review_file(self):
        self.ready_to_grade()
        self.prepare()
        run_path = self.grade()
        (run_path / "instructor_review_v001.csv").write_bytes(b"student_id\xff\xfe\x00bad")
        state = self.state()
        self.assertIn("CSV UTF-8", " ".join(state["warnings"]))

    # Finding the project.

    def test_resolve_by_name_from_workspace_projects_and_project_folder(self):
        self.assertEqual(resolve_status_target("demo_fa26_prj3", self.workspace),
                         ("project", self.project))
        self.assertEqual(resolve_status_target("demo_fa26_prj3", self.workspace / "projects"),
                         ("project", self.project))
        self.assertEqual(resolve_status_target("demo_fa26_prj3", self.project),
                         ("project", self.project))

    def test_resolve_without_name(self):
        self.assertEqual(resolve_status_target(None, self.workspace),
                         ("list", self.workspace / "projects"))
        self.assertEqual(resolve_status_target(None, self.project), ("project", self.project))
        elsewhere = self.workspace / "elsewhere"
        elsewhere.mkdir()
        with self.assertRaises(StatusError):
            resolve_status_target(None, elsewhere)
        with self.assertRaises(StatusError) as caught:
            resolve_status_target("nope", self.workspace)
        self.assertIn("demo_fa26_prj3", str(caught.exception))

    def test_overview_skips_the_policies_folder(self):
        self.set_up_folders()
        self.add_files()
        (self.workspace / "projects" / "other_fa26_prj2").mkdir()
        (self.workspace / "projects" / "_policies").mkdir()
        overview = projects_overview(self.workspace / "projects", cwd=self.workspace)
        self.assertEqual([item["project"] for item in overview],
                         ["demo_fa26_prj3", "other_fa26_prj2"])
        text = format_overview(self.workspace / "projects", overview)
        self.assertIn("stage 2 of 5: Rubric (run continue)", text)
        self.assertIn("stage 1 of 5: Set up (run continue)", text)

    # The command.

    def run_cli(self, *arguments, cwd=None):
        output = io.StringIO()
        previous = Path.cwd()
        os.chdir(cwd or self.workspace)
        try:
            with patch("sys.argv", ["ai_grader", "status", *arguments]), \
                 patch("sys.stdout", output):
                main()
        finally:
            os.chdir(previous)
        return output.getvalue()

    def test_cli_text_json_and_list(self):
        self.set_up_folders(unrecognized=1)
        self.add_files()
        text = self.run_cli("demo_fa26_prj3")
        self.assertIn("[done] 1. Set up", text)
        self.assertIn("[next] 2. Rubric", text)
        self.assertIn("Needs attention:", text)
        self.assertIn(self.CONTINUE, text)
        for secret in NAMES + USERNAMES + ["LateStudent"]:
            self.assertNotIn(secret, text)
        data = json.loads(self.run_cli("demo_fa26_prj3", "--json"))
        self.assertEqual(data["current_stage"], "rubric")
        self.assertIn("demo_fa26_prj3  stage 2 of 5", self.run_cli())

    def test_status_writes_nothing(self):
        self.set_up_folders()
        self.add_files()
        before = sorted(str(path) for path in self.workspace.rglob("*"))
        self.run_cli("demo_fa26_prj3")
        self.run_cli()
        self.assertEqual(before, sorted(str(path) for path in self.workspace.rglob("*")))


if __name__ == "__main__":
    unittest.main()
