import tempfile
import unittest
from pathlib import Path

from ai_grader.dataset_inspection import summarize_csv


class DatasetInspectionTests(unittest.TestCase):
    def test_small_csv_is_included_in_full(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "datasets" / "data.csv"
            path.parent.mkdir()
            path.write_text("x,y\n1,2\n", encoding="utf-8")

            result = summarize_csv(path, root)

            self.assertEqual(result["mode"], "full")
            self.assertEqual(result["content"], "x,y\n1,2\n")
            self.assertIsNone(result["limitation"])

    def test_large_csv_is_sampled_and_labeled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "datasets" / "data.csv"
            path.parent.mkdir()
            path.write_text("x,y\n1,2\n3,4\n", encoding="utf-8")

            result = summarize_csv(path, root, full_csv_max_bytes=5)

            self.assertEqual(result["mode"], "sampled")
            self.assertIn("exceeds the 5-byte", result["limitation"])
            self.assertEqual(result["row_count_including_header"], 3)


if __name__ == "__main__":
    unittest.main()
