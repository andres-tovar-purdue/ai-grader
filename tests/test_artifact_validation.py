import tempfile
import unittest
from pathlib import Path

from ai_grader.artifact_validation import inspect_slx_package


class ArtifactValidationTests(unittest.TestCase):
    def test_classifies_empty_and_substituted_slx_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty = root / "empty.slx"
            empty.write_bytes(b"")
            substituted = root / "substituted.slx"
            substituted.write_text("not a Simulink package", encoding="utf-8")

            self.assertEqual(
                inspect_slx_package(empty)["status"],
                "corrupted_or_unreadable",
            )
            self.assertEqual(
                inspect_slx_package(substituted)["status"],
                "wrong_or_mislabeled_deliverable",
            )


if __name__ == "__main__":
    unittest.main()
