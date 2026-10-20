import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("install", REPOSITORY_ROOT / "install.py")
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)

FAKE_KEY = "sk-test-not-a-real-key"


def answers(*values):
    """An input() replacement that returns values in order, then raises EOFError."""

    remaining = list(values)

    def ask(prompt=""):
        if not remaining:
            raise EOFError
        return remaining.pop(0)

    return ask


class PythonVersionTests(unittest.TestCase):
    def test_accepts_supported_versions(self):
        install.check_python((3, 10, 0))
        install.check_python((3, 13, 5))

    def test_rejects_old_python(self):
        with self.assertRaises(install.SetupError) as raised:
            install.check_python((3, 9, 18))
        self.assertIn("3.9.18", str(raised.exception))


class VenvTests(unittest.TestCase):
    def test_venv_python_path(self):
        venv = Path("repo/.venv")
        self.assertEqual(install.venv_python(venv, windows=True),
                         venv / "Scripts" / "python.exe")
        self.assertEqual(install.venv_python(venv, windows=False),
                         venv / "bin" / "python")

    def test_creates_missing_venv(self):
        with tempfile.TemporaryDirectory() as directory:
            venv = Path(directory) / ".venv"
            created = []
            self.assertTrue(install.ensure_venv(venv, create=created.append))
            self.assertEqual(created, [venv])

    def test_keeps_existing_venv(self):
        with tempfile.TemporaryDirectory() as directory:
            venv = Path(directory) / ".venv"
            python = install.venv_python(venv)
            python.parent.mkdir(parents=True)
            python.write_text("")
            self.assertFalse(install.ensure_venv(venv, create=self.fail))

    def test_stops_on_broken_venv(self):
        with tempfile.TemporaryDirectory() as directory:
            venv = Path(directory) / ".venv"
            venv.mkdir()
            with self.assertRaises(install.SetupError):
                install.ensure_venv(venv, create=self.fail)

    def test_install_package_runs_editable_pip_install(self):
        calls = []

        def run(command):
            calls.append(command)
            return SimpleNamespace(returncode=0)

        install.install_package(Path("py"), Path("repo"), run=run)
        self.assertEqual(calls, [["py", "-m", "pip", "install", "-e", "repo"]])

    def test_install_package_failure_stops_setup(self):
        with self.assertRaises(install.SetupError):
            install.install_package(Path("py"), Path("repo"),
                                    run=lambda command: SimpleNamespace(returncode=1))


class ProjectsFolderTests(unittest.TestCase):
    def test_existing_folder_is_not_asked_about(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                install.ensure_projects_folder(Path(directory), ask=answers()),
                "exists")

    def test_creates_folder_when_confirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            projects = Path(directory) / "projects"
            for reply in ("", "y", "YES"):
                with self.subTest(reply=reply):
                    if projects.exists():
                        projects.rmdir()
                    self.assertEqual(
                        install.ensure_projects_folder(projects, ask=answers(reply)),
                        "created")
                    self.assertTrue(projects.is_dir())

    def test_skips_folder_when_declined_or_no_input(self):
        with tempfile.TemporaryDirectory() as directory:
            projects = Path(directory) / "projects"
            for ask in (answers("n"), answers()):
                self.assertEqual(install.ensure_projects_folder(projects, ask=ask),
                                 "skipped")
            self.assertFalse(projects.exists())


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.env = root / ".env"
        self.example = root / ".env.example"
        self.example.write_text(
            (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8"),
            encoding="utf-8")

    def create(self, ask, ask_secret):
        with patch("sys.stdout", new=io.StringIO()) as output:
            created = install.ensure_env_file(self.env, self.example,
                                              ask=ask, ask_secret=ask_secret)
        return created, output.getvalue()

    def test_example_has_placeholders_and_planned_anthropic_lines(self):
        text = self.example.read_text(encoding="utf-8")
        self.assertIn("\nOPENAI_API_KEY=\n", text)
        self.assertIn("\nOPENAI_MODEL=\n", text)
        self.assertIn("planned, not yet supported", text)
        self.assertIn("# ANTHROPIC_API_KEY=", text)

    def test_creates_env_from_example(self):
        created, output = self.create(answers("test-model", "Test Instructor"),
                                      answers(FAKE_KEY))
        self.assertTrue(created)
        text = self.env.read_text(encoding="utf-8")
        self.assertIn(f"\nOPENAI_API_KEY={FAKE_KEY}\n", text)
        self.assertIn("\nOPENAI_MODEL=test-model\n", text)
        self.assertIn("\nGRADER_INSTRUCTOR_NAME=Test Instructor\n", text)
        self.assertIn("# ANTHROPIC_API_KEY=\n", text)
        self.assertNotIn(FAKE_KEY, output)

    def test_model_is_required(self):
        created, _ = self.create(answers("", "  ", "test-model", ""),
                                 answers(FAKE_KEY))
        self.assertTrue(created)
        text = self.env.read_text(encoding="utf-8")
        self.assertIn("\nOPENAI_MODEL=test-model\n", text)
        self.assertIn("\nGRADER_INSTRUCTOR_NAME=\n", text)

    def test_key_is_required(self):
        created, _ = self.create(answers("test-model", ""), answers("", FAKE_KEY))
        self.assertTrue(created)
        self.assertIn(f"OPENAI_API_KEY={FAKE_KEY}",
                      self.env.read_text(encoding="utf-8"))

    def test_no_input_stops_without_writing(self):
        with self.assertRaises(install.SetupError):
            self.create(answers(), answers(FAKE_KEY))
        self.assertFalse(self.env.exists())

    def test_existing_env_is_never_overwritten_or_asked_about(self):
        self.env.write_text("OPENAI_API_KEY=keep-me\n", encoding="utf-8")
        created, _ = self.create(answers(), answers())
        self.assertFalse(created)
        self.assertEqual(self.env.read_text(encoding="utf-8"),
                         "OPENAI_API_KEY=keep-me\n")

    def test_fill_template_only_changes_named_keys(self):
        template = "# A=\nA=\nB=\nC = \n"
        self.assertEqual(install.fill_env_template(template, {"A": "1", "C": "3"}),
                         "# A=\nA=1\nB=\nC=3\n")


class CheckSetupTests(unittest.TestCase):
    def test_runs_check_setup_with_venv_python_from_workspace(self):
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0)

        self.assertTrue(install.run_check_setup(Path("venv-python"), Path("workspace"),
                                                run=run))
        self.assertEqual(calls, [(["venv-python", "-m", "ai_grader", "check-setup"],
                                  {"cwd": "workspace"})])

    def test_failed_check_returns_false(self):
        self.assertFalse(install.run_check_setup(
            Path("p"), Path("w"), run=lambda command, **kwargs: SimpleNamespace(returncode=1)))


class MainTests(unittest.TestCase):
    """Run main() twice against a temporary repository with every side effect faked."""

    def test_rerun_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "ai-grader"
            repo.mkdir()
            (repo / ".env.example").write_text("OPENAI_API_KEY=\nOPENAI_MODEL=\n",
                                                encoding="utf-8")
            venv = repo / ".venv"

            def fake_create(path):
                python = install.venv_python(path)
                python.parent.mkdir(parents=True)
                python.write_text("")

            calls = []

            def fake_run(command, **kwargs):
                calls.append((command, kwargs))
                return SimpleNamespace(returncode=0)

            paths = {
                "REPOSITORY_ROOT": repo,
                "VENV_PATH": venv,
                "ENV_PATH": repo / ".env",
                "ENV_EXAMPLE_PATH": repo / ".env.example",
                "PROJECTS_PATH": repo.parent / "projects",
            }
            replies = answers("y", "test-model", "")
            with patch.multiple(install, **paths), \
                 patch.object(install.venv, "create", side_effect=lambda p, **k: fake_create(p)), \
                 patch.object(install.subprocess, "run", side_effect=fake_run), \
                 patch("builtins.input", side_effect=replies), \
                 patch.object(install.getpass, "getpass", side_effect=answers(FAKE_KEY)), \
                 patch("sys.stdout", new=io.StringIO()) as first:
                self.assertEqual(install.main(), 0)

            self.assertTrue((repo.parent / "projects").is_dir())
            env_text = (repo / ".env").read_text(encoding="utf-8")
            self.assertIn("OPENAI_MODEL=test-model", env_text)
            self.assertNotIn(FAKE_KEY, first.getvalue())
            self.assertIn("Activate.ps1", first.getvalue())
            self.assertIn("Next step:", first.getvalue())
            self.assertIn("init-project PROJECT_ID", first.getvalue())
            self.assertIn((["check-setup"], {"cwd": str(repo.parent)}),
                          [(command[-1:], kwargs) for command, kwargs in calls])

            # Second run: nothing is asked and nothing is replaced.
            with patch.multiple(install, **paths), \
                 patch.object(install.venv, "create", side_effect=AssertionError), \
                 patch.object(install.subprocess, "run", side_effect=fake_run), \
                 patch("builtins.input", side_effect=AssertionError), \
                 patch.object(install.getpass, "getpass", side_effect=AssertionError), \
                 patch("sys.stdout", new=io.StringIO()) as second:
                self.assertEqual(install.main(), 0)

            self.assertEqual((repo / ".env").read_text(encoding="utf-8"), env_text)
            self.assertIn("Keeping existing", second.getvalue())

    def test_failed_check_prints_fix_steps_not_next_step(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "ai-grader"
            venv = repo / ".venv"
            python = install.venv_python(venv)
            python.parent.mkdir(parents=True)
            python.write_text("")
            (repo / ".env").write_text("OPENAI_API_KEY=bad\n", encoding="utf-8")
            (repo.parent / "projects").mkdir()
            calls = []

            def fake_run(command, **kwargs):
                calls.append(command)
                if "pip" in command:
                    return SimpleNamespace(returncode=0)
                return SimpleNamespace(returncode=1)

            with patch.multiple(install, REPOSITORY_ROOT=repo, VENV_PATH=venv,
                                ENV_PATH=repo / ".env",
                                ENV_EXAMPLE_PATH=repo / ".env.example",
                                PROJECTS_PATH=repo.parent / "projects"), \
                 patch.object(install.subprocess, "run", side_effect=fake_run), \
                 patch("builtins.input", side_effect=AssertionError), \
                 patch("sys.stdout", new=io.StringIO()) as output:
                self.assertEqual(install.main(), 1)

            text = output.getvalue()
            self.assertEqual(calls[-1][-2:], ["ai_grader", "check-setup"])
            self.assertIn("run this script again", text)
            self.assertIn(str(repo / ".env"), text)
            self.assertIn("python install.py", text)
            self.assertNotIn("Next step:", text)
            self.assertNotIn("init-project", text)
            self.assertEqual((repo / ".env").read_text(encoding="utf-8"),
                             "OPENAI_API_KEY=bad\n")


if __name__ == "__main__":
    unittest.main()
