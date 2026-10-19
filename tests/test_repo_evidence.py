import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_grader import repo_evidence
from ai_grader.repo_evidence import (
    find_github_repositories, git_environment, map_repositories, parse_index,
)
from ai_grader.submission_checks import refresh_submission_checks
from ai_grader.submission_processing import write_submission_manifest

# Synthetic students; no real data. Student IDs follow username order.
STUDENTS = [
    ("100001-2000001", "asmoke", "Avery Smoke"),   # Student_001
    ("100002-2000002", "bfake", "Blake Fake"),     # Student_002
    ("100003-2000003", "cmock", "Casey Mock"),     # Student_003
]
IDENTITY_STRINGS = ["asmoke", "Avery", "Smoke", "bfake", "Blake", "cmock",
                    "Casey", "avery-gh", "averygh"]
EXAMPLE_SPEC = (Path(__file__).resolve().parents[1]
                / "examples/sample_project/grader/grading_spec_v001.json")


def git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.com",
         "-c", "init.defaultBranch=main", "-c", "core.autocrlf=false", *args],
        cwd=cwd, check=True, capture_output=True,
    )


def make_repo(path, commits):
    """commits: list of (message, {relative path: text})."""
    path.mkdir(parents=True)
    git(path, "init", "--quiet")
    for message, files in commits:
        for relative, text in files.items():
            target = path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))
        git(path, "add", "--all")
        git(path, "commit", "--quiet", "-m", message)
    return path


INDEX_HTML = """<html><body bgcolor="#ffffff"><table><tr><td bgcolor="#eeeeee">
<table>
<tr><td colspan="2"><font><b>Smoke, Avery</b></font></td></tr>
<tr><td valign="top">Submitted Oct 18<p style="x"><b>Comments</b><br>
  <p>My project: <a href="https://github.com/avery-gh/AverySmoke-pr02.git">link</a></p>
</td><td valign="top"><b>Files</b><br>analysis.py</td></tr>
<tr><td colspan="2"><font><b>Blake Fake (bfake)</b></font></td></tr>
<tr><td valign="top"><p style="x"><b>Comments</b><br>
  <p>Repo is github.com/bfake/missing-repo.</p>
</td><td valign="top"><b>Files</b><br>main.py</td></tr>
<tr><td colspan="2"><font><b>Casey Mock</b></font></td></tr>
<tr><td valign="top"><p style="x"><b>Comments</b><br><p>No link, sorry.</p></td>
<td valign="top"><b>Files</b><br>a.py</td></tr>
<tr><td colspan="2"><font><b>Jordan Unknown</b></font></td></tr>
<tr><td valign="top"><p><a href="https://github.com/someone/other">https://github.com/someone/other</a></p></td></tr>
</table></td></tr></table></body></html>
"""


class RepositoryProject:
    """A synthetic project with Brightspace folders, index.html and local repos."""

    def __init__(self, root, index_html=INDEX_HTML, spec=True):
        self.root = Path(root)
        self.project = self.root / "project"
        submissions = self.project / "submissions"
        for prefix, username, name in STUDENTS:
            folder = submissions / f"{prefix} - {username} {name} - Oct 18, 2026 1053 PM"
            folder.mkdir(parents=True)
            (folder / "analysis.py").write_text("print('synthetic')\n", encoding="utf-8")
        if index_html is not None:
            (submissions / "index.html").write_text(index_html, encoding="utf-8")
        (self.project / "grader").mkdir()
        if spec:
            shutil.copy(EXAMPLE_SPEC, self.project / "grader" / "grading_spec_v001.json")

        self.repos = {}
        self.repos[("avery-gh", "AverySmoke-pr02")] = make_repo(
            self.root / "remotes" / "avery", [
                ("Initial commit by Avery Smoke", {
                    "README.md": "Project by Avery Smoke (asmoke), GitHub avery-gh.\n",
                    "asmoke_analysis.py": "import pandas as pd\nprint('hello')\n",
                }),
                ("Add environment by mistake", {
                    ".venv/pyvenv.cfg": "home = /usr/bin\n",
                    ".venv/Lib/site-packages/pkg.py": "x = 1\n",
                    "data/speed_data.csv": "time_s,speed_kmh\n0,10\n",
                }),
                ("Fix plot for avery-gh", {"plot.py": "print('plot')\n"}),
            ])

    MISSING = "missing-remote"

    def clone_url_for(self, owner, repo):
        local = self.repos.get((owner, repo))
        return str(local) if local else f"{self.MISSING}:{owner}/{repo}"


class NoNetwork:
    """Route clones to local repos and fail on any https URL reaching git."""

    def __init__(self, fixture):
        self.fixture = fixture
        self.git_calls = []

    def __enter__(self):
        real_run = subprocess.run

        def guarded_run(args, *rest, **kwargs):
            if args and args[0] == "git":
                self.git_calls.append((list(args), kwargs.get("env")))
                if any(str(arg).startswith(("http://", "https://")) for arg in args):
                    raise AssertionError(f"network URL passed to git: {args}")
                if any(str(arg).startswith(RepositoryProject.MISSING) for arg in args):
                    # What GitHub returns for a missing or private repository.
                    return subprocess.CompletedProcess(args, 128, b"", (
                        b"remote: Repository not found.\n"
                        b"fatal: repository 'https://github.com/x/y.git/' not found\n"))
            return real_run(args, *rest, **kwargs)

        self.patches = [
            patch.object(repo_evidence, "clone_url_for", self.fixture.clone_url_for),
            patch.object(repo_evidence.subprocess, "run", guarded_run),
        ]
        for item in self.patches:
            item.start()
        return self

    def __exit__(self, *exc):
        for item in reversed(self.patches):
            item.stop()


def files_of(manifest, student_id, source=None):
    submission = next(s for s in manifest["submissions"] if s["student_id"] == student_id)
    return [f for f in submission["files"] if source is None or f.get("source") == source]


class UrlParsingTests(unittest.TestCase):
    def test_finds_repositories_in_common_url_forms(self):
        text = ("https://github.com/Owner-1/repo.git and www.github.com/owner2/proj/tree/main "
                "and github.com/owner3/name.with.dots. and "
                "https://github.com/Owner-1/REPO (duplicate)")
        self.assertEqual(find_github_repositories(text), [
            ("Owner-1", "repo"), ("owner2", "proj"), ("owner3", "name.with.dots"),
        ])

    def test_ignores_profiles_site_pages_and_other_hosts(self):
        text = ("https://github.com/someone https://github.com/settings/profile "
                "https://gitlab.com/a/b https://github.com/orgs/x")
        self.assertEqual(find_github_repositories(text), [])

    def test_index_blocks_follow_brightspace_header_rows(self):
        blocks = parse_index(INDEX_HTML)
        self.assertEqual([b["header"] for b in blocks],
                         ["Smoke, Avery", "Blake Fake (bfake)", "Casey Mock", "Jordan Unknown"])
        self.assertEqual([len(b["repositories"]) for b in blocks], [1, 1, 0, 1])

    def test_maps_blocks_to_students_and_counts_unmatched(self):
        student_map = {f"Student_{i:03d}": {"username": u, "full_name": n}
                       for i, (_, u, n) in enumerate(STUDENTS, 1)}
        ambiguous = INDEX_HTML.replace(
            "Jordan Unknown", "Avery Smoke and Blake Fake")
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "index.html"
            for html, expected_unmatched in ((INDEX_HTML, 1), (ambiguous, 1)):
                index.write_text(html, encoding="utf-8")
                by_student, counts = map_repositories(index, student_map)
                self.assertEqual(by_student, {
                    "Student_001": [("avery-gh", "AverySmoke-pr02")],
                    "Student_002": [("bfake", "missing-repo")],
                })
                self.assertEqual(counts["index_blocks_not_matched_to_a_student"],
                                 expected_unmatched)
                self.assertEqual(counts["students_without_repository_urls"], 1)


class GitIsolationTests(unittest.TestCase):
    def test_git_runs_without_credentials_or_prompts(self):
        env = git_environment()
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], repo_evidence.os.devnull)
        self.assertEqual(env["GCM_INTERACTIVE"], "never")
        with patch.object(repo_evidence.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, b"", b"")
            repo_evidence.clone_repository("owner", "repo", Path("dest"))
        args = run.call_args.args[0]
        self.assertEqual(args[:3], ["git", "-c", "credential.helper="])
        self.assertIn("https://github.com/owner/repo.git", args)
        self.assertIn("core.symlinks=false", args)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_clone_failures_are_classified_without_keeping_git_output(self):
        cases = [
            (b"remote: Repository not found.", ("not_public_or_not_found", False)),
            (b"fatal: could not read Username for 'https://github.com'",
             ("not_public_or_not_found", False)),
            (b"fatal: unable to access: Could not resolve host: github.com",
             ("network_error", None)),
            (b"fatal: something else", ("clone_failed", None)),
        ]
        for stderr, expected in cases:
            with patch.object(repo_evidence.subprocess, "run",
                              return_value=subprocess.CompletedProcess([], 128, b"", stderr)):
                self.assertEqual(
                    repo_evidence.clone_repository("o", "r", Path("d")), expected)
        with patch.object(repo_evidence.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(repo_evidence.clone_repository("o", "r", Path("d")),
                             ("git_not_available", None))


class PrepareSubmissionsTests(unittest.TestCase):
    def prepare(self, directory, **kwargs):
        fixture = RepositoryProject(directory, **kwargs)
        with NoNetwork(fixture) as guard:
            _, _, manifest = write_submission_manifest(fixture.project)
        return fixture, manifest, guard

    def test_repository_files_and_facts_become_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, manifest, _ = self.prepare(directory)
            root = fixture.project / "grader" / "anonymized_submissions" / "Student_001"
            repo_files = files_of(manifest, "Student_001", "github_repository")
            paths = [f["path"] for f in repo_files]

            # Original file first, then repository files, then the facts file.
            self.assertEqual(files_of(manifest, "Student_001")[0]["path"],
                             "artifact_001_analysis.py")
            self.assertIn("github_repo/artifact_002_README.md", paths)
            self.assertIn("github_repo/artifact_003_student_analysis.py", paths)
            self.assertTrue(paths[-1].endswith("_github_repo_facts.json"))
            self.assertFalse(any(".venv" in p for p in paths))
            for record in repo_files:
                self.assertTrue(Path(record["path"]).name.startswith("artifact_"))
                self.assertTrue((root / record["path"]).is_file())

            readme = (root / "github_repo/artifact_002_README.md").read_text(encoding="utf-8")
            self.assertEqual(
                readme, "Project by <student_name> (<username>), GitHub <repo_owner>.\n")

            facts = json.loads((root / paths[-1]).read_text(encoding="utf-8"))
            self.assertEqual(facts["evidence_type"], "github_repository_facts")
            self.assertTrue(facts["publicly_reachable"])
            self.assertEqual(facts["fetch_status"], "cloned")
            self.assertEqual(facts["repository_name"], "<student_name>-pr02")
            self.assertEqual(facts["repository_owner"], "<repo_owner>")
            self.assertEqual(facts["tracked_file_count"], 6)
            self.assertEqual(sorted(facts["tracked_files"]), [
                "README.md", "data/speed_data.csv", "plot.py", "student_analysis.py"])
            self.assertEqual(facts["commit_count"], 3)
            self.assertEqual([c["message"] for c in facts["commits"]], [
                "Fix plot for <repo_owner>", "Add environment by mistake",
                "Initial commit by <student_name>"])
            self.assertTrue(all(c["committed_at"] for c in facts["commits"]))
            self.assertTrue(facts["environment_folders_tracked"])
            self.assertEqual(facts["environment_folders"], [".venv"])
            self.assertEqual(facts["environment_folder_file_count"], 2)
            self.assertEqual(facts["files_not_copied"]["environment_folder_files"], 2)

            serialized = json.dumps(repo_files) + json.dumps(facts)
            for text in IDENTITY_STRINGS:
                self.assertNotIn(text.lower(), serialized.lower())

    def test_unreachable_repository_is_recorded_as_a_fact(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, manifest, _ = self.prepare(directory)
            repo_files = files_of(manifest, "Student_002", "github_repository")
            self.assertEqual(len(repo_files), 1)
            facts = json.loads(repo_files[0]["content"])
            self.assertFalse(facts["publicly_reachable"])
            self.assertEqual(facts["fetch_status"], "not_public_or_not_found")
            self.assertNotIn("commit_count", facts)

    def test_summary_counts_and_private_repository_map(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, manifest, guard = self.prepare(directory)
            self.assertEqual(manifest["repository_evidence"], {
                "index_blocks_with_repository_urls": 3,
                "index_blocks_not_matched_to_a_student": 1,
                "students_with_repository_urls": 2,
                "students_without_repository_urls": 1,
                "repository_urls": 2,
                "fetch_status_counts": {"cloned": 1, "not_public_or_not_found": 1},
            })
            self.assertEqual(files_of(manifest, "Student_003", "github_repository"), [])
            private = json.loads(
                (fixture.project / "grader/repository_map.json").read_text(encoding="utf-8"))
            self.assertEqual(private["Student_001"][0]["owner"], "avery-gh")
            # The unmatched block's repository was never cloned.
            self.assertFalse(any("other" in " ".join(map(str, args))
                                 for args, _ in guard.git_calls))
            self.assertEqual(list((fixture.project / "grader").glob(".repo_fetch_*")), [])

    def test_inactive_without_index_or_repository_links(self):
        for index_html in (None, "<html><body><p>No links here.</p></body></html>"):
            with tempfile.TemporaryDirectory() as directory:
                fixture, manifest, guard = self.prepare(directory, index_html=index_html)
                self.assertNotIn("repository_evidence", manifest)
                self.assertEqual(guard.git_calls, [])
                self.assertFalse((fixture.project / "grader/repository_map.json").exists())
                for submission in manifest["submissions"]:
                    self.assertEqual([f["path"] for f in submission["files"]],
                                     ["artifact_001_analysis.py"])

    def test_grading_receives_repository_evidence(self):
        from ai_grader.grading import build_student_input, load_grading_inputs
        with tempfile.TemporaryDirectory() as directory:
            fixture, _, _ = self.prepare(directory)
            _, spec, _, manifest, anonymized = load_grading_inputs(fixture.project)
            submission = next(s for s in manifest["submissions"]
                              if s["student_id"] == "Student_001")
            request = build_student_input(spec, submission,
                                          anonymized / "Student_001", "Instructions.")
            prompt = request[0]["content"][0]["text"]
            self.assertIn("EVIDENCE TYPE: github_repository_facts", prompt)
            self.assertIn("EVIDENCE TYPE: github_repository_file", prompt)
            self.assertIn('"commit_count": 3', prompt)

    def test_refresh_checks_accept_repository_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, _, _ = self.prepare(directory)
            refresh_submission_checks(fixture.project)

    def test_refresh_rejects_unredacted_owner_in_repository_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, _, _ = self.prepare(directory)
            manifest_path = fixture.project / "grader/submission_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            record = files_of(manifest, "Student_001", "github_repository")[0]
            record["content"] += "contact avery-gh\n"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(repo_evidence.RepositoryEvidenceError):
                refresh_submission_checks(fixture.project)

    def test_owner_redaction_keeps_longer_words(self):
        identity = {"username": "zz_user", "full_name": "Zed Zulu"}
        self.assertEqual(repo_evidence.redact("data dataset data_set", identity, "data"),
                         "<repo_owner> dataset <repo_owner>_set")


if __name__ == "__main__":
    unittest.main()
