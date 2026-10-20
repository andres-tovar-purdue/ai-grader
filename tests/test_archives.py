"""ZIP archives in submissions and repositories are extracted. Synthetic data only."""

import io
import json
import re
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from ai_grader import archives
from ai_grader.archives import extract_archive, safe_member_path
from ai_grader.grading import archive_section
from ai_grader.grading_validation import artifact_review_reasons
from ai_grader.submission_checks import refresh_submission_checks
from ai_grader.submission_processing import write_submission_manifest
import test_grading as grading_fixtures
import test_repo_evidence as repo_fixtures
from test_submission_merging import write_attempt


def make_zip(path, members):
    """members: {name: bytes or text}; a ZipInfo key is written as given."""
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            data = content if isinstance(content, bytes) else content.encode("utf-8")
            archive.writestr(name, data)
    return path


def zip_bytes(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


class ExtractArchiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.out = self.root / "out"

    def test_extracts_files_and_skips_clutter(self):
        path = make_zip(self.root / "a.zip", {
            "project/main.py": "print(1)\n",
            "project/data.csv": "x\n1\n",
            "__MACOSX/project/._main.py": b"\x00",
            "project/.DS_Store": b"\x00",
            "project/__pycache__/main.cpython-313.pyc": b"\x00",
        })
        report = extract_archive(path, self.out)
        self.assertEqual(report["status"], "extracted")
        self.assertEqual(report["files"], [Path("project/data.csv"), Path("project/main.py")])
        self.assertEqual(report["skipped"]["clutter"], 3)
        self.assertEqual((self.out / "project/main.py").read_text(), "print(1)\n")

    def test_unsafe_paths_are_never_written_outside(self):
        for name in ("../evil.py", "/abs.py", "C:/win.py", "a/../../evil.py", "..\\evil.py"):
            self.assertIsNone(safe_member_path(name), name)
        self.assertEqual(safe_member_path("a\\b.py").as_posix(), "a/b.py")
        path = make_zip(self.root / "slip.zip", {"../evil.py": "x", "ok.py": "y"})
        report = extract_archive(path, self.out)
        self.assertEqual(report["status"], "partly_extracted")
        self.assertEqual(report["skipped"]["unsafe_path"], 1)
        self.assertFalse((self.root / "evil.py").exists())
        self.assertEqual(report["files"], [Path("ok.py")])

    def test_links_and_encrypted_members_are_skipped(self):
        path = self.root / "special.zip"
        with zipfile.ZipFile(path, "w") as archive:
            link = zipfile.ZipInfo("link.py")
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, "/etc/passwd")
            archive.writestr("plain.py", "print(1)\n")
        report = extract_archive(path, self.out)
        self.assertEqual(report["skipped"]["link"], 1)
        self.assertEqual(report["files"], [Path("plain.py")])

        encrypted = zipfile.ZipInfo("secret.py")
        encrypted.flag_bits |= 0x1
        with patch.object(zipfile.ZipFile, "infolist", return_value=[encrypted]):
            report = extract_archive(path, self.root / "out2")
        self.assertEqual(report["status"], "encrypted")
        self.assertEqual(report["files"], [])

    def test_size_limits(self):
        path = make_zip(self.root / "big.zip", {"a.txt": "x" * 100, "b.txt": "y" * 100})
        with patch.object(archives, "MAX_MEMBER_BYTES", 50):
            report = extract_archive(path, self.out)
        self.assertEqual((report["status"], report["skipped"]["too_large"]),
                         ("partly_extracted", 2))
        with patch.object(archives, "MAX_ARCHIVE_TOTAL_BYTES", 150):
            report = extract_archive(path, self.root / "out2")
        self.assertEqual(report["files"], [Path("a.txt")])
        self.assertEqual(report["skipped"]["over_total_limit"], 1)
        with patch.object(archives, "MAX_ARCHIVE_MEMBERS", 1):
            report = extract_archive(path, self.root / "out3")
        self.assertEqual(report["skipped"]["over_member_limit"], 1)

    def test_copy_never_writes_more_than_the_limit(self):
        # Guards against a member whose declared size is smaller than its data.
        target = self.root / "copy.txt"
        self.assertEqual(archives.copy_limited(io.BytesIO(b"x" * 100), target, 50), (False, 0))
        self.assertFalse(target.exists())
        self.assertEqual(archives.copy_limited(io.BytesIO(b"x" * 40), target, 50), (True, 40))

    def test_one_bad_member_does_not_stop_the_rest(self):
        path = make_zip(self.root / "mixed.zip", {"bad.py": "1", "good.py": "2"})
        real_open = zipfile.ZipFile.open

        def failing_open(archive, info, *args, **kwargs):
            name = info.filename if isinstance(info, zipfile.ZipInfo) else info
            if name == "bad.py":
                raise zipfile.BadZipFile("Bad CRC-32 for file 'bad.py'")
            return real_open(archive, info, *args, **kwargs)

        with patch.object(zipfile.ZipFile, "open", failing_open):
            report = extract_archive(path, self.out)
        self.assertEqual((report["status"], report["skipped"]["damaged"], report["files"]),
                         ("partly_extracted", 1, [Path("good.py")]))
        self.assertFalse((self.out / "bad.py").exists())

    def test_unreadable_and_nested(self):
        broken = self.root / "broken.zip"
        broken.write_bytes(b"not a zip")
        self.assertEqual(extract_archive(broken, self.out)["status"], "unreadable")
        path = make_zip(self.root / "outer.zip", {"inner.zip": zip_bytes({"x.py": "1"})})
        report = extract_archive(path, self.root / "out2")
        self.assertEqual((report["status"], report["nested_archives"]), ("extracted", 1))


class SubmissionArchiveTests(unittest.TestCase):
    """A student submits analysis.py and files.zip (one duplicate, one new file)."""

    FOLDER = "300-1 - abc123 Jane Chen - Oct 1, 2026 0900 AM"

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        folder = write_attempt(self.root, self.FOLDER, {
            "analysis.py": b"print('ok')\r\n",   # Windows line endings
        })
        make_zip(folder / "chen_files.zip", {
            "chen_files/analysis.py": "print('ok')\n",          # identical
            "chen_files/chenj_extra.py": "# Extra by Jane Chen (abc123)\n",
            "chen_files/inner.zip": zip_bytes({"deep.py": "1"}),
            "__MACOSX/chen_files/._analysis.py": b"\x00",
        })
        write_attempt(self.root, "400-1 - def456 Sam Sample - Oct 2, 2026 1000 AM", {
            "broken.zip": b"not a zip",
        })
        _, map_path, self.manifest = write_submission_manifest(self.root)
        self.student_map = json.loads(map_path.read_text(encoding="utf-8"))
        self.files = {item["path"]: item for item in self.manifest["submissions"][0]["files"]}
        self.anonymized = self.root / "grader" / "anonymized_submissions" / "Student_001"

    def test_archive_files_become_redacted_artifacts(self):
        self.assertEqual(sorted(self.files), [
            "artifact_001_analysis.py",
            "artifact_002_student_files.zip",
            "student_files.zip_contents/student_files/artifact_003_student_extra.py",
            "student_files.zip_contents/student_files/artifact_004_inner.zip",
        ])
        extra = self.files["student_files.zip_contents/student_files/artifact_003_student_extra.py"]
        self.assertEqual(extra["content"], "# Extra by <student_name> (<username>)\n")
        self.assertEqual(extra["extracted_from"], "artifact_002_student_files.zip")
        self.assertEqual((self.anonymized / extra["path"]).read_text(encoding="utf-8"),
                         extra["content"])

    def test_archive_record_reports_duplicates_and_nested_archives(self):
        archive = self.files["artifact_002_student_files.zip"]["archive_extraction"]
        self.assertEqual(archive, {
            "method": "zip_local_v1",
            "status": "extracted",
            "contents_folder": "student_files.zip_contents",
            "files_added": 2,
            "identical_to_artifacts": ["artifact_001_analysis.py"],
            "skipped_members": {},
            "nested_archives_not_extracted": 1,
        })
        submission = self.manifest["submissions"][0]
        reasons = artifact_review_reasons(submission)
        self.assertEqual(len(reasons), 1)
        self.assertIn("1 ZIP file(s) that were not extracted", reasons[0])
        text = archive_section("artifact_002_student_files.zip",
                               self.files["artifact_002_student_files.zip"])
        self.assertIn("EVIDENCE TYPE: zip_archive", text)
        self.assertIn("identical to separately submitted artifacts (not repeated): "
                      "artifact_001_analysis.py", text)
        self.assertIn("Do not treat it as missing", text)

    def test_cli_reports_archives_without_names(self):
        from ai_grader.cli import main
        output = io.StringIO()
        with patch("sys.argv", ["ai_grader", "prepare-submissions", str(self.root)]), \
             patch("sys.stdout", output):
            main()
        text = output.getvalue()
        self.assertIn("ZIP archives extracted (originals unchanged):", text)
        self.assertIn("Student_001: ZIP in submission: extracted, 2 file(s) added, "
                      "1 identical to other files (not repeated) - needs review", text)
        self.assertIn("Student_002: ZIP in submission: unreadable, 0 file(s) added - needs review",
                      text)
        for name in ("Chen", "Jane", "abc123", "Sam"):
            self.assertNotIn(name, text)

    def test_duplicates_ignore_line_endings_but_not_content(self):
        from ai_grader.submission_processing import content_key
        crlf = self.root / "crlf.py"
        crlf.write_bytes(b"print('ok')\r\n")
        lf = self.root / "lf.py"
        lf.write_bytes(b"print('ok')\n")
        changed = self.root / "changed.py"
        changed.write_bytes(b"print('no')\n")
        self.assertEqual(content_key(crlf), content_key(lf))
        self.assertNotEqual(content_key(lf), content_key(changed))
        # Binary files compare by bytes.
        a, b = self.root / "a.png", self.root / "b.png"
        a.write_bytes(b"\x89PNG\r\n")
        b.write_bytes(b"\x89PNG\n")
        self.assertNotEqual(content_key(a), content_key(b))

    def test_unreadable_archive_needs_review(self):
        other = self.manifest["submissions"][1]
        record = other["files"][0]
        self.assertEqual(record["archive_extraction"]["status"], "unreadable")
        self.assertIn("could not be fully extracted (unreadable)",
                      artifact_review_reasons(other)[0])

    def test_private_map_and_originals(self):
        files = {item["original_path"]: item for item in self.student_map["Student_001"]["files"]}
        self.assertEqual(files["chen_files.zip_contents/chen_files/chenj_extra.py"]["extracted_from"],
                         "chen_files.zip")
        self.assertNotIn("extracted_from", files["analysis.py"])
        # Nothing is extracted into the original submission folder.
        original = self.root / "submissions" / self.FOLDER
        self.assertEqual(sorted(p.name for p in original.iterdir()),
                         ["analysis.py", "chen_files.zip"])

    def test_manifest_has_no_identity(self):
        serialized = json.dumps(self.manifest).casefold()
        for text in ("chen", "jane", "abc123", "chenj", "sam", "def456"):
            self.assertIsNone(
                re.search(r"(?<![^\W\d_])" + re.escape(text) + r"(?![^\W\d_])", serialized),
                text)

    def test_refresh_checks_accept_extracted_files(self):
        spec, _, _ = grading_fixtures.GradingTests().make_project(self.root / "fixture")
        (self.root / "grader" / "grading_spec_v001.json").write_text(
            json.dumps(spec), encoding="utf-8")
        _, manifest = refresh_submission_checks(self.root)
        self.assertIn("deliverable_checks", manifest["submissions"][0])

    def test_refresh_detects_changed_extracted_file(self):
        spec, _, _ = grading_fixtures.GradingTests().make_project(self.root / "fixture")
        (self.root / "grader" / "grading_spec_v001.json").write_text(
            json.dumps(spec), encoding="utf-8")
        nested = self.anonymized / "student_files.zip_contents/student_files/artifact_004_inner.zip"
        self.assertTrue(nested.is_file())
        nested.write_bytes(b"tampered")
        with self.assertRaises(RuntimeError):
            refresh_submission_checks(self.root)


class RepositoryArchiveTests(unittest.TestCase):
    def test_zip_in_repository_is_extracted(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = repo_fixtures.RepositoryProject(directory)
            repo = fixture.repos[("avery-gh", "AverySmoke-pr02")]
            make_zip(repo / "smoke_project.zip", {
                "asmoke_analysis.py": "import pandas as pd\nprint('hello')\n",  # tracked
                "smokea_extra.py": "# by Avery Smoke\n",
            })
            repo_fixtures.git(repo, "add", "--all")
            repo_fixtures.git(repo, "commit", "--quiet", "-m", "Add zip",
                              when=repo_fixtures.local(2026, 10, 18, 21, 0))
            with repo_fixtures.NoNetwork(fixture):
                _, _, manifest = write_submission_manifest(fixture.project)

        files = {item["path"]: item
                 for item in repo_fixtures.files_of(manifest, "Student_001", "github_repository")}
        archive_path = next(path for path in files if path.endswith("_student_project.zip"))
        archive = files[archive_path]["archive_extraction"]
        self.assertEqual(archive["status"], "extracted")
        self.assertEqual(archive["files_added"], 1)
        self.assertEqual(len(archive["identical_to_artifacts"]), 1)
        self.assertTrue(archive["identical_to_artifacts"][0].endswith("_student_analysis.py"))
        extra = next(item for path, item in files.items()
                     if path.startswith("github_repo/student_project.zip_contents/"))
        self.assertTrue(extra["path"].endswith("_student_extra.py"))
        self.assertEqual(extra["content"], "# by <student_name>\n")
        self.assertEqual(extra["extracted_from"], archive_path)


if __name__ == "__main__":
    unittest.main()
