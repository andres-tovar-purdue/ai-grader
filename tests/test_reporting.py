import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_grader.cli import main
from ai_grader.finalization import load_approved_scores
from ai_grader.reporting import generate_review_report, open_report


class ReportingTests(unittest.TestCase):
    def prepare(self, root, version="run_v004", invalid=False):
        grader = root / "grader"
        run = grader / "grading_runs" / version
        run.mkdir(parents=True)
        (grader / "student_map.json").write_text(json.dumps({
            "Student_001": {"full_name": "Zoë Student, Jr."},
            "Student_002": {"full_name": "Avery Student"},
        }), encoding="utf-8")
        (run / "grading_results.json").write_text(json.dumps({"results": [
            {"student_id": "Student_001"}, {"student_id": "Student_002"},
        ]}), encoding="utf-8")
        with (run / "preliminary_grading_report.csv").open(
                "w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["student_id", "total_agent_score",
                        "total_instructor_score", "validation_status", "task_1_feedback"])
            writer.writeheader()
            for index in (1, 2):
                writer.writerow({"student_id": f"Student_{index:03d}",
                    "total_agent_score": "" if invalid and index == 2 else "98",
                    "total_instructor_score": "99",
                    "validation_status": "invalid_model_response" if invalid and index == 2 else "validated",
                    "task_1_feedback": "Missing plot (-2)."})
        return run

    def read(self, path):
        with path.open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))

    def test_names_feedback_and_blank_approval_without_changing_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self.prepare(root)
            originals = {path: path.read_bytes() for path in (run / "grading_results.json",
                       run / "preliminary_grading_report.csv", root / "grader/student_map.json")}
            output = generate_review_report(root, "run_v004")
            rows = self.read(output)
            self.assertEqual(rows[0]["student_name"], "Zoë Student, Jr.")
            self.assertEqual(rows[0]["task_1_feedback"], "Missing plot (-2).")
            self.assertTrue(all(row["total_instructor_score"] == "" for row in rows))
            self.assertTrue(output.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertEqual(originals, {path: path.read_bytes() for path in originals})
            with self.assertRaisesRegex(ValueError, "blank"):
                load_approved_scores(output)
            for row in rows:
                row["total_instructor_score"] = "98"
            with output.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            self.assertEqual(load_approved_scores(output), {"Student_001": 98, "Student_002": 98})

    def test_repeat_preserves_existing_instructor_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            first = generate_review_report(root)
            first.write_text("instructor edits", encoding="utf-8")
            second = generate_review_report(root)
            self.assertEqual(second.name, "instructor_review_v002.csv")
            self.assertEqual(first.read_text(), "instructor edits")

    def test_latest_valid_run_and_explicit_older_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root, "run_v003")
            self.prepare(root, "run_v004")
            (root / "grader/grading_runs/run_v_backup").mkdir()
            self.assertEqual(generate_review_report(root).parent.name, "run_v004")
            self.assertEqual(generate_review_report(root, "run_v003").parent.name, "run_v003")

    def test_roster_mismatch_duplicate_rows_and_missing_names_write_nothing(self):
        for case in ("roster", "duplicate", "name"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                run = self.prepare(root)
                if case == "duplicate":
                    path = run / "preliminary_grading_report.csv"
                    with path.open("a", encoding="utf-8") as stream:
                        stream.write("Student_001,98,,validated,Feedback\n")
                else:
                    path = root / "grader/student_map.json"
                    mapping = json.loads(path.read_text())
                    if case == "roster":
                        del mapping["Student_002"]
                    else:
                        mapping["Student_002"]["full_name"] = ""
                    path.write_text(json.dumps(mapping))
                with self.assertRaises(ValueError):
                    generate_review_report(root)
                self.assertFalse(list(run.glob("instructor_review*.csv")))

    def test_unscored_response_remains_blank_and_flagged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root, invalid=True)
            row = self.read(generate_review_report(root))[1]
            self.assertEqual(row["total_agent_score"], "")
            self.assertEqual(row["validation_status"], "invalid_model_response")

    def test_invalid_run_reference_and_no_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            for version in (None, "../../elsewhere"):
                with self.assertRaises(ValueError):
                    generate_review_report(directory, version)

    def test_cli_generates_opens_and_prints_next_command_without_api(self):
        with tempfile.TemporaryDirectory() as directory:
            # Resolve so the expected path matches the CLI's resolved path even
            # when TEMP uses a Windows short name (e.g. RUNNER~1 on CI).
            root = Path(directory).resolve()
            self.prepare(root)
            output = io.StringIO()
            with patch("sys.argv", ["ai_grader", "generate-report", str(root), "--run", "run_v004",
                                    "--open", "--template-comments"]), \
                 patch("ai_grader.grade_adjustment.get_client",
                       side_effect=AssertionError("No API calls")), \
                 patch("sys.stdout", output), patch("ai_grader.cli.open_report") as opener, \
                 patch("ai_grader.grading.get_client", side_effect=AssertionError("No API calls")), \
                 patch("ai_grader.cli.finalize_grading", side_effect=AssertionError("No finalization")):
                main()
            report = root / "grader/grading_runs/run_v004/instructor_review_v001.csv"
            opener.assert_called_once_with(report)
            self.assertIn("--scores-file", output.getvalue())
            self.assertIn("AI grades stay preliminary until you finalize", output.getvalue())
            header = report.read_text(encoding="utf-8-sig").splitlines()[0].split(",")
            self.assertEqual(header[:6], ["student_name", "student_id", "total_agent_score",
                                          "total_instructor_score", "flag", "final_comment"])

    def test_windows_opens_with_default_application(self):
        with patch("ai_grader.reporting.sys.platform", "win32"), \
             patch("ai_grader.reporting.os.startfile", create=True) as start:
            open_report(Path("review.csv"))
        start.assert_called_once_with("review.csv")
