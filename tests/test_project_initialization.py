import builtins
import contextlib
import errno
import io
import os
import shutil
import stat
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from ai_grader.cli import main
from ai_grader.project_initialization import (
    PROJECT_SUBFOLDERS, RENAME_RETRY_DELAYS, ProjectInitializationError,
    count_items, init_project,
)

# Synthetic Brightspace download; no real student data.
STUDENT_FOLDERS = [
    "100001-2000001 - asmoke Avery Smoke - Oct 18, 2026 1053 PM",
    "100002-2000002 - bfake Blake Fake - Oct 18, 2026 1101 PM",
    "100003-2000003 - cmock Casey Mock - Oct 19, 2026 0915 AM",
]
STUDENT_STRINGS = ["asmoke", "Avery", "bfake", "Blake", "cmock", "Casey", "2000001"]


def snapshot(root):
    """Map every path under root to its bytes (None for folders)."""
    return {
        path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
        for path in sorted(root.rglob("*"))
    }


@contextlib.contextmanager
def no_submission_access(project):
    """
    Fail if init reads any file in the project other than a top-level ZIP, or
    lists a submission folder. Writing (ZIP extraction) is allowed.
    """
    project = project.resolve()
    real_open, real_io_open, real_os_open = builtins.open, io.open, os.open
    real_iterdir = Path.iterdir

    def inside_project(path):
        return Path(path).resolve().is_relative_to(project)

    def fd_path(fd):
        """Path behind an open descriptor (Linux), or None if unknown."""
        link = f"/proc/self/fd/{fd}"
        return Path(os.readlink(link)) if os.path.islink(link) else None

    def opened_path(file, dir_fd):
        """Absolute path an open call refers to, or None if it can't be located."""
        if isinstance(file, int):
            return fd_path(file)
        path = Path(os.fspath(file))
        if dir_fd is not None and not path.is_absolute():
            # shutil.rmtree on Linux opens entries relative to a directory fd.
            base = fd_path(dir_fd)
            return None if base is None else base / path
        return path

    def is_regular_file(file, dir_fd):
        try:
            if dir_fd is not None and not isinstance(file, int):
                info = os.stat(file, dir_fd=dir_fd)
            else:
                info = os.stat(file)
        except OSError:
            return False  # Nothing to read; the real open call will fail.
        return stat.S_ISREG(info.st_mode)

    def is_top_level_zip(path):
        path = Path(path).resolve()
        return path.parent == project and path.suffix.lower() == ".zip"

    def is_read(args, kwargs, real):
        mode = args[0] if args else kwargs.get("mode", kwargs.get("flags", "r"))
        if real is real_os_open:
            return not mode & (os.O_WRONLY | os.O_RDWR)
        return not any(flag in mode for flag in "wxa+")

    def guard(real):
        def guarded(file, *args, **kwargs):
            dir_fd = kwargs.get("dir_fd")
            # Directories may be opened (rmtree does on Linux); only regular
            # files count as reads. A file we can't locate counts as inside.
            if is_read(args, kwargs, real) and is_regular_file(file, dir_fd):
                path = opened_path(file, dir_fd)
                if path is None or (inside_project(path) and not is_top_level_zip(path)):
                    raise AssertionError(f"init-project read a file: {file}")
            return real(file, *args, **kwargs)
        return guarded

    def guarded_iterdir(self):
        if self.resolve() != project and inside_project(self):
            raise AssertionError(f"init-project listed a folder's contents: {self}")
        return real_iterdir(self)

    with patch("builtins.open", guard(real_open)), \
         patch("io.open", guard(real_io_open)), \
         patch("os.open", guard(real_os_open)), \
         patch.object(Path, "iterdir", guarded_iterdir):
        yield


def make_download(project, extra=True):
    """Create an extracted Brightspace download in an existing project folder."""
    project.mkdir(parents=True)
    for name in STUDENT_FOLDERS:
        (project / name).mkdir()
        (project / name / "analysis.py").write_text("print('synthetic')\n")
    (project / "index.html").write_text("<html>synthetic index</html>")
    if extra:
        (project / "Project 1 Download Oct 18, 2026.zip").write_bytes(b"PK\x05\x06")
        (project / "notes.txt").write_text("instructor notes")
        (project / "rubric").mkdir()
        (project / "rubric" / "instructor_rubric.md").write_text("# Existing rubric")


def run_cli(root, *args):
    output = io.StringIO()
    code = 0
    with patch.object(Path, "cwd", return_value=root), \
         patch("sys.argv", ["ai_grader", "init-project", *args]), \
         patch("sys.stdout", output):
        try:
            main()
        except SystemExit as exit_:
            code = exit_.code
    return code, output.getvalue()


LINUX_DIR_FD = os.path.isdir("/proc/self/fd") and os.open in os.supports_dir_fd


class ReadGuardTests(unittest.TestCase):
    """The guard must flag file reads but allow directory opens (rmtree)."""

    def make_tree(self, root):
        project = root / "projects" / "demo"
        folder = project / "submissions" / STUDENT_FOLDERS[0]
        folder.mkdir(parents=True)
        (folder / "analysis.py").write_text("print('synthetic')\n")
        make_zip(project / ZIP_NAME)
        return project, folder

    def test_flags_file_reads_inside_project(self):
        with tempfile.TemporaryDirectory() as directory:
            project, folder = self.make_tree(Path(directory))
            target = folder / "analysis.py"
            reads = [
                lambda: open(target),
                lambda: io.open(target, "rb"),
                lambda: target.read_bytes(),
                lambda: os.close(os.open(target, os.O_RDONLY)),
            ]
            for read in reads:
                with no_submission_access(project), self.assertRaises(AssertionError):
                    read()

    def test_allows_directory_opens_writes_and_top_level_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            project, folder = self.make_tree(Path(directory))
            with no_submission_access(project):
                try:
                    os.close(os.open(folder, os.O_RDONLY))
                except PermissionError:
                    pass  # Windows can't os.open a directory; the guard allowed it.
                (folder / "new.txt").write_text("written")
                with zipfile.ZipFile(project / ZIP_NAME) as archive:
                    archive.namelist()
                with open(Path(directory) / "outside.txt", "w") as file:
                    file.write("x")
                with open(Path(directory) / "outside.txt") as file:
                    file.read()

    def test_allows_rmtree_inside_project(self):
        with tempfile.TemporaryDirectory() as directory:
            project, folder = self.make_tree(Path(directory))
            (folder / "plots").mkdir()
            (folder / "plots" / "speed_plot.png").write_bytes(b"\x89PNG synthetic")
            with no_submission_access(project):
                shutil.rmtree(project / "submissions")
            self.assertFalse((project / "submissions").exists())

    @unittest.skipUnless(LINUX_DIR_FD, "needs /proc/self/fd and os.open dir_fd support")
    def test_dir_fd_relative_opens(self):
        with tempfile.TemporaryDirectory() as directory:
            project, folder = self.make_tree(Path(directory))
            outside = Path(directory) / "outside"
            outside.mkdir()
            (outside / "data.txt").write_text("x")
            dir_fd = os.open(folder, os.O_RDONLY)
            outside_fd = os.open(outside, os.O_RDONLY)
            try:
                with no_submission_access(project):
                    with self.assertRaises(AssertionError):
                        os.open("analysis.py", os.O_RDONLY, dir_fd=dir_fd)
                    (folder / "plots").mkdir()
                    os.close(os.open("plots", os.O_RDONLY, dir_fd=dir_fd))
                    os.close(os.open("data.txt", os.O_RDONLY, dir_fd=outside_fd))
            finally:
                os.close(dir_fd)
                os.close(outside_fd)


class NewProjectTests(unittest.TestCase):
    def test_new_project_creates_all_folders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = init_project("demo", workspace_path=root)
            project = root / "projects" / "demo"
            # init_project resolves the workspace; resolve here too so a Windows
            # short-name TEMP (e.g. RUNNER~1 on CI) compares equal.
            self.assertEqual(plan["project_path"], project.resolve())
            self.assertFalse(plan["exists"])
            self.assertEqual(sorted(p.name for p in project.iterdir()),
                             sorted(PROJECT_SUBFOLDERS))

    def test_new_project_cli_output_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            code, output = run_cli(Path(directory), "demo")
            self.assertEqual(code, 0)
            self.assertIn("Project initialized successfully:", output)
            for folder in PROJECT_SUBFOLDERS:
                self.assertIn(f"  - {folder}\\", output)
            self.assertIn("3. Place downloaded Brightspace submissions in submissions\\", output)
            self.assertNotIn("Moved into submissions", output)
            self.assertNotIn("Not moved", output)

    def test_dry_run_on_new_project_creates_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, output = run_cli(root, "demo", "--dry-run")
            self.assertEqual(code, 0)
            self.assertIn("Would create project folder", output)
            self.assertFalse((root / "projects").exists())


class ProjectLocationTests(unittest.TestCase):
    """Running from the wrong folder must never nest projects\\projects\\NAME."""

    def test_inside_projects_folder_uses_existing_project(self):
        with tempfile.TemporaryDirectory() as directory:
            projects = Path(directory) / "projects"
            make_download(projects / "demo", extra=False)
            code, output = run_cli(projects, "demo")
            self.assertEqual(code, 0, output)
            self.assertFalse((projects / "projects").exists())
            self.assertIn("Running inside the projects folder, so using:", output)
            self.assertEqual(len(list((projects / "demo" / "submissions").iterdir())), 4)

    def test_inside_projects_folder_creates_new_project_there(self):
        with tempfile.TemporaryDirectory() as directory:
            projects = Path(directory) / "projects"
            projects.mkdir()
            plan = init_project("demo", workspace_path=projects)
            self.assertEqual(plan["project_path"], projects.resolve() / "demo")
            self.assertFalse((projects / "projects").exists())
            self.assertEqual(sorted(p.name for p in (projects / "demo").iterdir()),
                             sorted(PROJECT_SUBFOLDERS))

    def test_inside_the_project_folder_uses_it(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "projects" / "demo"
            make_download(project, extra=False)
            code, output = run_cli(project, "demo")
            self.assertEqual(code, 0, output)
            self.assertFalse((project / "projects").exists())
            self.assertIn("Running inside the project folder, so using it:", output)
            self.assertTrue((project / "submissions" / "index.html").is_file())

    def test_name_given_as_projects_path_is_used(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = init_project(str(Path("projects") / "demo"), workspace_path=root)
            self.assertEqual(plan["project_path"], root.resolve() / "projects" / "demo")
            self.assertFalse((root / "projects" / "projects").exists())

    def test_path_outside_a_projects_folder_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = snapshot(root)
            code, output = run_cli(root, str(Path("elsewhere") / "demo"))
            self.assertEqual(code, 1)
            self.assertIn("not inside a projects folder", output)
            self.assertEqual(snapshot(root), before)

    def test_stray_folder_with_same_name_stops_without_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_download(root / "demo", extra=False)
            before = snapshot(root)
            for args in (("demo",), ("demo", "--dry-run")):
                code, output = run_cli(root, *args)
                self.assertEqual(code, 1)
                self.assertIn("found a folder named demo here", output)
                self.assertIn("Nothing was changed", output)
                self.assertEqual(snapshot(root), before)
            for text in STUDENT_STRINGS:
                self.assertNotIn(text, output)

    def test_stray_folder_is_ignored_when_project_already_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "demo").mkdir()
            (root / "projects" / "demo").mkdir(parents=True)
            plan = init_project("demo", workspace_path=root)
            self.assertEqual(plan["project_path"], root.resolve() / "projects" / "demo")
            self.assertEqual(list((root / "demo").iterdir()), [])

    def test_ambiguous_inside_projects_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            projects = Path(directory) / "projects"
            (projects / "projects" / "demo").mkdir(parents=True)
            before = snapshot(Path(directory))
            with self.assertRaises(ProjectInitializationError):
                init_project("demo", workspace_path=projects)
            self.assertEqual(snapshot(Path(directory)), before)

    def test_workspace_root_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, output = run_cli(root, "demo")
            self.assertEqual(code, 0)
            self.assertTrue((root / "projects" / "demo" / "grader").is_dir())
            self.assertNotIn("so using", output)


class ExistingFolderTests(unittest.TestCase):
    def test_moves_brightspace_items_and_keeps_everything_else(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "projects" / "demo"
            make_download(project)
            with no_submission_access(project):
                plan = init_project("demo", workspace_path=root)

            self.assertTrue(plan["exists"])
            self.assertEqual(plan["missing_subfolders"],
                             [f for f in PROJECT_SUBFOLDERS if f != "rubric"])
            for name in STUDENT_FOLDERS:
                self.assertFalse((project / name).exists())
                self.assertEqual(
                    (project / "submissions" / name / "analysis.py").read_text(),
                    "print('synthetic')\n")
            self.assertTrue((project / "submissions" / "index.html").is_file())
            self.assertFalse((project / "index.html").exists())
            self.assertEqual(plan["not_moved"],
                             ["notes.txt", "Project 1 Download Oct 18, 2026.zip"])
            self.assertTrue((project / "notes.txt").is_file())
            self.assertTrue((project / "Project 1 Download Oct 18, 2026.zip").is_file())
            self.assertEqual((project / "rubric" / "instructor_rubric.md").read_text(),
                             "# Existing rubric")

    def test_cli_reports_counts_and_never_student_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_download(root / "projects" / "demo")
            code, output = run_cli(root, "demo")
            self.assertEqual(code, 0)
            self.assertIn("Moved into submissions\\: 3 submission folders, 1 index.html", output)
            self.assertIn("Not moved (left in place):", output)
            self.assertIn("  - notes.txt", output)
            self.assertIn("Created folders:", output)
            self.assertNotIn("  - rubric\\", output)
            self.assertIn("3. Confirm that submissions\\ holds", output)
            for text in STUDENT_STRINGS:
                self.assertNotIn(text, output)

    def test_dry_run_changes_nothing_and_prints_counts_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "projects" / "demo"
            make_download(project)
            before = snapshot(root)
            with no_submission_access(project):
                code, output = run_cli(root, "demo", "--dry-run")
            self.assertEqual(code, 0)
            self.assertEqual(snapshot(root), before)
            self.assertIn("Dry run: nothing was changed.", output)
            self.assertIn("Would move into submissions\\: 3 submission folders, 1 index.html",
                          output)
            self.assertIn("  - datasets\\", output)
            for text in STUDENT_STRINGS:
                self.assertNotIn(text, output)

    def test_singular_count_wording(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "projects" / "demo"
            project.mkdir(parents=True)
            (project / STUDENT_FOLDERS[0]).mkdir()
            _, output = run_cli(root, "demo", "--dry-run")
            self.assertIn("1 submission folder, 0 index.html", output)

    def test_conflict_stops_before_any_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "projects" / "demo"
            make_download(project)
            (project / "submissions" / STUDENT_FOLDERS[1]).mkdir(parents=True)
            (project / "submissions" / "index.html").write_text("older index")
            before = snapshot(root)

            with no_submission_access(project), \
                 self.assertRaises(ProjectInitializationError) as raised:
                init_project("demo", workspace_path=root)
            self.assertIn("1 submission folder, 1 index.html", str(raised.exception))
            self.assertIn("Nothing was changed", str(raised.exception))
            self.assertEqual(snapshot(root), before)

            code, output = run_cli(root, "demo")
            self.assertEqual(code, 1)
            self.assertEqual(snapshot(root), before)
            for text in STUDENT_STRINGS:
                self.assertNotIn(text, output)

    def test_dry_run_reports_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "projects" / "demo"
            make_download(project)
            (project / "submissions" / STUDENT_FOLDERS[0]).mkdir(parents=True)
            code, output = run_cli(root, "demo", "--dry-run")
            self.assertEqual(code, 0)
            self.assertIn("would stop without changing anything", output)
            self.assertIn("1 submission folder, 0 index.html", output)
            for text in STUDENT_STRINGS:
                self.assertNotIn(text, output)

    def test_brightspace_named_file_is_counted_not_moved_or_named(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "projects" / "demo"
            project.mkdir(parents=True)
            stray = project / (STUDENT_FOLDERS[0] + " - report.pdf")
            stray.write_bytes(b"%PDF-1.4")
            code, output = run_cli(root, "demo")
            self.assertEqual(code, 0)
            self.assertTrue(stray.is_file())
            self.assertIn("1 Brightspace-named file(s) that are not submission folders",
                          output)
            for text in STUDENT_STRINGS:
                self.assertNotIn(text, output)

    def test_rerun_on_initialized_project_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_download(root / "projects" / "demo", extra=False)
            init_project("demo", workspace_path=root)
            before = snapshot(root)
            code, output = run_cli(root, "demo")
            self.assertEqual(code, 0)
            self.assertEqual(snapshot(root), before)
            self.assertIn("All standard folders already existed.", output)
            self.assertIn("0 submission folders, 0 index.html", output)

    def test_standard_name_that_is_a_file_stops_without_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "projects" / "demo"
            make_download(project, extra=False)
            (project / "grader").write_text("not a folder")
            before = snapshot(root)
            with self.assertRaises(ProjectInitializationError):
                init_project("demo", workspace_path=root)
            self.assertEqual(snapshot(root), before)

    def test_failed_move_reports_progress_without_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "projects" / "demo"
            make_download(project, extra=False)
            real_rename = Path.rename
            sources = []

            def locked_rename(self, target):
                # The second item stays locked through every retry.
                if self not in sources:
                    sources.append(self)
                if len(sources) > 1 and self == sources[1]:
                    raise PermissionError(13, "Access is denied", str(self))
                return real_rename(self, target)

            with patch.object(Path, "rename", locked_rename), \
                 patch("ai_grader.project_initialization.time.sleep") as sleep, \
                 self.assertRaises(ProjectInitializationError) as raised:
                init_project("demo", workspace_path=root)
            message = str(raised.exception)
            self.assertIn("after placing 1 of 4", message)
            self.assertIn("Access is denied", message)
            for text in STUDENT_STRINGS:
                self.assertNotIn(text, message)
            self.assertEqual([c.args[0] for c in sleep.call_args_list],
                             list(RENAME_RETRY_DELAYS))

            # Rerunning finishes the remaining moves without conflicts.
            init_project("demo", workspace_path=root)
            self.assertEqual(len(list((project / "submissions").iterdir())), 4)


class TemporaryLockTests(unittest.TestCase):
    """Folder moves retry briefly while a Windows file lock clears."""

    def locked_for(self, attempts, error=None):
        """Patch Path.rename so each source fails `attempts` times, then works."""
        real_rename = Path.rename
        failures = {}
        error = error or (lambda path: PermissionError(
            13, "The process cannot access the file because it is being used "
            "by another process", str(path)))

        def rename(path, target):
            failures[path] = failures.get(path, 0) + 1
            if failures[path] <= attempts:
                raise error(path)
            return real_rename(path, target)

        return patch.object(Path, "rename", rename), failures

    def test_temporary_lock_on_move_is_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "projects" / "demo"
            make_download(project, extra=False)
            locked, failures = self.locked_for(2)
            with locked, patch("ai_grader.project_initialization.time.sleep") as sleep:
                code, output = run_cli(root, "demo")
            self.assertEqual(code, 0, output)
            self.assertIn("Moved into submissions\\: 3 submission folders, 1 index.html",
                          output)
            self.assertEqual(len(list((project / "submissions").iterdir())), 4)
            self.assertTrue(all(count == 3 for count in failures.values()))
            # Two waits per item, with backoff: 0.125 s, then 0.25 s.
            self.assertEqual([c.args[0] for c in sleep.call_args_list],
                             [0.125, 0.25] * 4)

    def test_temporary_lock_on_zip_extraction_is_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "projects" / "demo"
            project.mkdir(parents=True)
            make_zip(project / ZIP_NAME)
            locked, _ = self.locked_for(3)
            with locked, patch("ai_grader.project_initialization.time.sleep"):
                plan = init_project("demo", workspace_path=root)
            self.assertEqual(count_items(plan, "extract"), (3, 1))
            self.assertEqual(len(list((project / "submissions").iterdir())), 4)
            self.assertEqual(staging_folders(project), [])

    def test_windows_sharing_violation_is_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "projects" / "demo"
            make_download(project, extra=False)

            def sharing_violation(path):
                error = OSError(errno.EIO, "Sharing violation", str(path))
                error.winerror = 32  # Set directly so the test also runs on Linux.
                return error

            locked, _ = self.locked_for(1, sharing_violation)
            with locked, patch("ai_grader.project_initialization.time.sleep"):
                init_project("demo", workspace_path=root)
            self.assertEqual(len(list((project / "submissions").iterdir())), 4)

    @unittest.skipUnless(os.name == "nt", "open files block folder renames only on Windows")
    def test_real_windows_lock_released_during_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "projects" / "demo"
            make_download(project, extra=False)
            held = open(project / STUDENT_FOLDERS[0] / "analysis.py", "rb")
            timer = threading.Timer(0.3, held.close)
            timer.start()
            try:
                init_project("demo", workspace_path=root)
            finally:
                timer.cancel()
                held.close()
            self.assertEqual(len(list((project / "submissions").iterdir())), 4)

    def test_lock_that_outlasts_retries_stops_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "projects" / "demo"
            make_download(project, extra=False)
            locked, failures = self.locked_for(99)
            with locked, patch("ai_grader.project_initialization.time.sleep") as sleep, \
                 self.assertRaises(ProjectInitializationError) as raised:
                init_project("demo", workspace_path=root)
            self.assertIn("Stopped after placing 0 of 4", str(raised.exception))
            self.assertEqual(list(failures.values()), [len(RENAME_RETRY_DELAYS) + 1])
            self.assertAlmostEqual(sum(c.args[0] for c in sleep.call_args_list), 1.875)
            self.assertEqual(list((project / "submissions").iterdir()), [])

    def test_other_errors_are_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "projects" / "demo"
            make_download(project, extra=False)
            locked, failures = self.locked_for(
                99, lambda path: OSError(errno.ENOSPC, "No space left on device"))
            with locked, patch("ai_grader.project_initialization.time.sleep") as sleep, \
                 self.assertRaises(ProjectInitializationError) as raised:
                init_project("demo", workspace_path=root)
            self.assertIn("No space left on device", str(raised.exception))
            self.assertEqual(list(failures.values()), [1])
            sleep.assert_not_called()

    def test_item_appearing_during_retry_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "projects" / "demo"
            make_download(project, extra=False)
            locked, _ = self.locked_for(99)
            first = sorted(STUDENT_FOLDERS, key=str.lower)[0]
            intruder = project / "submissions" / first

            def appear(delay):
                intruder.mkdir(exist_ok=True)

            with locked, patch("ai_grader.project_initialization.time.sleep",
                               side_effect=appear), \
                 self.assertRaises(ProjectInitializationError) as raised:
                init_project("demo", workspace_path=root)
            self.assertIn("appeared in submissions", str(raised.exception))
            self.assertEqual(list(intruder.iterdir()), [])
            self.assertTrue((project / first / "analysis.py").is_file())


ZIP_NAME = "Project 1 Download Oct 18, 2026.zip"


def make_zip(path, readme=True):
    """Write a synthetic Brightspace download ZIP."""
    with zipfile.ZipFile(path, "w") as archive:
        for name in STUDENT_FOLDERS:
            archive.writestr(f"{name}/analysis.py", "print('synthetic')\n")
            archive.writestr(f"{name}/plots/speed_plot.png", b"\x89PNG synthetic")
        archive.writestr("index.html", "<html>synthetic index</html>")
        if readme:
            archive.writestr("README.txt", "not a submission")


def make_windows_extraction(project):
    """Mimic Windows "Extract All": a wrapper folder named after the ZIP."""
    wrapper = project / Path(ZIP_NAME).stem
    with zipfile.ZipFile(project / ZIP_NAME) as archive:
        archive.extractall(wrapper)
    (wrapper / "README.txt").unlink()
    return wrapper


def staging_folders(project):
    return [p for p in project.iterdir() if p.name.startswith(".ai_grader_extract_")]


class ZipDownloadTests(unittest.TestCase):
    def make_project(self, root, readme=True):
        project = root / "projects" / "demo"
        project.mkdir(parents=True)
        make_zip(project / ZIP_NAME, readme=readme)
        return project

    def assert_extracted(self, project):
        for name in STUDENT_FOLDERS:
            folder = project / "submissions" / name
            self.assertEqual((folder / "analysis.py").read_text(), "print('synthetic')\n")
            self.assertEqual((folder / "plots" / "speed_plot.png").read_bytes(),
                             b"\x89PNG synthetic")
        self.assertTrue((project / "submissions" / "index.html").is_file())

    def test_extracts_zip_keeps_it_and_reports_counts_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = self.make_project(root)
            zip_bytes = (project / ZIP_NAME).read_bytes()
            with no_submission_access(project):
                code, output = run_cli(root, "demo")
            self.assertEqual(code, 0, output)
            self.assert_extracted(project)
            self.assertEqual((project / ZIP_NAME).read_bytes(), zip_bytes)
            self.assertFalse((project / "README.txt").exists())
            self.assertEqual(staging_folders(project), [])
            self.assertIn("Extracted from Brightspace ZIP into submissions\\: "
                          "3 submission folders, 1 index.html", output)
            self.assertIn(f"  - {ZIP_NAME} (kept; 1 other file(s) in it are not extracted)",
                          output)
            self.assertIn("3. Confirm that submissions\\ holds", output)
            for text in STUDENT_STRINGS:
                self.assertNotIn(text, output)

    def test_dry_run_with_zip_changes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = self.make_project(root)
            before = snapshot(root)
            with no_submission_access(project):
                code, output = run_cli(root, "demo", "--dry-run")
            self.assertEqual(code, 0)
            self.assertEqual(snapshot(root), before)
            self.assertIn("Would extract from Brightspace ZIP into submissions\\: "
                          "3 submission folders, 1 index.html", output)
            for text in STUDENT_STRINGS:
                self.assertNotIn(text, output)

    def test_rerun_after_extraction_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_project(root)
            init_project("demo", workspace_path=root)
            before = snapshot(root)
            code, output = run_cli(root, "demo")
            self.assertEqual(code, 0, output)
            self.assertEqual(snapshot(root), before)
            self.assertIn("Already in submissions\\ (skipped): "
                          "3 submission folders, 1 index.html", output)
            self.assertIn("ZIP into submissions\\: 0 submission folders, 0 index.html",
                          output)

    def test_windows_extract_all_next_to_zip_moves_folders_and_skips_zip_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = self.make_project(root)
            wrapper = make_windows_extraction(project)
            with patch("zipfile.ZipFile.extract",
                       side_effect=AssertionError("extracted a duplicate")):
                code, output = run_cli(root, "demo")
            self.assertEqual(code, 0, output)
            self.assert_extracted(project)
            self.assertFalse(wrapper.exists())
            self.assertTrue((project / ZIP_NAME).is_file())
            self.assertIn("Moved into submissions\\: 3 submission folders, 1 index.html",
                          output)
            self.assertIn(f"Removed the emptied download folder: {wrapper.name}", output)

    def test_wrapper_with_other_files_is_kept_and_listed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = self.make_project(root)
            wrapper = make_windows_extraction(project)
            (project / ZIP_NAME).unlink()
            (wrapper / "instructor notes.txt").write_text("keep me")
            code, output = run_cli(root, "demo")
            self.assertEqual(code, 0, output)
            self.assert_extracted(project)
            self.assertTrue((wrapper / "instructor notes.txt").is_file())
            self.assertIn(f"  - {wrapper.name} (download folder that still holds "
                          "other items)", output)

    def test_zip_and_extracted_copy_that_differ_stop_without_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = self.make_project(root)
            wrapper = make_windows_extraction(project)
            (wrapper / STUDENT_FOLDERS[2] / "analysis.py").write_text("edited after download\n")
            before = snapshot(root)
            code, output = run_cli(root, "demo")
            self.assertEqual(code, 1)
            self.assertEqual(snapshot(root), before)
            self.assertIn("1 submission folder, 0 index.html", output)
            self.assertIn("Nothing was changed", output)
            for text in STUDENT_STRINGS:
                self.assertNotIn(text, output)

    def test_same_size_edit_in_extracted_copy_is_a_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = self.make_project(root)
            wrapper = make_windows_extraction(project)
            # Same length, different bytes. Written as bytes so Windows newline
            # translation can't change the length and let the size check catch it.
            (wrapper / STUDENT_FOLDERS[1] / "analysis.py").write_bytes(
                (wrapper / STUDENT_FOLDERS[1] / "analysis.py").read_bytes().upper())
            before = snapshot(root)
            with self.assertRaises(ProjectInitializationError) as raised:
                init_project("demo", workspace_path=root)
            self.assertIn("1 submission folder, 0 index.html", str(raised.exception))
            self.assertEqual(snapshot(root), before)

    def test_same_size_edit_in_submissions_blocks_rerun_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = self.make_project(root)
            init_project("demo", workspace_path=root)
            edited = project / "submissions" / STUDENT_FOLDERS[0] / "analysis.py"
            edited.write_bytes(edited.read_bytes().upper())
            before = snapshot(root)
            plan = init_project("demo", workspace_path=root, dry_run=True)
            self.assertEqual(count_items(plan, "conflict"), (1, 0))
            self.assertEqual(count_items(plan, "already_present"), (2, 1))
            with self.assertRaises(ProjectInitializationError):
                init_project("demo", workspace_path=root)
            self.assertEqual(snapshot(root), before)

    def test_hashing_is_skipped_when_sizes_differ(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = self.make_project(root)
            (project / "submissions" / STUDENT_FOLDERS[0]).mkdir(parents=True)
            (project / "submissions" / STUDENT_FOLDERS[0] / "analysis.py").write_text("x")
            with patch("ai_grader.project_initialization.source_hashes",
                       side_effect=AssertionError("hashed despite size mismatch")):
                plan = init_project("demo", workspace_path=root, dry_run=True)
            self.assertEqual(count_items(plan, "conflict"), (1, 0))

    def test_unreadable_file_during_comparison_stops_without_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = self.make_project(root)
            init_project("demo", workspace_path=root)
            before = snapshot(root)
            real_open = Path.open

            def locked_open(path, *args, **kwargs):
                if path.name == "analysis.py":
                    raise PermissionError(13, "Access is denied", str(path))
                return real_open(path, *args, **kwargs)

            with patch.object(Path, "open", locked_open), \
                 self.assertRaises(ProjectInitializationError) as raised:
                init_project("demo", workspace_path=root)
            message = str(raised.exception)
            self.assertIn("Access is denied", message)
            self.assertIn("Nothing was changed", message)
            for text in STUDENT_STRINGS:
                self.assertNotIn(text, message)
            self.assertEqual(snapshot(root), before)

    def test_zip_clashing_with_different_submissions_stops_without_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = self.make_project(root)
            (project / "submissions" / STUDENT_FOLDERS[0]).mkdir(parents=True)
            (project / "submissions" / STUDENT_FOLDERS[0] / "old.py").write_text("older")
            before = snapshot(root)
            with self.assertRaises(ProjectInitializationError) as raised:
                init_project("demo", workspace_path=root)
            self.assertIn("1 submission folder, 0 index.html", str(raised.exception))
            self.assertEqual(snapshot(root), before)

    def test_unsafe_zip_path_stops_without_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "projects" / "demo"
            project.mkdir(parents=True)
            with zipfile.ZipFile(project / ZIP_NAME, "w") as archive:
                archive.writestr(f"{STUDENT_FOLDERS[0]}/../../escaped.txt", "x")
            before = snapshot(root)
            with self.assertRaises(ProjectInitializationError) as raised:
                init_project("demo", workspace_path=root)
            self.assertIn("unsafe", str(raised.exception))
            self.assertEqual(snapshot(root), before)
            self.assertFalse((root / "escaped.txt").exists())

    def test_password_protected_zip_stops_without_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_project(root)
            real_infolist = zipfile.ZipFile.infolist

            def encrypted_infolist(archive):
                infos = real_infolist(archive)
                for info in infos:
                    info.flag_bits |= 0x1
                return infos

            before = snapshot(root)
            with patch.object(zipfile.ZipFile, "infolist", encrypted_infolist), \
                 self.assertRaises(ProjectInitializationError) as raised:
                init_project("demo", workspace_path=root)
            self.assertIn("password-protected", str(raised.exception))
            self.assertEqual(snapshot(root), before)

    def test_non_brightspace_zip_is_left_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "projects" / "demo"
            project.mkdir(parents=True)
            with zipfile.ZipFile(project / "datasets.zip", "w") as archive:
                archive.writestr("speed_data.csv", "time_s,speed_kmh\n0,10\n")
            code, output = run_cli(root, "demo")
            self.assertEqual(code, 0, output)
            self.assertEqual(list((project / "submissions").iterdir()), [])
            self.assertIn("  - datasets.zip", output)
            self.assertNotIn("Extracted from", output)

    def test_not_enough_disk_space_stops_without_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_project(root)
            before = snapshot(root)
            with patch("shutil.disk_usage", return_value=shutil._ntuple_diskusage(1, 1, 0)), \
                 self.assertRaises(ProjectInitializationError) as raised:
                init_project("demo", workspace_path=root)
            self.assertIn("Not enough free disk space", str(raised.exception))
            self.assertEqual(snapshot(root), before)

    def test_failed_extraction_cleans_up_and_rerun_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = self.make_project(root)
            real_extract = zipfile.ZipFile.extract
            calls = []

            def flaky_extract(archive, member, path=None, pwd=None):
                calls.append(member)
                if len(calls) == 3:
                    raise PermissionError(13, "Access is denied", "somewhere")
                return real_extract(archive, member, path, pwd)

            with patch.object(zipfile.ZipFile, "extract", flaky_extract), \
                 self.assertRaises(ProjectInitializationError) as raised:
                init_project("demo", workspace_path=root)
            message = str(raised.exception)
            self.assertIn("after placing 1 of 4", message)
            for text in STUDENT_STRINGS:
                self.assertNotIn(text, message)
            self.assertEqual(staging_folders(project), [])

            plan = init_project("demo", workspace_path=root)
            self.assertEqual(count_items(plan, "already_present"), (1, 0))
            self.assert_extracted(project)
            self.assertEqual(staging_folders(project), [])


if __name__ == "__main__":
    unittest.main()
