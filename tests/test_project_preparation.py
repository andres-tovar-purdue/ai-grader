import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ai_grader.project_preparation import prepare_project


def valid_artifacts():
    return {
        "project_instructions": (
            "# Draft Project Instructions\n\n"
            "## Instructor Review Required\n\nReview before generate-spec."
        ),
        "instructor_rubric": (
            "# Draft Instructor Rubric\n\n"
            "## Instructor Review Required\n\n"
            "Allocation status: Proposed — instructor review required\n\n"
            "## Proposed Allocations Requiring Instructor Review\n\n"
            "Criterion 1.1: 10 points"
        ),
        "reference_solution": (
            "# Draft Reference Solution\n\n"
            "## Instructor Review Required\n\n"
            "This is not the only acceptable solution."
        ),
    }


class FakeResponses:
    def __init__(self, output):
        self.output = output
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=self.output)


class ProjectPreparationTests(unittest.TestCase):
    def make_project(self, root):
        for folder in ("project", "datasets", "submissions"):
            (root / folder).mkdir()
        original = root / "project" / "assignment.txt"
        original.write_text("Complete Task 1 for 10 points.", encoding="utf-8")
        (root / "datasets" / "values.csv").write_text(
            "x,y\n1,2\n3,4\n", encoding="utf-8"
        )
        (root / "submissions" / "student.txt").write_text(
            "PRIVATE STUDENT CONTENT", encoding="utf-8"
        )
        return original

    def test_prepares_three_drafts_and_preserves_originals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = self.make_project(root)
            responses = FakeResponses(json.dumps(valid_artifacts()))
            client = SimpleNamespace(responses=responses)

            result = prepare_project(root, client=client)

            self.assertEqual(original.read_text(encoding="utf-8"),
                             "Complete Task 1 for 10 points.")
            self.assertTrue((root / "project" / "project_instructions.md").is_file())
            self.assertTrue((root / "rubric" / "instructor_rubric.md").is_file())
            self.assertTrue((root / "reference" / "reference_solution.md").is_file())
            prompt = responses.calls[0]["input"]
            self.assertIn("x,y\n1,2\n3,4", prompt)
            self.assertNotIn("PRIVATE STUDENT CONTENT", prompt)
            self.assertEqual(result["limitations"], [])
            self.assertFalse((root / "grader" / "grading_spec_v001.json").exists())

    def test_existing_artifact_blocks_before_api_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_project(root)
            output = root / "project" / "project_instructions.md"
            output.write_text("Reviewed content", encoding="utf-8")
            responses = FakeResponses(json.dumps(valid_artifacts()))

            with self.assertRaisesRegex(FileExistsError, "Refusing to overwrite"):
                prepare_project(root, client=SimpleNamespace(responses=responses))

            self.assertEqual(output.read_text(encoding="utf-8"), "Reviewed content")
            self.assertEqual(responses.calls, [])

    def test_invalid_response_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_project(root)
            artifacts = valid_artifacts()
            artifacts["reference_solution"] = "missing required headings"
            client = SimpleNamespace(
                responses=FakeResponses(json.dumps(artifacts))
            )

            with self.assertRaisesRegex(ValueError, "missing required heading"):
                prepare_project(root, client=client)

            for relative_path in (
                "project/project_instructions.md",
                "rubric/instructor_rubric.md",
                "reference/reference_solution.md",
            ):
                self.assertFalse((root / relative_path).exists())


if __name__ == "__main__":
    unittest.main()
