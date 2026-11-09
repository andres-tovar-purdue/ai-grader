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
    StatusError, format_overview, format_project_status, project_status,
    projects_overview, resolve_status_target,
)


NAMES = ["Avery Student", "Jordan Example", "Riley Sample", "Casey Synthetic"]
USERNAMES = ["astudent", "jexample", "rsample", "csynthet"]


def stage(status, key):
    return next(item for item in status["stages"] if item["key"] == key)


class WorkflowStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name).resolve()
        self.project = self.workspace / "projects" / "demo_fa26_prj2"
        self.project.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    # Builders for each stage, with synthetic students only.

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
            (submissions / f"Project02_LateStudent{index}").mkdir()

    def add_files(self, docx=True):
        (self.project / "project" / "assignment.pdf").write_bytes(b"%PDF")
        if docx:
            (self.project / "project" / "assignment_v3.docx").write_bytes(b"PK")
        (self.project / "datasets" / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    def add_drafts(self, count=3):
        paths = ["project/project_instructions.md", "rubric/instructor_rubric.md",
                 "reference/reference_solution.md"]
        for relative in paths[:count]:
            (self.project / relative).write_text("# Draft\n", encoding="utf-8")

    def add_spec(self, version=1, status="draft", unresolved=0):
        spec = {"status": status, "known_ambiguities": [
            {"ambiguity_id": f"A{index}", "status": "unresolved"}
            for index in range(unresolved)]}
        path = self.project / "grader" / f"grading_spec_v{version:03d}.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def prepare(self, students=4):
        grader = self.project / "grader"
        (grader / "submission_manifest.json").write_text(
            json.dumps({"submission_count": students, "unparsed_folder_count": 0}),
            encoding="utf-8")
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
            {"model": "gpt-5.5", "grading_spec": spec, "results": results}),
            encoding="utf-8")
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
        final = (self.project / "grader" / "finalizations" / run_path.name
                 / "finalization_v001")
        final.mkdir(parents=True)
        (final / "final_grades_for_brightspace.csv").write_text("x\n", encoding="utf-8")
        return final

    def status(self):
        return project_status(self.project, cwd=self.workspace)

    def assert_private(self, status):
        text = format_project_status(status) + json.dumps(status)
        for secret in NAMES + USERNAMES + ["LateStudent"]:
            self.assertNotIn(secret, text)

    # Stages in order.

    def test_brightspace_zip_not_unpacked_yet(self):
        (self.project / "Project 2 Download Nov 9, 2026 112 PM.zip").write_bytes(b"PK")
        status = self.status()
        self.assertEqual(status["current_stage"], "setup")
        self.assertEqual(status["next"]["command"],
                         "python -m ai_grader init-project demo_fa26_prj2")

    def test_empty_project_asks_for_the_download(self):
        self.set_up_folders(students=0)
        status = self.status()
        self.assertEqual(status["current_stage"], "setup")
        self.assertIn("Download the submissions from Brightspace", status["next"]["action"])

    def test_unrecognized_folder_and_ignored_docx_are_reported_without_names(self):
        self.set_up_folders(students=4, unrecognized=1)
        self.add_files()
        status = self.status()
        self.assertEqual(stage(status, "setup")["state"], "done")
        self.assertIn("4 submission folders, 4 students, index.html",
                      stage(status, "setup")["detail"])
        self.assertIn("1 assignment file in project\\ (1 ignored",
                      stage(status, "files")["detail"])
        self.assertEqual(status["current_stage"], "draft")
        self.assertEqual(status["next"]["command"],
                         "python -m ai_grader prepare-project .\\projects\\demo_fa26_prj2")
        self.assertEqual(len(status["warnings"]), 1)
        self.assertIn("1 folder in submissions\\ is not named like a Brightspace",
                      status["warnings"][0])
        self.assert_private(status)

    def test_missing_assignment_files(self):
        self.set_up_folders()
        status = self.status()
        self.assertEqual(status["current_stage"], "files")
        self.assertIn("Put the assignment PDF", status["next"]["action"])

    def test_unreadable_date_and_loose_files(self):
        self.set_up_folders(students=1)
        (self.project / "submissions" / "999999-1 - lateuser Late Student - 12 Oct 2026").mkdir()
        (self.project / "submissions" / "stray.csv").write_text("", encoding="utf-8")
        warnings = " ".join(self.status()["warnings"])
        self.assertIn("1 submission folder has a date AI Grader can't read", warnings)
        self.assertIn("1 file sits directly in submissions\\", warnings)

    def test_partial_drafts_warn(self):
        self.set_up_folders()
        self.add_files()
        self.add_drafts(count=2)
        status = self.status()
        self.assertEqual(status["current_stage"], "draft")
        self.assertIn("Only 2 of the 3 rubric drafts exist", " ".join(status["warnings"]))

    def test_drafts_ready_for_review_and_spec(self):
        self.set_up_folders()
        self.add_files()
        self.add_drafts()
        status = self.status()
        self.assertEqual(status["current_stage"], "spec")
        self.assertEqual(status["next"]["command"],
                         "python -m ai_grader generate-spec .\\projects\\demo_fa26_prj2")

    def test_draft_spec_awaits_approval(self):
        self.set_up_folders()
        self.add_files()
        self.add_drafts()
        self.add_spec(status="draft", unresolved=2)
        status = self.status()
        self.assertEqual(status["current_stage"], "approve")
        self.assertIn("2 unresolved questions", stage(status, "approve")["detail"])
        self.assertEqual(status["next"]["command"],
                         'python -m ai_grader approve-spec '
                         '".\\projects\\demo_fa26_prj2\\grader\\grading_spec_v001.json"')

    def test_approved_spec_then_anonymize(self):
        self.set_up_folders()
        self.add_files()
        self.add_drafts()
        self.add_spec(status="approved")
        status = self.status()
        self.assertEqual(status["current_stage"], "anonymize")
        self.assertIn("prepare-submissions", status["next"]["command"])

    def test_new_submission_after_anonymizing_is_flagged(self):
        self.set_up_folders(students=4)
        self.add_files()
        self.add_drafts()
        self.add_spec(status="approved")
        self.prepare(students=3)
        status = self.status()
        self.assertEqual(status["current_stage"], "grade")
        warning = " ".join(status["warnings"])
        self.assertIn("now has 4 students, but 3 were anonymized", warning)
        self.assertIn("prepare-submissions", warning)

    def test_unscored_students_are_retried(self):
        self.set_up_folders()
        self.add_files()
        self.add_drafts()
        self.add_spec(status="approved")
        self.prepare()
        self.grade(invalid=1)
        status = self.status()
        self.assertEqual(status["current_stage"], "grade")
        self.assertIn("1 not scored", stage(status, "grade")["detail"])
        self.assertTrue(status["next"]["command"].endswith("--resume --retry-failed"))

    def test_newer_approved_spec_needs_a_new_run(self):
        self.set_up_folders()
        self.add_files()
        self.add_drafts()
        self.add_spec(version=1, status="approved")
        self.prepare()
        self.grade(spec="grading_spec_v001.json")
        self.add_spec(version=2, status="approved")
        status = self.status()
        self.assertEqual(status["current_stage"], "grade")
        self.assertIn("grading_spec_v002.json is now approved", status["next"]["action"])

    def test_review_file_missing_partial_and_complete(self):
        self.set_up_folders()
        self.add_files()
        self.add_drafts()
        self.add_spec(status="approved")
        self.prepare()
        run_path = self.grade()

        status = self.status()
        self.assertEqual(status["current_stage"], "review")
        self.assertEqual(status["next"]["command"],
                         "python -m ai_grader generate-report "
                         ".\\projects\\demo_fa26_prj2 --run run_v001 --open")

        self.review(run_path, filled=3)
        status = self.status()
        self.assertEqual(status["current_stage"], "review")
        self.assertIn("3 of 4 grades entered", stage(status, "review")["detail"])
        self.assertIsNone(status["next"]["command"])
        self.assert_private(status)

        self.review(run_path, filled=4, version=2)
        status = self.status()
        self.assertEqual(status["current_stage"], "finalize")
        self.assertEqual(status["next"]["command"],
                         "python -m ai_grader finalize-grading .\\projects\\demo_fa26_prj2 "
                         "--run run_v001 --scores-file "
                         '".\\projects\\demo_fa26_prj2\\grader\\grading_runs\\run_v001'
                         '\\instructor_review_v002.csv"')

    def test_unreadable_review_file(self):
        self.set_up_folders()
        self.add_files()
        self.add_drafts()
        self.add_spec(status="approved")
        self.prepare()
        run_path = self.grade()
        (run_path / "instructor_review_v001.csv").write_bytes(b"student_id\xff\xfe\x00bad")
        status = self.status()
        self.assertIn("can't be read", stage(status, "review")["detail"])
        self.assertIn("CSV UTF-8", " ".join(status["warnings"]))

    def test_finalized_points_to_brightspace(self):
        self.set_up_folders()
        self.add_files()
        self.add_drafts()
        self.add_spec(status="approved")
        self.prepare()
        run_path = self.grade()
        review = self.review(run_path, filled=4)
        final = self.finalize(run_path)
        status = self.status()
        self.assertEqual(status["current_stage"], "brightspace")
        self.assertEqual(stage(status, "finalize")["state"], "done")
        self.assertIn("final_grades_for_brightspace.csv", status["next"]["action"])
        self.assertEqual(status["warnings"], [])

        later = time.time() + 60
        os.utime(review, (later, later))
        self.assertIn("was changed after finalization_v001",
                      " ".join(self.status()["warnings"]))
        self.assertTrue(final.is_dir())

    def test_only_one_stage_is_next(self):
        self.set_up_folders()
        self.add_files()
        self.add_drafts()
        self.add_spec(version=1, status="approved")
        self.prepare()
        run_path = self.grade()
        self.review(run_path, filled=4)
        self.finalize(run_path)
        self.add_spec(version=2, status="approved")
        status = self.status()
        states = [item["state"] for item in status["stages"]]
        self.assertEqual(states.count("next"), 1)
        self.assertEqual(status["current_stage"], "grade")

    def test_spaces_in_paths_are_quoted(self):
        project = self.workspace / "projects" / "demo prj3"
        project.mkdir()
        (project / "x.zip").write_bytes(b"PK")
        status = project_status(project, cwd=self.workspace)
        self.assertEqual(status["next"]["command"],
                         'python -m ai_grader init-project "demo prj3"')
        (project / "x.zip").unlink()
        for folder in ("project", "datasets", "rubric", "reference", "submissions", "grader"):
            (project / folder).mkdir()
        (project / "submissions" / "1-2 - auser A User - Oct 1, 2026 718 PM").mkdir()
        status = project_status(project, cwd=self.workspace)
        self.assertIn('".\\projects\\demo prj3"', status["next"]["action"])

    # Finding the project.

    def test_resolve_by_name_from_workspace_projects_and_project_folder(self):
        self.assertEqual(resolve_status_target("demo_fa26_prj2", self.workspace),
                         ("project", self.project))
        self.assertEqual(resolve_status_target("demo_fa26_prj2", self.workspace / "projects"),
                         ("project", self.project))
        self.assertEqual(resolve_status_target("demo_fa26_prj2", self.project),
                         ("project", self.project))
        self.assertEqual(resolve_status_target(".\\projects\\demo_fa26_prj2".replace(
            "\\", os.sep), self.workspace), ("project", self.project))

    def test_resolve_without_name(self):
        self.assertEqual(resolve_status_target(None, self.workspace),
                         ("list", self.workspace / "projects"))
        self.assertEqual(resolve_status_target(None, self.workspace / "projects"),
                         ("list", self.workspace / "projects"))
        self.assertEqual(resolve_status_target(None, self.project), ("project", self.project))
        elsewhere = self.workspace / "elsewhere"
        elsewhere.mkdir()
        with self.assertRaises(StatusError):
            resolve_status_target(None, elsewhere)

    def test_unknown_project_lists_the_available_ones(self):
        with self.assertRaises(StatusError) as caught:
            resolve_status_target("nope", self.workspace)
        self.assertIn("demo_fa26_prj2", str(caught.exception))

    def test_overview_lists_every_project(self):
        self.set_up_folders(unrecognized=1)
        self.add_files()
        (self.workspace / "projects" / "other_prj3").mkdir()
        overview = projects_overview(self.workspace / "projects", cwd=self.workspace)
        self.assertEqual([item["project"] for item in overview],
                         ["demo_fa26_prj2", "other_prj3"])
        self.assertEqual((overview[0]["step"], overview[0]["title"]), (3, "Draft the rubric"))
        text = format_overview(self.workspace / "projects", overview)
        self.assertIn("step 3 of 10: Draft the rubric  (1 item needs attention)", text)
        self.assertIn("step 1 of 10: Set up the project folder", text)

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
        text = self.run_cli("demo_fa26_prj2")
        self.assertIn("[done]  1. Set up the project folder", text)
        self.assertIn("[next]  3. Draft the rubric", text)
        self.assertIn("Needs attention:", text)
        self.assertIn("python -m ai_grader prepare-project .\\projects\\demo_fa26_prj2", text)
        for secret in NAMES + USERNAMES + ["LateStudent"]:
            self.assertNotIn(secret, text)

        data = json.loads(self.run_cli("demo_fa26_prj2", "--json"))
        self.assertEqual(data["current_stage"], "draft")

        listing = self.run_cli()
        self.assertIn("demo_fa26_prj2  step 3 of 10", listing)

        inside = self.run_cli(cwd=self.project)
        self.assertIn("Project: demo_fa26_prj2", inside)
        self.assertIn("prepare-project .", inside)

    def test_cli_unknown_project_exits_with_message(self):
        output = io.StringIO()
        previous = Path.cwd()
        os.chdir(self.workspace)
        try:
            with patch("sys.argv", ["ai_grader", "status", "missing"]), \
                 patch("sys.stdout", output), self.assertRaises(SystemExit):
                main()
        finally:
            os.chdir(previous)
        self.assertIn("Project not found: missing", output.getvalue())

    def test_status_writes_nothing(self):
        self.set_up_folders()
        self.add_files()
        before = sorted(str(path) for path in self.workspace.rglob("*"))
        self.run_cli("demo_fa26_prj2")
        self.run_cli()
        after = sorted(str(path) for path in self.workspace.rglob("*"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
