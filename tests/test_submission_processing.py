import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_grader.cli import main
from ai_grader.submission_processing import (
    SubmissionsAlreadyGradedError, write_submission_manifest,
)


class SubmissionAnonymizationTests(unittest.TestCase):
    def test_writes_identity_free_manifest_and_physical_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = (
                root
                / "submissions"
                / "123-456 - abc123 Jane Student - Jul 30, 2026 1053 PM"
            )
            source.mkdir(parents=True)
            code = source / "abc123_project2_matlab.m"
            code.write_text(
                "% Jane Student\nsaveas(gcf, 'abc123_project2_plot.png')\n",
                encoding="utf-8",
            )
            image = source / "Jane_Student_project2_plot.png"
            image.write_bytes(b"not-a-real-image")

            manifest_path, map_path, manifest = write_submission_manifest(root)

            serialized = manifest_path.read_text(encoding="utf-8")
            self.assertNotIn("abc123", serialized.lower())
            self.assertNotIn("jane student", serialized.lower())
            self.assertNotIn("jane_student", serialized.lower())
            self.assertNotIn("123-456 -", serialized)
            self.assertEqual(manifest["manifest_version"], "2.0")
            self.assertEqual(manifest["anonymization"]["status"], "validated")

            student_root = root / "grader" / "anonymized_submissions" / "Student_001"
            copied = list(student_root.iterdir())
            self.assertEqual(len(copied), 2)
            copied_code = next(path for path in copied if path.suffix == ".m")
            copied_text = copied_code.read_text(encoding="utf-8")
            self.assertIn("<student_name>", copied_text)
            self.assertIn("<username>_project2_plot.png", copied_text)
            self.assertEqual(code.read_text(encoding="utf-8").splitlines()[0],
                             "% Jane Student")

            private_map = json.loads(map_path.read_text(encoding="utf-8"))
            self.assertEqual(private_map["Student_001"]["username"], "abc123")

    def test_publication_does_not_require_windows_directory_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = (
                root
                / "submissions"
                / "123-456 - abc123 Jane Student - Jul 30, 2026 1053 PM"
            )
            source.mkdir(parents=True)
            (source / "abc123_project2_matlab.m").write_text(
                "disp('ok')", encoding="utf-8"
            )

            with patch.object(
                Path,
                "rename",
                side_effect=PermissionError("Windows directory rename denied"),
            ) as rename:
                write_submission_manifest(root)

            rename.assert_not_called()
            self.assertTrue(
                (root / "grader" / "anonymized_submissions" / "Student_001").is_dir()
            )


class RerunTests(unittest.TestCase):
    """prepare-submissions may be rerun until grading has used its output."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.grader = self.root / "grader"
        self.add_student("123-456 - abc123 Jane Student - Jul 30, 2026 1053 PM",
                         "abc123_project2_matlab.m")

    def add_student(self, folder_name, filename):
        source = self.root / "submissions" / folder_name
        source.mkdir(parents=True)
        (source / filename).write_text("disp('ok')", encoding="utf-8")

    def grader_snapshot(self):
        return {
            path.relative_to(self.grader).as_posix():
                None if path.is_dir() else path.read_bytes()
            for path in sorted(self.grader.rglob("*"))
        }

    def test_rerun_before_grading_replaces_output_and_manifest(self):
        write_submission_manifest(self.root)
        stale = self.grader / "anonymized_submissions" / "Student_001" / "stale.txt"
        stale.write_text("from an earlier preparation", encoding="utf-8")
        self.add_student("123-457 - def456 Sam Sample - Jul 30, 2026 1100 PM",
                         "def456_project2_matlab.m")

        manifest_path, map_path, manifest = write_submission_manifest(self.root)

        self.assertFalse(stale.exists())
        self.assertEqual(manifest["submission_count"], 2)
        saved = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["submission_count"], 2)
        self.assertEqual(sorted(json.loads(map_path.read_text(encoding="utf-8"))),
                         ["Student_001", "Student_002"])
        self.assertEqual(
            sorted(p.name for p in (self.grader / "anonymized_submissions").iterdir()),
            ["Student_001", "Student_002"])

    def test_empty_grading_folders_do_not_block_a_rerun(self):
        write_submission_manifest(self.root)
        (self.grader / "grading_runs").mkdir()
        (self.grader / "debug_responses").mkdir()
        write_submission_manifest(self.root)

    def test_refuses_after_grading_without_changes(self):
        for evidence in ("grading_runs/run_v001/grading_results.json",
                         "debug_responses/Student_001_20261019_120000_000000.json"):
            with self.subTest(evidence=evidence):
                write_submission_manifest(self.root)
                path = self.grader / evidence
                path.parent.mkdir(parents=True)
                path.write_text("{}", encoding="utf-8")
                before = self.grader_snapshot()
                with self.assertRaises(SubmissionsAlreadyGradedError) as raised:
                    write_submission_manifest(self.root)
                self.assertIn("Nothing was changed", str(raised.exception))
                self.assertNotIn("abc123", str(raised.exception))
                self.assertEqual(self.grader_snapshot(), before)
                shutil.rmtree(self.grader)

    def test_failed_rerun_keeps_earlier_preparation(self):
        write_submission_manifest(self.root)
        before = self.grader_snapshot()
        with patch("ai_grader.submission_processing.validate_anonymized_manifest",
                   side_effect=RuntimeError("validation failed")):
            with self.assertRaises(RuntimeError):
                write_submission_manifest(self.root)
        self.assertEqual(self.grader_snapshot(), before)


class PrepareSubmissionsCommandTests(unittest.TestCase):
    def run_cli(self, project):
        output = io.StringIO()
        with patch("sys.argv", ["ai_grader", "prepare-submissions", str(project)]), \
             patch("sys.stdout", output):
            try:
                main()
                code = 0
            except SystemExit as exit:
                code = exit.code
        return code, output.getvalue()

    def test_rerun_says_it_replaced_and_refusal_has_no_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "submissions" / "123-456 - abc123 Jane Student - Jul 30, 2026 1053 PM"
            source.mkdir(parents=True)
            (source / "abc123_project2_matlab.m").write_text("disp('ok')", encoding="utf-8")

            code, output = self.run_cli(root)
            self.assertEqual(code, 0)
            self.assertNotIn("Replaced the earlier", output)

            code, output = self.run_cli(root)
            self.assertEqual(code, 0)
            self.assertIn("Replaced the earlier anonymized submissions", output)

            (root / "grader" / "grading_runs" / "run_v001").mkdir(parents=True)
            code, output = self.run_cli(root)
            self.assertEqual(code, 1)
            self.assertIn("Refusing to replace the prepared submissions", output)
            self.assertIn("Nothing was changed", output)
            self.assertNotIn("Traceback", output)


if __name__ == "__main__":
    unittest.main()
