"""Merging Brightspace attempts and redacting name parts. Synthetic data only."""

import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_grader.cli import main
from ai_grader.repo_evidence import submission_cutoff
from ai_grader.submission_checks import refresh_submission_checks
from ai_grader.submission_processing import (
    name_handles, name_parts, redact_identity, write_submission_manifest,
)
import test_grading as grading_fixtures


# Folder prefixes run opposite to submission time, so folder-name order
# cannot be mistaken for attempt order.
ATTEMPTS = [
    ("300-1 - abc123 Jane Chen - Oct 1, 2026 0900 AM", {
        "chen_prj1_2.ipynb": '{"cells": ["Notebook by Jane Chen"]}',
        "analysis.py": "# Chen: kitchen-sink analysis\nprint('ok')\n",
        "data.csv": "x\n1\n",
        "report.pdf": b"%PDF attempt 1",
    }),
    ("200-1 - abc123 Jane Chen - Oct 2, 2026 0900 AM", {
        "data.csv": "x\n2\n",
        "report.pdf": b"%PDF attempt 2",
    }),
    ("100-1 - abc123 Jane Chen - Oct 3, 2026 1130 PM", {
        "report.pdf": b"%PDF attempt 3",
    }),
]
SINGLE = ("400-1 - def456 Sam Sample - Oct 2, 2026 1000 AM", {"main.py": "print(1)\n"})
IDENTITY_STRINGS = ["chen", "jane", "abc123", "sam sample", "def456", "300-1", "200-1"]


def write_attempt(root, folder_name, files):
    folder = root / "submissions" / folder_name
    folder.mkdir(parents=True)
    for name, content in files.items():
        if isinstance(content, bytes):
            (folder / name).write_bytes(content)
        else:
            (folder / name).write_text(content, encoding="utf-8")
    return folder


def make_project(root):
    for folder_name, files in [*ATTEMPTS, SINGLE]:
        write_attempt(root, folder_name, files)


class AttemptMergeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        make_project(self.root)
        _, map_path, self.manifest = write_submission_manifest(self.root)
        self.student_map = json.loads(map_path.read_text(encoding="utf-8"))
        self.chen = self.manifest["submissions"][0]
        self.anonymized = self.root / "grader" / "anonymized_submissions" / "Student_001"

    def artifact(self, original_name):
        record = next(item for item in self.student_map["Student_001"]["files"]
                      if item["original_path"] == original_name)
        return self.anonymized / record["artifact_path"], record

    def test_union_of_all_attempts_with_newest_copy_kept(self):
        self.assertEqual(self.chen["file_count"], 4)
        self.assertEqual(len(list(self.anonymized.iterdir())), 4)
        report, _ = self.artifact("report.pdf")
        self.assertEqual(report.read_bytes(), b"%PDF attempt 3")
        data, _ = self.artifact("data.csv")
        self.assertEqual(data.read_text(encoding="utf-8"), "x\n2\n")
        notebook, _ = self.artifact("chen_prj1_2.ipynb")
        self.assertTrue(notebook.is_file())   # Only in the first attempt.
        script, _ = self.artifact("analysis.py")
        self.assertTrue(script.is_file())

    def test_private_map_records_the_attempt_of_each_file(self):
        identity = self.student_map["Student_001"]
        sources = {item["original_path"]: (item["attempt"], item["source_folder"],
                                           item["replaced_attempts"])
                   for item in identity["files"]}
        self.assertEqual(sources, {
            "analysis.py": (1, ATTEMPTS[0][0], []),
            "chen_prj1_2.ipynb": (1, ATTEMPTS[0][0], []),
            "data.csv": (2, ATTEMPTS[1][0], [1]),
            "report.pdf": (3, ATTEMPTS[2][0], [1, 2]),
        })
        self.assertEqual(identity["newest_source_folder"], ATTEMPTS[2][0])
        self.assertEqual([item["source_folder"] for item in identity["attempts"]],
                         [folder for folder, _ in ATTEMPTS])

    def test_manifest_reports_only_counts(self):
        self.assertEqual(self.chen["attempt_count"], 3)
        self.assertEqual(self.chen["files_replaced_by_newer_attempts"], 2)
        self.assertIn("3 attempts", self.chen["review_flags"][0])
        self.assertIn("merged", self.chen["review_flags"][0])
        single = self.manifest["submissions"][1]
        self.assertEqual((single["attempt_count"], single["files_replaced_by_newer_attempts"],
                          single["review_flags"]), (1, 0, []))
        serialized = json.dumps(self.manifest).casefold()
        for text in IDENTITY_STRINGS:
            # Whole tokens: "kitchen" legitimately contains "chen".
            self.assertIsNone(
                re.search(r"(?<![^\W\d_])" + re.escape(text) + r"(?![^\W\d_])", serialized),
                text)
        self.assertIn("kitchen-sink", serialized)
        for item in self.chen["files"]:
            self.assertNotIn("attempt", item)
            self.assertNotIn("original_path", item)

    def test_submission_time_is_the_newest_attempt(self):
        cutoff = submission_cutoff(self.student_map["Student_001"])
        self.assertEqual((cutoff.month, cutoff.day, cutoff.hour, cutoff.minute, cutoff.second),
                         (10, 3, 23, 30, 59))

    def test_refresh_checks_accept_merged_attempts(self):
        spec, _, _ = grading_fixtures.GradingTests().make_project(self.root / "fixture")
        (self.root / "grader" / "grading_spec_v001.json").write_text(
            json.dumps(spec), encoding="utf-8")
        _, manifest = refresh_submission_checks(self.root)
        self.assertIn("deliverable_checks", manifest["submissions"][0])

    def test_cli_reports_counts_without_names(self):
        output = io.StringIO()
        with patch("sys.argv", ["ai_grader", "prepare-submissions", str(self.root)]), \
             patch("sys.stdout", output):
            main()
        text = output.getvalue()
        self.assertIn("Student_001: 3 attempts merged, 2 file(s) replaced by a newer attempt",
                      text)
        self.assertNotIn("Student_002:", text)
        for name in ("Chen", "Jane", "abc123", "Sam"):
            self.assertNotIn(name, text)


class NameRedactionTests(unittest.TestCase):
    def redact(self, text, full_name="Jane Chen", username="abc123"):
        return redact_identity(text, username, full_name)

    def test_surname_alone_in_file_names_and_text(self):
        self.assertEqual(self.redact("chen_prj1_2.ipynb"), "<student_name>_prj1_2.ipynb")
        self.assertEqual(self.redact("CHEN-2026.py"), "<student_name>-2026.py")
        self.assertEqual(self.redact("by Chen, (chen) chen2 2chen"),
                         "by <student_name>, (<student_name>) <student_name>2 2<student_name>")

    def test_first_name_alone(self):
        self.assertEqual(self.redact("Jane wrote jane_notes.txt"),
                         "<student_name> wrote <student_name>_notes.txt")

    def test_letters_on_either_side_mean_no_match(self):
        self.assertEqual(self.redact("kitchen Chenille Janet"), "kitchen Chenille Janet")

    def test_full_name_and_username_still_redacted(self):
        self.assertEqual(self.redact("Jane Chen (abc123), Jane_Chen"),
                         "<student_name> (<username>), <student_name>")

    def test_placeholders_are_not_rewritten(self):
        # A surname that is also a placeholder word.
        self.assertEqual(self.redact("% Jane Student and Student", "Jane Student"),
                         "% <student_name> and <student_name>")
        self.assertEqual(self.redact("<repo_owner> Chen"), "<repo_owner> <student_name>")

    def test_hyphenated_names_and_initials(self):
        self.assertEqual(name_parts("Mary-Jane L. Chen"), ["Chen", "Jane", "Mary"])
        self.assertEqual(self.redact("mary_jane.py by Jane L. Chen; L. stays", "Mary-Jane L. Chen"),
                         "<student_name>_<student_name>.py by <student_name> L. "
                         "<student_name>; L. stays")
        self.assertEqual(name_parts("Ann B Chen"), ["Chen", "Ann"])

    def test_handles_built_from_name_parts_are_redacted(self):
        # Surname plus initial, initial plus surname, and joined parts,
        # in file names and text, for a handle that is not the username.
        self.assertEqual(
            self.redact("smithj_beam_ai.py jsmith.ipynb Author: JohnSmith2 smith_j", "John Smith"),
            "<student_name>_beam_ai.py <student_name>.ipynb Author: <student_name>2 "
            "<student_name>_j")
        self.assertEqual(self.redact("smithjohn, Smith-John", "John Smith"),
                         "<student_name>, <student_name>-<student_name>")

    def test_handles_leave_other_words_alone(self):
        # Letters on either side still block a match.
        self.assertEqual(self.redact("blacksmiths ajsmithx jsmiths", "John Smith"),
                         "blacksmiths ajsmithx jsmiths")
        # A short part is not combined with an initial: "away" is not "Al Way".
        self.assertEqual(self.redact("go away, wayA", "Al Way"), "go away, wayA")
        self.assertNotIn("away", name_handles("Al Way"))

    def test_name_handles(self):
        self.assertEqual(sorted(name_handles("John Smith")),
                         sorted(["JohnSmith", "SmithJohn", "JSmith", "SmithJ", "SJohn", "JohnS"]))
        self.assertIn("ChenJ", name_handles("Mary-Jane L. Chen"))
        self.assertEqual(name_handles("Cher"), [])

    def test_prepared_files_have_no_name_parts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_project(root)
            write_submission_manifest(root)
            anonymized = root / "grader" / "anonymized_submissions" / "Student_001"
            names = [path.name for path in anonymized.iterdir()]
            self.assertIn("artifact_002_student_prj1_2.ipynb", names)
            for name in names:
                self.assertNotIn("chen", name.casefold())
            script = next(path for path in anonymized.iterdir() if path.suffix == ".py")
            self.assertEqual(script.read_text(encoding="utf-8"),
                             "# <student_name>: kitchen-sink analysis\nprint('ok')\n")
            notebook = next(path for path in anonymized.iterdir() if path.suffix == ".ipynb")
            self.assertNotIn("jane", notebook.read_text(encoding="utf-8").casefold())


if __name__ == "__main__":
    unittest.main()
