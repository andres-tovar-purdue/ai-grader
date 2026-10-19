import codecs
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from ai_grader import repo_evidence
from ai_grader.repo_evidence import (
    find_github_links, find_github_repositories, git_environment,
    map_repositories, matches_pattern, parse_index, submission_cutoff,
)
from ai_grader.submission_checks import refresh_submission_checks
from ai_grader.submission_processing import write_submission_manifest

# Synthetic students; no real data. Student IDs follow username order.
STUDENTS = [
    ("100001-2000001", "asmoke", "Avery Smoke"),   # Student_001
    ("100002-2000002", "bfake", "Blake Fake"),     # Student_002
    ("100003-2000003", "cmock", "Casey Mock"),     # Student_003
]
SUBMITTED = "Oct 18, 2026 1053 PM"   # 22:53 local time
IDENTITY_STRINGS = ["asmoke", "Avery", "Smoke", "bfake", "Blake", "cmock",
                    "Casey", "avery-gh", "averygh", "missing-repo", "github.com"]
EXAMPLE_SPEC = (Path(__file__).resolve().parents[1]
                / "examples/sample_project/grader/grading_spec_v001.json")


def local(*parts):
    """An aware local time, as git and Brightspace times are compared locally."""
    return datetime(*parts).astimezone().isoformat()


def git(cwd, *args, when=None):
    env = dict(os.environ)
    if when:
        env.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    subprocess.run(
        ["git", "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.com",
         "-c", "init.defaultBranch=main", "-c", "core.autocrlf=false", *args],
        cwd=cwd, check=True, capture_output=True, env=env,
    )


def make_repo(path, commits):
    """commits: list of (message, {relative path: bytes or text}, local time)."""
    path.mkdir(parents=True)
    git(path, "init", "--quiet")
    for message, files, when in commits:
        for relative, content in files.items():
            target = path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content if isinstance(content, bytes) else content.encode())
        git(path, "add", "--all")
        git(path, "commit", "--quiet", "--allow-empty", "-m", message, when=when)
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
<tr><td valign="top"><p style="x"><b>Comments</b><br>
  <p>See <a href="https://github.com/cmock">my GitHub</a></p></td>
<td valign="top"><b>Files</b><br>a.py</td></tr>
<tr><td colspan="2"><font><b>Jordan Unknown</b></font></td></tr>
<tr><td valign="top"><p><a href="https://github.com/someone/other">https://github.com/someone/other</a></p></td></tr>
</table></td></tr></table></body></html>
"""


class RepositoryProject:
    """A synthetic project with Brightspace folders, index.html and local repos."""

    MISSING = "missing-remote"

    def __init__(self, root, index_html=INDEX_HTML, spec=True, students=STUDENTS,
                 submitted=SUBMITTED):
        self.root = Path(root)
        self.project = self.root / "projects" / "demo_course_pr02"
        submissions = self.project / "submissions"
        for prefix, username, name in students:
            folder = submissions / f"{prefix} - {username} {name} - {submitted}"
            folder.mkdir(parents=True)
            (folder / "analysis.py").write_text("print('synthetic')\n", encoding="utf-8")
        if index_html is not None:
            data = index_html if isinstance(index_html, bytes) else index_html.encode("utf-8")
            (submissions / "index.html").write_bytes(data)
        (self.project / "grader").mkdir()
        if spec:
            shutil.copy(EXAMPLE_SPEC, self.project / "grader" / "grading_spec_v001.json")

        self.public_repos = {
            "bfake": ["missing_repo", "MissingRepo", "missing-repo-v2", "dotfiles"],
            "cmock": ["Demo-Course-PR02", "demo_pr02", "dotfiles", "course"],
        }
        self.repos = {}
        self.repos[("avery-gh", "AverySmoke-pr02")] = make_repo(
            self.root / "remotes" / "avery", [
                ("Initial commit by Avery Smoke", {
                    "README.md": "Project by Avery Smoke (asmoke), GitHub avery-gh.\n",
                    "asmoke_analysis.py": "import pandas as pd\nprint('hello')\n",
                }, local(2026, 10, 17, 10, 0)),
                ("Add environment by mistake", {
                    ".venv/pyvenv.cfg": "home = /usr/bin\n",
                    ".venv/Lib/site-packages/pkg.py": "x = 1\n",
                    "data/speed_data.csv": "time_s,speed_kmh\n0,10\n",
                }, local(2026, 10, 18, 20, 0)),
                ("Fix plot for avery-gh after the deadline",
                 {"plot.py": "print('plot')\n"}, local(2026, 10, 19, 9, 0)),
            ])

    def clone_url_for(self, owner, repo):
        local_repo = self.repos.get((owner, repo))
        return str(local_repo) if local_repo else f"{self.MISSING}:{owner}/{repo}"


class NoNetwork:
    """Route clones to local repos, fake the public-repo lookup, block the network."""

    def __init__(self, fixture):
        self.fixture = fixture
        self.git_calls = []
        self.lookups = []

    def __enter__(self):
        real_run = subprocess.run

        def guarded_run(args, *rest, **kwargs):
            if args and args[0] == "git":
                self.git_calls.append(list(args))
                if any(str(arg).startswith(("http://", "https://")) for arg in args):
                    raise AssertionError(f"network URL passed to git: {args}")
                if any(str(arg).startswith(RepositoryProject.MISSING) for arg in args):
                    # What GitHub returns for a missing or private repository.
                    return subprocess.CompletedProcess(args, 128, b"", (
                        b"remote: Repository not found.\n"
                        b"fatal: repository 'https://github.com/x/y.git/' not found\n"))
            return real_run(args, *rest, **kwargs)

        def lookup(owner):
            self.lookups.append(owner)
            return self.fixture.public_repos.get(owner, [])

        def no_network(*args, **kwargs):
            raise AssertionError("network access attempted")

        self.patches = [
            patch.object(repo_evidence, "clone_url_for", self.fixture.clone_url_for),
            patch.object(repo_evidence.subprocess, "run", guarded_run),
            patch.object(repo_evidence, "list_public_repositories", lookup),
            patch("urllib.request.urlopen", no_network),
        ]
        for item in self.patches:
            item.start()
        return self

    def __exit__(self, *exc):
        for item in reversed(self.patches):
            item.stop()


def prepare(directory, **kwargs):
    fixture = RepositoryProject(directory, **kwargs)
    with NoNetwork(fixture) as guard:
        _, _, manifest = write_submission_manifest(fixture.project)
    return fixture, manifest, guard


def files_of(manifest, student_id, source=None):
    submission = next(s for s in manifest["submissions"] if s["student_id"] == student_id)
    return [f for f in submission["files"] if source is None or f.get("source") == source]


def facts_of(manifest, student_id):
    facts_files = [f for f in files_of(manifest, student_id, "github_repository")
                   if "/" not in f["path"]]
    return [json.loads(f["content"]) for f in facts_files]


def student_map(students=STUDENTS):
    return {f"Student_{i:03d}": {"username": u, "full_name": n}
            for i, (_, u, n) in enumerate(students, 1)}


class LinkParsingTests(unittest.TestCase):
    def test_finds_repositories_in_common_url_forms(self):
        text = ("https://github.com/Owner-1/repo.git and www.github.com/owner2/proj/tree/main "
                "and github.com/owner3/name.with.dots. and git@github.com:owner4/ssh-repo.git "
                "and http://GitHub.com/owner5/plain/ and https://github.com/Owner-1/REPO")
        self.assertEqual(find_github_repositories(text), [
            ("Owner-1", "repo"), ("owner2", "proj"), ("owner3", "name.with.dots"),
            ("owner5", "plain"), ("owner4", "ssh-repo"),
        ])

    def test_bare_link_without_scheme_in_text(self):
        self.assertEqual(find_github_repositories("repo: github.com/someone/project1,"),
                         [("someone", "project1")])

    def test_profile_links_and_site_pages(self):
        repositories, profiles = find_github_links(
            "https://github.com/someone and github.com/other/ "
            "https://github.com/settings/profile https://gitlab.com/a/b")
        self.assertEqual(repositories, [])
        self.assertEqual(profiles, ["someone", "other"])

    def test_profile_of_an_owner_with_a_linked_repo_is_not_reported(self):
        repositories, profiles = find_github_links(
            "github.com/someone and github.com/someone/project1")
        self.assertEqual(repositories, [("someone", "project1")])
        self.assertEqual(profiles, [])

    def test_index_entries_follow_brightspace_header_rows(self):
        entries = parse_index(INDEX_HTML)
        self.assertEqual([e["header"] for e in entries],
                         ["Smoke, Avery", "Blake Fake (bfake)", "Casey Mock", "Jordan Unknown"])
        self.assertEqual([len(e["repositories"]) for e in entries], [1, 1, 0, 1])
        self.assertEqual(entries[2]["reason"], "github_profile_link_only")


class NoLinkReasonTests(unittest.TestCase):
    STUDENTS = [("1-1", "aone", "Ann One"), ("2-2", "btwo", "Ben Two"),
                ("3-3", "cthree", "Cid Three"), ("4-4", "dfour", "Dee Four"),
                ("5-5", "efive", "Eve Five")]

    def test_each_student_without_a_repository_gets_a_reason(self):
        html = """<table>
<tr><td colspan="2">Ann One</td></tr><tr><td><a href="https://github.com/aone/p">x</a></td></tr>
<tr><td colspan="2">Ben Two</td></tr><tr><td>Code is at https://gitlab.com/btwo/p</td></tr>
<tr><td colspan="2">Cid Three</td></tr><tr><td>I pushed it to github.com, see there</td></tr>
<tr><td colspan="2">Dee Four</td></tr><tr><td>Submitted files only.</td></tr>
</table>"""
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "index.html"
            index.write_text(html, encoding="utf-8")
            _, counts = map_repositories(index, student_map(self.STUDENTS))
        self.assertEqual(counts["index_entries"], 4)
        self.assertEqual(counts["index_entries_with_github_links"], 1)
        self.assertEqual(counts["students_without_recognized_repository_link"], {
            "Student_002": "non_github_link_only",
            "Student_003": "github_link_not_recognized",
            "Student_004": "no_link_in_submission_text",
            "Student_005": "no_index_entry",
        })

    def test_unmatched_and_ambiguous_entries_are_counted(self):
        ambiguous = INDEX_HTML.replace("Jordan Unknown", "Avery Smoke and Blake Fake")
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "index.html"
            for html in (INDEX_HTML, ambiguous):
                index.write_text(html, encoding="utf-8")
                by_student, counts = map_repositories(index, student_map())
                self.assertEqual(by_student["Student_001"]["repositories"],
                                 [("avery-gh", "AverySmoke-pr02")])
                self.assertEqual(by_student["Student_003"]["profiles"], ["cmock"])
                self.assertEqual(counts["index_entries_with_github_links_not_matched"], 1)

    def test_utf16_index_html_is_parsed(self):
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "index.html"
            index.write_bytes(codecs.BOM_UTF16_LE + INDEX_HTML.encode("utf-16-le"))
            by_student, _ = map_repositories(index, student_map())
        self.assertEqual(by_student["Student_002"]["repositories"],
                         [("bfake", "missing-repo")])


class PatternTests(unittest.TestCase):
    def test_separator_variants_and_parts_in_order(self):
        for name in ("mspe_49600_pr02", "MSPE-49600-PR02", "mspe49600pr02",
                     "MSPE49600_PR02", "fa26_pr02"):
            self.assertTrue(matches_pattern(name, "mspe_49600_fa26_pr02"), name)
        for name in ("pr02", "mspe-notes", "pr02_mspe", "dotfiles", ""):
            self.assertFalse(matches_pattern(name, "mspe_49600_fa26_pr02"), name)
        self.assertTrue(matches_pattern("missing_repo", "missing-repo"))


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

    def test_public_repository_lookup_is_anonymous(self):
        requests = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(request, timeout):
            requests.append(request)
            return Response(json.dumps([{"name": "one"}, {"name": "two"}]).encode())

        with patch.dict(os.environ, {"GITHUB_TOKEN": "secret-token", "GH_TOKEN": "secret"}), \
             patch("urllib.request.urlopen", urlopen):
            self.assertEqual(repo_evidence.list_public_repositories("someone"), ["one", "two"])
        request = requests[0]
        self.assertEqual(request.full_url,
                         "https://api.github.com/users/someone/repos?per_page=100&type=owner")
        self.assertNotIn("Authorization", {k.title() for k in request.header_items()[0]})
        self.assertFalse(any("token" in str(v).lower() for _, v in request.header_items()))

        with patch("urllib.request.urlopen",
                   side_effect=repo_evidence.urllib.error.URLError("offline")):
            self.assertIsNone(repo_evidence.list_public_repositories("someone"))


class SubmissionTimeTests(unittest.TestCase):
    def test_cutoff_is_end_of_the_selected_attempts_minute(self):
        identity = {"selected_source_folder": "b", "attempts": [
            {"source_folder": "a", "submitted_at": "2026-10-17T09:00:00"},
            {"source_folder": "b", "submitted_at": "2026-10-18T22:53:00"},
        ]}
        cutoff = submission_cutoff(identity)
        self.assertEqual(cutoff.isoformat(), local(2026, 10, 18, 22, 53, 59))
        identity["attempts"][1]["submitted_at"] = None
        self.assertIsNone(submission_cutoff(identity))


class PrepareSubmissionsTests(unittest.TestCase):
    def test_graded_files_are_the_last_commit_at_or_before_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, manifest, _ = prepare(directory)
            root = fixture.project / "grader" / "anonymized_submissions" / "Student_001"
            repo_files = files_of(manifest, "Student_001", "github_repository")
            paths = [f["path"] for f in repo_files]

            self.assertEqual(files_of(manifest, "Student_001")[0]["path"],
                             "artifact_001_analysis.py")
            self.assertIn("github_repo/artifact_002_README.md", paths)
            self.assertIn("github_repo/artifact_003_student_analysis.py", paths)
            self.assertFalse(any("plot.py" in p for p in paths))   # Committed after.
            self.assertFalse(any(".venv" in p for p in paths))
            self.assertTrue(paths[-1].endswith("_github_repo_facts.json"))
            for record in repo_files:
                self.assertTrue(Path(record["path"]).name.startswith("artifact_"))
                self.assertTrue((root / record["path"]).is_file())

            readme = (root / "github_repo/artifact_002_README.md").read_text(encoding="utf-8")
            self.assertEqual(
                readme, "Project by <student_name> (<username>), GitHub <repo_owner>.\n")

            [facts] = facts_of(manifest, "Student_001")
            self.assertEqual(facts["fetch_status"], "cloned")
            self.assertTrue(facts["publicly_reachable"])
            self.assertEqual(facts["repository_name"], "<student_name>-pr02")
            self.assertEqual(facts["submission_time"], local(2026, 10, 18, 22, 53, 59))
            self.assertEqual(facts["evaluated_state"], "last commit at or before submission")
            self.assertEqual(facts["evaluated_commit"]["message"], "Add environment by mistake")
            self.assertEqual(facts["commit_count"], 3)
            self.assertEqual(facts["commits_at_or_before_submission_count"], 2)
            self.assertEqual([c["message"] for c in facts["commits_after_submission"]],
                             ["Fix plot for <repo_owner> after the deadline"])
            self.assertEqual(sorted(facts["tracked_files"]), [
                "README.md", "data/speed_data.csv", "student_analysis.py"])
            self.assertTrue(facts["environment_folders_tracked"])
            self.assertEqual(facts["environment_folders"], [".venv"])
            self.assertEqual(facts["environment_folder_file_count"], 2)
            self.assertEqual(facts["files_not_copied"]["environment_folder_files"], 2)

            current = facts["current_state"]
            self.assertFalse(current["graded"])
            self.assertFalse(current["same_as_evaluated_commit"])
            self.assertEqual(current["commit"]["message"],
                             "Fix plot for <repo_owner> after the deadline")
            self.assertIn("plot.py", current["tracked_files"])
            kept = fixture.project / current["kept_for_instructor_in"]
            self.assertEqual(sorted(p.name for p in kept.iterdir() if p.is_file()), [
                "artifact_001_README.md", "artifact_002_student_analysis.py",
                "artifact_004_plot.py"])
            self.assertIn("<student_name>",
                          (kept / "artifact_001_README.md").read_text(encoding="utf-8"))

            serialized = json.dumps(repo_files) + json.dumps(facts)
            for text in IDENTITY_STRINGS:
                self.assertNotIn(text.lower(), serialized.lower())

    def test_commit_within_the_submission_minute_counts_as_before(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RepositoryProject(directory)
            fixture.repos[("avery-gh", "AverySmoke-pr02")] = make_repo(
                fixture.root / "remotes" / "minute", [
                    ("first", {"a.py": "1\n"}, local(2026, 10, 18, 22, 53, 40)),
                    ("second", {"b.py": "2\n"}, local(2026, 10, 18, 22, 54, 5)),
                ])
            with NoNetwork(fixture):
                _, _, manifest = write_submission_manifest(fixture.project)
            [facts] = facts_of(manifest, "Student_001")
            self.assertEqual(facts["evaluated_commit"]["message"], "first")
            self.assertEqual(facts["commits_after_submission_count"], 1)

    def test_no_commit_before_submission_grades_no_files_but_keeps_current(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RepositoryProject(directory)
            fixture.repos[("avery-gh", "AverySmoke-pr02")] = make_repo(
                fixture.root / "remotes" / "late",
                [("late", {"a.py": "1\n"}, local(2026, 10, 20, 8, 0))])
            with NoNetwork(fixture):
                _, _, manifest = write_submission_manifest(fixture.project)
            [facts] = facts_of(manifest, "Student_001")
            self.assertIsNone(facts["evaluated_commit"])
            self.assertEqual(facts["evaluated_state"], "none: no commit at or before submission")
            self.assertIsNone(facts["files_folder"])
            self.assertEqual(len(files_of(manifest, "Student_001", "github_repository")), 1)
            self.assertTrue((fixture.project / facts["current_state"]["kept_for_instructor_in"]
                             ).is_dir())

    def test_unknown_submission_time_evaluates_the_latest_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, manifest, _ = prepare(directory, submitted="sometime")
            [facts] = facts_of(manifest, "Student_001")
            self.assertIsNone(facts["submission_time"])
            self.assertEqual(facts["evaluated_state"], "latest commit: submission time unknown")
            self.assertTrue(facts["current_state"]["same_as_evaluated_commit"])
            self.assertNotIn("kept_for_instructor_in", facts["current_state"])
            self.assertFalse((fixture.project / "grader/repository_current").exists())

    def test_public_but_empty_is_not_unreachable(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RepositoryProject(directory)
            fixture.repos[("avery-gh", "AverySmoke-pr02")] = make_repo(
                fixture.root / "remotes" / "empty", [])
            with NoNetwork(fixture):
                _, _, manifest = write_submission_manifest(fixture.project)
            [facts] = facts_of(manifest, "Student_001")
            self.assertEqual(facts["fetch_status"], "public_but_empty")
            self.assertTrue(facts["publicly_reachable"])
            self.assertEqual(facts["commit_count"], 0)
            self.assertEqual(manifest["repository_evidence"]["fetch_status_counts"],
                             {"no_repository_link": 1, "not_public_or_not_found": 1,
                              "public_but_empty": 1})

    def test_failed_link_lists_candidates_privately_and_never_fetches_them(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, manifest, guard = prepare(directory)
            [facts] = facts_of(manifest, "Student_002")
            self.assertEqual(facts["fetch_status"], "not_public_or_not_found")
            self.assertFalse(facts["publicly_reachable"])
            self.assertEqual(facts["candidate_lookup"], "looked_up")
            self.assertEqual(facts["candidate_repositories_for_instructor_review"], 2)
            self.assertIn("must not be graded", facts["candidate_note"])
            self.assertEqual(len(files_of(manifest, "Student_002", "github_repository")), 1)

            private = json.loads((fixture.project / "grader/repository_map.json")
                                 .read_text(encoding="utf-8"))
            self.assertEqual(
                [(c["repository"], c["matched_pattern"]) for c in private["Student_002"]["candidates"]],
                [("missing_repo", "missing-repo"), ("MissingRepo", "missing-repo")])
            self.assertEqual(private["Student_002"]["candidates"][0]["url"],
                             "https://github.com/bfake/missing_repo")
            cloned = [a for a in guard.git_calls if "clone" in a]
            self.assertEqual(len(cloned), 2)   # Avery's repo and Blake's link only.
            self.assertFalse(any("missing_repo" in " ".join(a) for a in cloned))

    def test_profile_only_link_looks_up_candidates_by_project_name(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, manifest, guard = prepare(directory)
            [facts] = facts_of(manifest, "Student_003")
            self.assertEqual(facts["fetch_status"], "no_repository_link")
            self.assertEqual(facts["repository_link"], "github_profile_link_only")
            self.assertEqual(facts["candidate_repositories_for_instructor_review"], 2)
            private = json.loads((fixture.project / "grader/repository_map.json")
                                 .read_text(encoding="utf-8"))
            self.assertEqual(
                [c["repository"] for c in private["Student_003"]["candidates"]],
                ["Demo-Course-PR02", "demo_pr02"])
            self.assertEqual(guard.lookups, ["bfake", "cmock"])

    def test_candidate_lookup_failure_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RepositoryProject(directory)
            with NoNetwork(fixture), \
                 patch.object(repo_evidence, "list_public_repositories", return_value=None):
                _, _, manifest = write_submission_manifest(fixture.project)
            [facts] = facts_of(manifest, "Student_002")
            self.assertEqual(facts["candidate_lookup"], "lookup_failed")
            self.assertEqual(facts["candidate_repositories_for_instructor_review"], 0)

    def test_summary_reports_students_by_id_without_names_or_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, manifest, _ = prepare(directory)
            summary = manifest["repository_evidence"]
            self.assertEqual(summary["index_entries"], 4)
            self.assertEqual(summary["index_entries_with_github_links"], 4)
            self.assertEqual(summary["index_entries_with_github_links_not_matched"], 1)
            self.assertEqual(summary["students_without_recognized_repository_link"],
                             {"Student_003": "github_profile_link_only"})
            self.assertEqual(summary["candidate_repositories_for_instructor_review"],
                             {"Student_002": 2, "Student_003": 2})
            for text in IDENTITY_STRINGS + ["someone", "Jordan"]:
                self.assertNotIn(text.lower(), json.dumps(summary).lower())
            self.assertEqual(list((fixture.project / "grader").glob(".repo*")), [])

    def test_cli_reports_students_by_id_only(self):
        from ai_grader.cli import main
        with tempfile.TemporaryDirectory() as directory:
            fixture = RepositoryProject(directory)
            output = io.StringIO()
            with NoNetwork(fixture), \
                 patch("sys.argv", ["ai_grader", "prepare-submissions", str(fixture.project)]), \
                 patch("sys.stdout", output):
                main()
            text = output.getvalue()
            self.assertIn("Student_003: GitHub profile link without a repository", text)
            self.assertIn("Student_002: 2 candidate(s)", text)
            for name in IDENTITY_STRINGS + ["someone", "Jordan"]:
                self.assertNotIn(name.lower(), text.lower())

    def test_inactive_without_index_or_github_links(self):
        for index_html in (None, "<html><body><p>No links here.</p></body></html>"):
            with tempfile.TemporaryDirectory() as directory:
                fixture, manifest, guard = prepare(directory, index_html=index_html)
                self.assertNotIn("repository_evidence", manifest)
                self.assertEqual(guard.git_calls, [])
                self.assertEqual(guard.lookups, [])
                self.assertFalse((fixture.project / "grader/repository_map.json").exists())
                self.assertFalse((fixture.project / "grader/repository_current").exists())
                for submission in manifest["submissions"]:
                    self.assertEqual([f["path"] for f in submission["files"]],
                                     ["artifact_001_analysis.py"])

    def test_refuses_to_overwrite_kept_current_state(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RepositoryProject(directory)
            (fixture.project / "grader/repository_current").mkdir()
            with NoNetwork(fixture), self.assertRaises(FileExistsError):
                write_submission_manifest(fixture.project)
            self.assertFalse((fixture.project / "grader/anonymized_submissions").exists())

    def test_grading_receives_repository_evidence(self):
        from ai_grader.grading import build_student_input, load_grading_inputs
        with tempfile.TemporaryDirectory() as directory:
            fixture, _, _ = prepare(directory)
            _, spec, _, manifest, anonymized = load_grading_inputs(fixture.project)
            submission = next(s for s in manifest["submissions"]
                              if s["student_id"] == "Student_001")
            request = build_student_input(spec, submission,
                                          anonymized / "Student_001", "Instructions.")
            prompt = request[0]["content"][0]["text"]
            self.assertIn("EVIDENCE TYPE: github_repository_facts", prompt)
            self.assertIn("EVIDENCE TYPE: github_repository_file", prompt)
            self.assertIn('"commit_count": 3', prompt)
            self.assertNotIn("print('plot')", prompt)   # Current state is not graded.

    def test_refresh_checks_accept_repository_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, _, _ = prepare(directory)
            refresh_submission_checks(fixture.project)

    def test_refresh_rejects_unredacted_owner_in_repository_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, _, _ = prepare(directory)
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


class Utf16Tests(unittest.TestCase):
    UTF16_TEXT = "Result by Avery Smoke: 42\r\nmean speed\r\n"

    def test_utf16_submission_and_repository_text_is_decoded_and_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RepositoryProject(directory)
            folder = next((fixture.project / "submissions").glob("100001-*"))
            (folder / "output.txt").write_bytes(
                codecs.BOM_UTF16_LE + self.UTF16_TEXT.encode("utf-16-le"))
            fixture.repos[("avery-gh", "AverySmoke-pr02")] = make_repo(
                fixture.root / "remotes" / "utf16", [(
                    "add output",
                    {"out.txt": codecs.BOM_UTF16_LE + self.UTF16_TEXT.encode("utf-16-le")},
                    local(2026, 10, 18, 12, 0),
                )])
            with NoNetwork(fixture):
                _, _, manifest = write_submission_manifest(fixture.project)
            texts = {Path(f["path"]).name: f["content"] for f in files_of(manifest, "Student_001")
                     if f["file_type"] == "text"}
            expected = "Result by <student_name>: 42\nmean speed\n"
            self.assertEqual(texts["artifact_002_output.txt"], expected)
            repo_text = next(v for k, v in texts.items() if k.endswith("_out.txt"))
            self.assertEqual(repo_text, expected)
            refresh_submission_checks(fixture.project)


if __name__ == "__main__":
    unittest.main()
