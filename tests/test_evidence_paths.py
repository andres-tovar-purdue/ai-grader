"""Evidence cited without its folders resolves to the one matching artifact."""

import tempfile
import unittest
from pathlib import Path

from ai_grader.grading_validation import calculate_grading_result, resolve_artifact_path
import test_grading as grading_fixtures


PATHS = [
    "artifact_001_student_beam_ai.py",
    "github_repo/artifact_010_beam_model.py",
    "github_repo/beam_project/artifact_013_beam_model.py",
    "student_files.zip_contents/artifact_020_summary.csv",
]


class ResolveArtifactPathTests(unittest.TestCase):
    def test_name_without_folders_resolves(self):
        self.assertEqual(resolve_artifact_path("artifact_010_beam_model.py", PATHS),
                         "github_repo/artifact_010_beam_model.py")
        self.assertEqual(resolve_artifact_path("artifact_013_beam_model.py", PATHS),
                         "github_repo/beam_project/artifact_013_beam_model.py")
        self.assertEqual(resolve_artifact_path("artifact_020_summary.csv", PATHS),
                         "student_files.zip_contents/artifact_020_summary.csv")
        # A wrong or Windows-style folder still names one artifact.
        self.assertEqual(resolve_artifact_path("beam_project\\artifact_010_beam_model.py", PATHS),
                         "github_repo/artifact_010_beam_model.py")

    def test_unknown_or_mismatched_names_do_not_resolve(self):
        for cited in ("artifact_011_beam_model.py",      # no such number
                      "artifact_010_other.py",           # number exists, name differs
                      "beam_model.py",                   # no artifact number
                      "artifact_010_beam_model.PY"):     # different name
            self.assertIsNone(resolve_artifact_path(cited, PATHS), cited)

    def test_ambiguous_number_does_not_resolve(self):
        paths = ["a/artifact_005_x.py", "b/artifact_005_x.py"]
        self.assertIsNone(resolve_artifact_path("artifact_005_x.py", paths))


class CalculateWithShortPathTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.spec, _, _ = grading_fixtures.GradingTests().make_project(
            Path(self.directory.name))
        self.submission = {
            "student_id": "Student_001",
            "files": [{"path": "github_repo/artifact_001_student_project2_matlab.m",
                       "file_type": "text", "extension": ".m", "size_bytes": 1}],
            "review_flags": [],
        }

    def test_short_path_is_resolved_and_recorded(self):
        response = grading_fixtures.model_response()
        result = calculate_grading_result(response, self.spec, self.submission)
        evidence = result["tasks"][0]["criteria"][0]["evidence"][0]
        self.assertEqual(evidence["artifact_path"],
                         "github_repo/artifact_001_student_project2_matlab.m")
        self.assertEqual(evidence["artifact_path_as_cited"],
                         "artifact_001_student_project2_matlab.m")

    def test_unresolvable_path_is_still_rejected(self):
        response = grading_fixtures.model_response()
        response["criteria"][0]["evidence"][0]["artifact_path"] = "artifact_009_missing.m"
        with self.assertRaisesRegex(ValueError, "Unknown evidence path for T1_C1"):
            calculate_grading_result(response, self.spec, self.submission)


if __name__ == "__main__":
    unittest.main()
