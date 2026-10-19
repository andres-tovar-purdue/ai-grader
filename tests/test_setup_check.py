import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_grader import setup_check
from ai_grader.cli import main


FAKE_KEY = "sk-test-not-a-real-key"


class SetupCheckTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.parent = Path(self.directory.name)
        self.repo = self.parent / "ai-grader"
        self.repo.mkdir()

    def test_venv(self):
        self.assertFalse(setup_check.check_venv("/usr", "/usr", self.repo)["ok"])
        venv = self.repo / ".venv"
        venv.mkdir()
        result = setup_check.check_venv(venv, "/usr", self.repo)
        self.assertTrue(result["ok"])
        self.assertIn("Active:", result["detail"])
        other = setup_check.check_venv(self.parent / "other", "/usr", self.repo)
        self.assertTrue(other["ok"])
        self.assertIn("not the repository's .venv", other["detail"])

    def test_env_file(self):
        self.assertFalse(setup_check.check_env_file(self.repo)["ok"])
        (self.parent / ".env").write_text("", encoding="utf-8")
        old = setup_check.check_env_file(self.repo)
        self.assertFalse(old["ok"])
        self.assertIn("old location", old["detail"])
        (self.repo / ".env").write_text("", encoding="utf-8")
        self.assertTrue(setup_check.check_env_file(self.repo)["ok"])

    def test_api_key_is_never_shown(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": FAKE_KEY}):
            result = setup_check.check_api_key()
        self.assertTrue(result["ok"])
        self.assertNotIn(FAKE_KEY, result["detail"])
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}):
            self.assertFalse(setup_check.check_api_key()["ok"])

    def test_model(self):
        with patch.dict("os.environ", {"OPENAI_MODEL": "test-model"}):
            self.assertEqual(setup_check.check_model(),
                             {"name": "OPENAI_MODEL", "ok": True, "detail": "test-model"})
        with patch.dict("os.environ", {"OPENAI_MODEL": ""}):
            self.assertFalse(setup_check.check_model()["ok"])

    def test_workspace(self):
        self.assertFalse(setup_check.check_workspace(self.repo, self.parent)["ok"])
        (self.parent / "projects").mkdir()
        here = setup_check.check_workspace(self.repo, self.parent)
        self.assertTrue(here["ok"])
        self.assertNotIn("Run commands from", here["detail"])
        elsewhere = setup_check.check_workspace(self.repo, self.repo)
        self.assertTrue(elsewhere["ok"])
        self.assertIn("Run commands from", elsewhere["detail"])

    def test_api(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}):
            self.assertIn("Skipped", setup_check.check_api(connect=self.fail)["detail"])
        with patch.dict("os.environ", {"OPENAI_API_KEY": FAKE_KEY}):
            self.assertTrue(setup_check.check_api(connect=lambda: "ok")["ok"])

            def refuse():
                raise RuntimeError("401 Unauthorized")

            failed = setup_check.check_api(connect=refuse)
        self.assertFalse(failed["ok"])
        self.assertIn("RuntimeError: 401 Unauthorized", failed["detail"])


class CheckSetupCommandTests(unittest.TestCase):
    def run_command(self, checks, *extra):
        output = io.StringIO()
        with patch("ai_grader.cli.run_setup_checks", return_value=checks) as run, \
             patch("sys.argv", ["ai_grader", "check-setup", *extra]), \
             patch("sys.stdout", output):
            try:
                main()
                code = 0
            except SystemExit as exit:
                code = exit.code
        return run, code, output.getvalue()

    def test_all_ok(self):
        run, code, output = self.run_command(
            [setup_check.check("OPENAI_MODEL", True, "test-model")], "--skip-api")
        run.assert_called_once_with(skip_api=True)
        self.assertEqual(code, 0)
        self.assertIn("[OK     ] OPENAI_MODEL: test-model", output)
        self.assertIn("Setup is complete.", output)

    def test_problem_exits_nonzero(self):
        run, code, output = self.run_command(
            [setup_check.check(".env file", False, "Missing")])
        run.assert_called_once_with(skip_api=False)
        self.assertEqual(code, 1)
        self.assertIn("[PROBLEM] .env file: Missing", output)
        self.assertIn("install.py", output)

    def test_skip_api_makes_no_request(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": FAKE_KEY}):
            checks = setup_check.run_setup_checks(skip_api=True, connect=self.fail)
        self.assertEqual(checks[-1]["name"], "API connection")
        self.assertIn("Skipped", checks[-1]["detail"])


if __name__ == "__main__":
    unittest.main()
