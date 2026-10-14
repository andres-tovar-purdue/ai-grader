import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_grader.cli import main


class CliTests(unittest.TestCase):
    @patch("ai_grader.cli.prepare_project")
    @patch("sys.argv", ["ai_grader", "prepare-project", "demo"])
    def test_prepare_project_command(self, prepare):
        prepare.return_value = {
            "output_paths": {
                "project_instructions": Path("project/project_instructions.md"),
                "instructor_rubric": Path("rubric/instructor_rubric.md"),
                "reference_solution": Path("reference/reference_solution.md"),
            },
            "limitations": [],
        }
        output = io.StringIO()

        with patch("sys.stdout", output):
            main()

        prepare.assert_called_once_with("demo")
        self.assertIn("Instructor review required", output.getvalue())

    def test_approval_next_command_refreshes_existing_submissions(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "grader/anonymized_submissions").mkdir(parents=True)
            spec_path = project / "grader/grading_spec_v002.json"
            result = (spec_path, {"approval": {"approved_by": "Instructor"}}, [])
            output = io.StringIO()
            with patch("ai_grader.cli.approve_grading_spec", return_value=result), \
                 patch.dict("os.environ", {"GRADER_INSTRUCTOR_NAME": "Test Instructor"}), \
                 patch("sys.argv", ["ai_grader", "approve-spec", str(spec_path)]), \
                 patch("sys.stdout", output):
                main()
            self.assertIn('refresh-submission-checks', output.getvalue())
            self.assertNotIn('prepare-submissions', output.getvalue())

    @patch("ai_grader.cli.grade_submissions")
    @patch("sys.argv", ["ai_grader", "grade-submissions", "demo"])
    def test_grade_submissions_command(self, grade):
        grade.return_value = (
            Path("grader/grading_runs/run_v001"),
            Path("grader/grading_runs/run_v001/grading_results.json"),
            Path("grader/grading_runs/run_v001/preliminary_grading_report.csv"),
            {"submission_count": 2},
        )
        output = io.StringIO()

        with patch("sys.stdout", output):
            main()

        grade.assert_called_once_with(
            "demo",
            model=None,
            rounding_policy="generous-v1",
        )
        self.assertIn("Final instructor scores were not assigned", output.getvalue())

    @patch("ai_grader.cli.grade_submissions")
    @patch(
        "sys.argv",
        ["ai_grader", "grade-submissions", "demo", "--model", "test-model"],
    )
    def test_grade_submissions_model_override(self, grade):
        grade.return_value = (
            Path("grader/grading_runs/run_v001"),
            Path("grader/grading_runs/run_v001/grading_results.json"),
            Path("grader/grading_runs/run_v001/preliminary_grading_report.csv"),
            {"submission_count": 2},
        )
        with patch("sys.stdout", new=io.StringIO()):
            main()
        grade.assert_called_once_with(
            "demo",
            model="test-model",
            rounding_policy="generous-v1",
        )

    @patch("ai_grader.cli.grade_submissions")
    @patch(
        "sys.argv",
        [
            "ai_grader",
            "grade-submissions",
            "demo",
            "--rounding-policy",
            "exact-v1",
        ],
    )
    def test_grade_submissions_rounding_override(self, grade):
        grade.return_value = (
            Path("grader/grading_runs/run_v001"),
            Path("grader/grading_runs/run_v001/grading_results.json"),
            Path("grader/grading_runs/run_v001/preliminary_grading_report.csv"),
            {"submission_count": 2},
        )
        with patch("sys.stdout", new=io.StringIO()):
            main()
        grade.assert_called_once_with(
            "demo",
            model=None,
            rounding_policy="exact-v1",
        )


if __name__ == "__main__":
    unittest.main()
