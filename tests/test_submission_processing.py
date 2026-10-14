import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_grader.submission_processing import write_submission_manifest


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

    def test_refuses_to_overwrite_anonymized_copies(self):
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
            write_submission_manifest(root)

            with self.assertRaisesRegex(FileExistsError, "Refusing to overwrite"):
                write_submission_manifest(root)

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


if __name__ == "__main__":
    unittest.main()
