"""Notebook decoding, PDF text, links in documents, archiving runs. Synthetic data only."""

import codecs
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_grader import submission_processing
from ai_grader.cli import main
from ai_grader.grading import build_student_input, grade_submissions, next_grading_run_path
from ai_grader.grading_validation import artifact_review_reasons
from ai_grader.repo_evidence import (
    LINK_SOURCE_DOCUMENT, add_document_links, verify_repository_redaction,
)
from ai_grader.run_archive import ArchiveError, archive_grading
from ai_grader.submission_checks import original_filename_checks
from ai_grader.submission_processing import (
    SubmissionsAlreadyGradedError, load_notebook, merge_attempt_files,
    write_submission_manifest,
)
from test_grading import FakeResponses, GradingTests, model_response
from test_repo_evidence import NoNetwork, RepositoryProject, files_of


def make_pdf(path, pages):
    """Write a small valid PDF; each page is a list of text lines (may be empty)."""

    def escape(line):
        return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    objects = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
               3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"}
    kids = []
    for number, lines in enumerate(pages):
        page_id, content_id = 4 + 2 * number, 5 + 2 * number
        kids.append(f"{page_id} 0 R")
        stream = "BT /F1 11 Tf 14 TL 72 720 Td " + " ".join(
            f"({escape(line)}) Tj T*" for line in lines) + " ET"
        stream = stream.encode("latin-1")
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode()
        objects[content_id] = (f"<< /Length {len(stream)} >>\nstream\n".encode()
                               + stream + b"\nendstream")
    objects[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode()

    data = b"%PDF-1.4\n"
    offsets = {}
    for object_id in sorted(objects):
        offsets[object_id] = len(data)
        data += f"{object_id} 0 obj\n".encode() + objects[object_id] + b"\nendobj\n"
    xref = len(data)
    size = max(objects) + 1
    data += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for object_id in range(1, size):
        data += f"{offsets[object_id]:010d} 00000 n \n".encode()
    data += (f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n").encode()
    Path(path).write_bytes(data)


NOTEBOOK = {"cells": [{"cell_type": "markdown", "source": ["# Results"]},
                      {"cell_type": "code", "source": "print(2)",
                       "outputs": [{"output_type": "stream", "text": "2\n"}]}],
            "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
FOLDER = "100-1 - abc123 Jane Chen - Oct 3, 2026 1130 PM"
SPEC = {"deliverables": []}
REPORT_LINES = [
    "Results table: RMSE 0.42 by Jane Chen",
    "Discussion: Chen found the kitchen sensor drifted.",
    "Limitations: one week of data.",
    "Code: https://github.com/jc-dev42/prj1-analysis",
]


def word(text, token):
    return re.search(r"(?<![^\W\d_])" + re.escape(token) + r"(?![^\W\d_])", text,
                     re.IGNORECASE)


class NotebookLoadingTests(unittest.TestCase):
    def test_byte_order_marks(self):
        text = json.dumps(NOTEBOOK)
        for data in (codecs.BOM_UTF8 + text.encode("utf-8"),
                     codecs.BOM_UTF16_LE + text.encode("utf-16-le"),
                     codecs.BOM_UTF16_BE + text.encode("utf-16-be"),
                     text.encode("utf-8"),
                     "﻿" + text):
            notebook, error = load_notebook(data)
            self.assertIsNone(error)
            self.assertEqual(len(notebook["cells"]), 2)

    def test_unreadable_notebooks_give_a_reason(self):
        self.assertIn("not valid JSON", load_notebook(b"{cells: oops")[1])
        self.assertIn("not a notebook", load_notebook(b"[1, 2]")[1])
        self.assertIn("not a notebook", load_notebook(b'{"cells": "x"}')[1])

    def notebook_project(self, root, notebook_bytes):
        text = notebook_bytes.decode("utf-8", errors="replace")
        GradingTests().make_project(root)
        grader = root / "grader"
        manifest = json.loads((grader / "submission_manifest.json").read_text(encoding="utf-8"))
        student_root = grader / "anonymized_submissions" / "Student_001"
        path = student_root / "artifact_002_analysis.ipynb"
        path.write_text(text, encoding="utf-8")
        submission = manifest["submissions"][0]
        submission["files"].append({"path": path.name, "file_type": "text",
                                    "extension": ".ipynb", "size_bytes": len(notebook_bytes),
                                    "content": path.read_text(encoding="utf-8")})
        submission["file_count"] = len(submission["files"])
        (grader / "submission_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return submission, student_root

    def test_bom_notebook_is_graded(self):
        with tempfile.TemporaryDirectory() as directory:
            submission, student_root = self.notebook_project(
                Path(directory), codecs.BOM_UTF8 + json.dumps(NOTEBOOK).encode("utf-8"))
            request = build_student_input(SPEC, submission, student_root, "Instructions.")
            prompt = request[0]["content"][0]["text"]
            self.assertIn("CELL 2 (code):\nprint(2)", prompt)
            self.assertEqual(artifact_review_reasons(submission), [])

    def test_invalid_notebook_is_flagged_and_the_batch_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.notebook_project(root, b'{"cells": [ truncated')
            responses = FakeResponses(model_response())
            with patch("sys.stdout", new=io.StringIO()):
                _, _, _, run = grade_submissions(root, SimpleNamespace(responses=responses))
            prompt = responses.calls[0]["input"][0]["content"][0]["text"]
            self.assertIn("NOTEBOOK COULD NOT BE READ: not valid JSON", prompt)
            result = run["results"][0]
            self.assertTrue(result["review_required"])
            self.assertTrue(any("could not be read as a notebook" in reason
                                for reason in result["review_reasons"]))


class PdfTextTests(unittest.TestCase):
    def prepare(self, pages, limit=None):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        folder = root / "submissions" / FOLDER
        folder.mkdir(parents=True)
        if pages is None:
            (folder / "report.pdf").write_bytes(b"%PDF-1.4 damaged")
        else:
            make_pdf(folder / "report.pdf", pages)
        (folder / "analysis.ipynb").write_text(json.dumps(NOTEBOOK), encoding="utf-8")
        with patch.object(submission_processing, "PDF_TEXT_CHARACTER_LIMIT",
                          limit or submission_processing.PDF_TEXT_CHARACTER_LIMIT):
            _, map_path, manifest = write_submission_manifest(root)
        self.root = root
        self.student_map = json.loads(map_path.read_text(encoding="utf-8"))
        self.submission = manifest["submissions"][0]
        self.pdf = next(f for f in self.submission["files"] if f["extension"] == ".pdf")
        return self.pdf

    def test_text_is_extracted_redacted_and_stored(self):
        pdf = self.prepare([REPORT_LINES[:2], REPORT_LINES[2:]])
        extraction = pdf["text_extraction"]
        self.assertEqual((extraction["status"], extraction["pages"], extraction["truncated"]),
                         ("extracted", 2, False))
        text = pdf["extracted_text"]
        self.assertIn("RMSE 0.42", text)
        self.assertIn("Limitations: one week of data.", text)
        self.assertIn("kitchen", text)
        self.assertIn("[Page 2]", text)
        for token in ("Jane", "Chen", "jc-dev42"):
            self.assertIsNone(word(text, token), token)
        self.assertIn("github.com/<repo_owner>/prj1-analysis", text)
        sidecar = (self.root / "grader" / "anonymized_submissions" / "Student_001"
                   / extraction["text_path"])
        self.assertEqual(extraction["text_path"], pdf["path"] + ".txt")
        self.assertEqual(sidecar.read_text(encoding="utf-8"), text)
        self.assertEqual(artifact_review_reasons(self.submission), [])

    def test_links_found_before_redaction_stay_private(self):
        self.prepare([REPORT_LINES])
        links = self.student_map["Student_001"]["document_github_links"]
        self.assertEqual(links, {"repositories": [["jc-dev42", "prj1-analysis"]],
                                 "profiles": []})
        manifest_text = (self.root / "grader" / "submission_manifest.json").read_text(
            encoding="utf-8")
        self.assertNotIn("jc-dev42", manifest_text)

    def test_long_text_is_truncated_and_says_so(self):
        pdf = self.prepare([REPORT_LINES], limit=40)
        self.assertTrue(pdf["text_extraction"]["truncated"])
        self.assertEqual(len(pdf["extracted_text"]), 40)
        self.assertTrue(any("Only part of the text" in reason
                            for reason in artifact_review_reasons(self.submission)))
        request = build_student_input(SPEC, self.submission,
                                      self.root / "grader/anonymized_submissions/Student_001",
                                      "Instructions.")
        self.assertIn("PDF TEXT TRUNCATED: only the first 40 of",
                      request[0]["content"][0]["text"])

    def test_grading_input_includes_text_and_limitations(self):
        self.prepare([REPORT_LINES])
        request = build_student_input(SPEC, self.submission,
                                      self.root / "grader/anonymized_submissions/Student_001",
                                      "Instructions.")
        prompt = request[0]["content"][0]["text"]
        self.assertIn("EVIDENCE TYPE: pdf_extracted_text", prompt)
        self.assertIn("Figures, images", prompt)
        self.assertIn("RMSE 0.42", prompt)

    def test_pdf_without_text_keeps_the_manual_check(self):
        pdf = self.prepare([[]])
        self.assertEqual(pdf["text_extraction"]["status"], "no_text")
        self.assertNotIn("extracted_text", pdf)
        self.assertIsNone(pdf["text_extraction"]["text_path"])
        self.assertEqual(artifact_review_reasons(self.submission),
                         [f"No text could be extracted from {pdf['path']}; "
                          "verify the PDF manually."])
        request = build_student_input(SPEC, self.submission,
                                      self.root / "grader/anonymized_submissions/Student_001",
                                      "Instructions.")
        self.assertIn("No text could be extracted from this PDF",
                      request[0]["content"][0]["text"])

    def test_damaged_pdf_is_unreadable_not_a_crash(self):
        pdf = self.prepare(None)
        self.assertEqual(pdf["text_extraction"]["status"], "unreadable")
        self.assertEqual(len(artifact_review_reasons(self.submission)), 1)

    def test_pdf_from_before_extraction_still_flagged(self):
        submission = {"files": [{"path": "artifact_001_report.pdf", "extension": ".pdf"}]}
        self.assertEqual(artifact_review_reasons(submission),
                         ["PDF contents of artifact_001_report.pdf were not inspected; "
                          "verify the PDF manually."])


class DocumentLinkTests(unittest.TestCase):
    STUDENT_MAP = {
        "Student_001": {"document_github_links": {"repositories": [["jc-dev42", "prj1"]],
                                                  "profiles": []}},
        "Student_002": {"document_github_links": {"repositories": [], "profiles": []}},
    }

    def test_no_links_anywhere_means_no_repository_evidence(self):
        student_map = {"Student_001": {"document_github_links": {"repositories": [],
                                                                 "profiles": []}}}
        self.assertEqual(add_document_links(None, None, student_map), (None, None))

    def test_document_links_used_without_index_html(self):
        by_student, counts = add_document_links(None, None, self.STUDENT_MAP)
        self.assertEqual(by_student["Student_001"]["repositories"], [("jc-dev42", "prj1")])
        self.assertEqual(by_student["Student_001"]["source"], LINK_SOURCE_DOCUMENT)
        self.assertEqual(counts["students_with_repository_urls_from_documents"],
                         ["Student_001"])
        self.assertEqual(counts["students_without_recognized_repository_link"],
                         {"Student_002": "no_link_in_index_or_documents"})

    def test_index_html_link_takes_precedence(self):
        index = {"Student_001": {"repositories": [("index-owner", "repo")], "profiles": [],
                                 "reasons": []}}
        by_student, counts = add_document_links(index, {"index_entries": 1}, self.STUDENT_MAP)
        self.assertEqual(by_student["Student_001"]["repositories"], [("index-owner", "repo")])
        self.assertEqual(counts["students_with_repository_urls_from_documents"], [])
        self.assertEqual(counts["students_without_recognized_repository_link"],
                         {"Student_002": "no_index_entry"})

    def test_owners_from_either_source_are_checked_in_every_file(self):
        manifest = {"submissions": [{"student_id": "Student_001", "files": [
            {"path": "artifact_001_report.pdf", "extracted_text": "see jc-dev42/prj1"}]}]}
        for source in (LINK_SOURCE_DOCUMENT, "brightspace_index_html"):
            repository_map = {"Student_001": {"link_source": source,
                                              "repositories": [{"owner": "jc-dev42"}]}}
            with self.assertRaises(RuntimeError):
                verify_repository_redaction(manifest, repository_map)
        manifest["submissions"][0]["files"][0]["extracted_text"] = "see <repo_owner>/prj1"
        verify_repository_redaction(manifest, repository_map)

    def test_index_html_owner_is_redacted_from_names_and_text(self):
        index_html = """<html><body><table>
<tr><td colspan="2"><b>Smoke, Avery</b></td></tr>
<tr><td><p>My repo: https://github.com/xq-lab7/prj-code</p></td></tr>
</table></body></html>"""
        with tempfile.TemporaryDirectory() as directory:
            fixture = RepositoryProject(directory, index_html=index_html)
            avery = next((fixture.project / "submissions").glob("*asmoke*"))
            (avery / "xq-lab7_analysis.csv").write_text("x\n1\n", encoding="utf-8")
            (avery / "notes.md").write_text(
                "Pushed to XQ_LAB7/prj-code; see xq lab7 and xqlab7.\n", encoding="utf-8")
            make_pdf(avery / "report.pdf", [["Code is at github.com/xq-lab7/prj-code"]])
            with NoNetwork(fixture), patch("sys.stdout", new=io.StringIO()):
                _, map_path, manifest = write_submission_manifest(fixture.project)
            student = manifest["submissions"][0]
            names = [f["path"] for f in student["files"]]
            self.assertIn("artifact_004_owner_analysis.csv", names)
            notes = next(f for f in student["files"] if f["path"].endswith("notes.md"))
            self.assertEqual(notes["content"],
                             "Pushed to <repo_owner>/prj-code; see <repo_owner> and <repo_owner>.\n")
            pdf = next(f for f in student["files"] if f["extension"] == ".pdf")
            self.assertIn("github.com/<repo_owner>/prj-code", pdf["extracted_text"])
            serialized = json.dumps(manifest).casefold()
            for form in ("xq-lab7", "xq_lab7", "xqlab7", "xq lab7"):
                self.assertNotIn(form, serialized)
            anonymized = fixture.project / "grader/anonymized_submissions/Student_001"
            self.assertNotIn("xq", " ".join(p.name for p in anonymized.rglob("*")).casefold())
            identity = json.loads(map_path.read_text(encoding="utf-8"))["Student_001"]
            self.assertEqual(identity["redacted_repository_owners"], ["xq-lab7"])
            # Index links still take precedence over the same link in the PDF.
            self.assertEqual(manifest["repository_evidence"]
                             ["students_with_repository_urls_from_documents"], [])

    def test_filename_checks_use_the_same_owner_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "xq-lab7_analysis.csv").write_text("x\n1\n", encoding="utf-8")
            spec = {"deliverables": [{"deliverable_id": "csv",
                                      "file_patterns": ["*_analysis.csv"]}]}
            checks = original_filename_checks(
                merge_attempt_files([folder]), "abc123", "Jane Chen", spec, ["xq-lab7"])
            self.assertEqual(checks["checks"][0]["artifact_paths"],
                             ["artifact_001_owner_analysis.csv"])

    def test_link_in_notebook_becomes_repository_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RepositoryProject(directory, index_html=None)
            avery = next((fixture.project / "submissions").glob("*asmoke*"))
            (avery / "analysis.ipynb").write_text(json.dumps({
                "cells": [{"cell_type": "markdown",
                           "source": "Repo: https://github.com/avery-gh/AverySmoke-pr02"}]}),
                encoding="utf-8")
            with NoNetwork(fixture), patch("sys.stdout", new=io.StringIO()):
                _, _, manifest = write_submission_manifest(fixture.project)
            evidence = manifest["repository_evidence"]
            self.assertEqual(evidence["students_with_repository_urls_from_documents"],
                             ["Student_001"])
            facts = json.loads(next(
                f["content"] for f in files_of(manifest, "Student_001", "github_repository")
                if "/" not in f["path"]))
            self.assertEqual(facts["repository_link_source"], LINK_SOURCE_DOCUMENT)
            self.assertEqual(facts["fetch_status"], "cloned")
            student = manifest["submissions"][0]
            self.assertTrue(any("not from index.html" in flag for flag in student["review_flags"]))
            notebook = next(f for f in student["files"] if f["extension"] == ".ipynb")
            self.assertIn("<repo_owner>", notebook["content"])
            self.assertNotIn("avery-gh", json.dumps(manifest))
            private = json.loads((fixture.project / "grader/repository_map.json")
                                 .read_text(encoding="utf-8"))
            self.assertEqual(private["Student_001"]["link_source"], LINK_SOURCE_DOCUMENT)
            self.assertEqual(private["Student_001"]["repositories"][0]["owner"], "avery-gh")


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        folder = self.root / "submissions" / FOLDER
        folder.mkdir(parents=True)
        (folder / "analysis.py").write_text("print(1)\n", encoding="utf-8")
        write_submission_manifest(self.root)
        self.grader = self.root / "grader"
        for run in ("run_v001", "run_v002"):
            (self.grader / "grading_runs" / run).mkdir(parents=True)
            (self.grader / "grading_runs" / run / "grading_results.json").write_text(
                "{}", encoding="utf-8")
        (self.grader / "debug_responses").mkdir()
        (self.grader / "debug_responses" / "Student_001_x.json").write_text(
            "{}", encoding="utf-8")

    def test_archiving_all_runs_unblocks_prepare_submissions(self):
        with self.assertRaises(SubmissionsAlreadyGradedError):
            write_submission_manifest(self.root)
        archive_path, moved = archive_grading(self.root, reason="incomplete run")
        self.assertEqual(moved, ["grading_runs/run_v001", "grading_runs/run_v002",
                                 "debug_responses"])
        self.assertTrue((archive_path / "grading_runs/run_v001/grading_results.json").is_file())
        self.assertTrue((archive_path / "debug_responses/Student_001_x.json").is_file())
        record = json.loads((archive_path / "archive_record.json").read_text(encoding="utf-8"))
        self.assertEqual((record["reason"], record["complete"]), ("incomplete run", True))
        self.assertEqual(archive_path.parent, self.grader / "archive")
        write_submission_manifest(self.root)   # No longer refused.

    def test_archiving_one_run_keeps_responses_for_the_rest(self):
        _, moved = archive_grading(self.root, ["run_v001"])
        self.assertEqual(moved, ["grading_runs/run_v001"])
        self.assertTrue((self.grader / "debug_responses" / "Student_001_x.json").is_file())
        with self.assertRaises(SubmissionsAlreadyGradedError):
            write_submission_manifest(self.root)

    def test_refusals_move_nothing(self):
        finalization = self.grader / "finalizations" / "run_v002" / "finalization_v001"
        finalization.mkdir(parents=True)
        for runs, message in ((["run_v002"], "finalized grades"),
                              (None, "finalized grades"),
                              (["run_v009"], "No such grading run")):
            with self.assertRaisesRegex(ArchiveError, message):
                archive_grading(self.root, runs)
        self.assertFalse((self.grader / "archive").exists())
        self.assertEqual(len(list((self.grader / "grading_runs").iterdir())), 2)

    def test_nothing_to_archive(self):
        archive_grading(self.root)
        with self.assertRaisesRegex(ArchiveError, "Nothing to archive"):
            archive_grading(self.root)

    def test_run_numbers_continue_past_archived_runs(self):
        archive_grading(self.root)
        self.assertEqual(next_grading_run_path(self.grader).name, "run_v003")

    def test_cli(self):
        output = io.StringIO()
        with patch("sys.argv", ["ai_grader", "archive-grading", str(self.root),
                                "--run", "run_v001"]), patch("sys.stdout", output):
            main()
        self.assertIn("Set aside (moved, not deleted) to:", output.getvalue())
        self.assertIn("prepare-submissions stays blocked", output.getvalue())
        output = io.StringIO()
        with patch("sys.argv", ["ai_grader", "archive-grading", str(self.root)]), \
             patch("sys.stdout", output):
            main()
        self.assertIn("python -m ai_grader prepare-submissions", output.getvalue())
        output = io.StringIO()
        with patch("sys.argv", ["ai_grader", "archive-grading", str(self.root)]), \
             patch("sys.stdout", output), self.assertRaises(SystemExit):
            main()
        self.assertIn("Nothing to archive", output.getvalue())

    def test_prepare_submissions_refusal_points_to_archive_grading(self):
        output = io.StringIO()
        with patch("sys.argv", ["ai_grader", "prepare-submissions", str(self.root)]), \
             patch("sys.stdout", output), self.assertRaises(SystemExit):
            main()
        self.assertIn("python -m ai_grader archive-grading", output.getvalue())


class NewSpecStartsNewRunTests(unittest.TestCase):
    def test_new_approved_spec_is_not_resumed_from_old_responses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec, _, _ = GradingTests().make_project(root)
            responses = FakeResponses(model_response())
            client = SimpleNamespace(responses=responses)
            with patch("sys.stdout", new=io.StringIO()):
                first, _, _, _ = grade_submissions(root, client)
                again, _, _, _ = grade_submissions(root, client, resume=True)
            self.assertEqual(len(responses.calls), 1)   # Same spec: reused.

            spec["spec_version"] = "0.2"
            spec["tasks"][0]["criteria"][0]["full_credit_condition"] = "Revised condition."
            (root / "grader" / "grading_spec_v002.json").write_text(
                json.dumps(spec), encoding="utf-8")
            output = io.StringIO()
            with patch("sys.stdout", output):
                new_run, _, _, run = grade_submissions(root, client, resume=True)
            self.assertEqual(len(responses.calls), 2)   # New spec: new request.
            self.assertIn("No saved response for Student_001 matches", output.getvalue())
            self.assertEqual([first.name, again.name, new_run.name],
                             ["run_v001", "run_v002", "run_v003"])
            self.assertEqual(run["grading_spec"], "grading_spec_v002.json")
            self.assertEqual(run["batch_execution"],
                             {"api_calls_made": 1, "reused_responses": 0})


if __name__ == "__main__":
    unittest.main()
