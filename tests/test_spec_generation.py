import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_grader.cli import main
from ai_grader.spec_generation import (
    InvalidSpecResponseError, generate_grading_spec, strip_schema_metadata,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "grading_spec.schema.json"
VALID_SPEC = json.loads(
    (ROOT / "tests" / "fixtures" / "mspe_49600_pr01_corrected_draft.json")
    .read_text(encoding="utf-8"))


class FakeResponses:
    def __init__(self, output_text):
        self.output_text = output_text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=self.output_text)


class SpecGenerationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        # Resolved, so a Windows short-name TEMP (e.g. RUNNER~1 on CI) compares
        # equal to the resolved paths generate_grading_spec returns.
        self.project = Path(self.directory.name).resolve() / "demo"
        (self.project / "project").mkdir(parents=True)
        (self.project / "project" / "instructions.md").write_text(
            "# Synthetic assignment\nWrite a script.\n", encoding="utf-8")
        self.grader = self.project / "grader"

    def generate(self, output_text):
        responses = FakeResponses(output_text)
        client = SimpleNamespace(responses=responses)
        with patch("ai_grader.spec_generation.get_client", return_value=client):
            result = generate_grading_spec(self.project, SCHEMA_PATH)
        return result, responses

    def specs_written(self):
        return sorted(self.grader.glob("grading_spec_v*.json"))

    def invalid_responses(self):
        return sorted(self.grader.glob("invalid_spec_response_*.json"))

    def test_prompt_says_not_to_include_schema_keywords(self):
        _, responses = self.generate(json.dumps(VALID_SPEC))
        self.assertIn("$schema, $id or $comment", responses.calls[0]["input"])

    def test_schema_metadata_keys_are_dropped_before_validation(self):
        spec = {"$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "spec", "$comment": "copied", **VALID_SPEC}
        (output_path, saved), _ = self.generate(json.dumps(spec))
        self.assertEqual(output_path.name, "grading_spec_v001.json")
        written = json.loads(output_path.read_text(encoding="utf-8"))
        for key in ("$schema", "$id", "$comment"):
            self.assertNotIn(key, saved)
            self.assertNotIn(key, written)
        self.assertEqual(written["status"], "draft")
        self.assertEqual(self.invalid_responses(), [])

    def test_only_top_level_metadata_is_dropped(self):
        spec = {"$schema": "x", "project": {"$comment": "kept"}}
        self.assertEqual(strip_schema_metadata(spec), {"project": {"$comment": "kept"}})

    def assert_saved_and_rejected(self, output_text):
        with self.assertRaises(InvalidSpecResponseError) as raised:
            self.generate(output_text)
        error = raised.exception
        self.assertEqual(self.invalid_responses(), [error.response_path])
        self.assertEqual(error.response_path.read_text(encoding="utf-8"), output_text)
        self.assertEqual(self.specs_written(), [])
        return error

    def test_invalid_spec_saves_raw_response(self):
        spec = {**VALID_SPEC, "unexpected_field": True}
        error = self.assert_saved_and_rejected(json.dumps(spec))
        self.assertTrue(any("unexpected_field" in message for message in error.messages))
        self.assertRegex(error.response_path.name,
                         r"^invalid_spec_response_\d{8}_\d{6}_\d{6}\.json$")

    def test_missing_tasks_is_a_validation_message_not_a_key_error(self):
        spec = dict(VALID_SPEC)
        del spec["tasks"]
        error = self.assert_saved_and_rejected(json.dumps(spec))
        self.assertTrue(any("'tasks' is a required property" in message
                            for message in error.messages), error.messages)

    def test_inconsistent_scoring_saves_raw_response(self):
        spec = json.loads(json.dumps(VALID_SPEC))
        spec["project"]["total_points"] += 1
        error = self.assert_saved_and_rejected(json.dumps(spec))
        self.assertTrue(any("project total_points" in message for message in error.messages))

    def test_non_json_response_saves_raw_response(self):
        error = self.assert_saved_and_rejected("Here is the spec: {not json")
        self.assertIn("not valid JSON", error.messages[0])

    def test_non_object_response_saves_raw_response(self):
        error = self.assert_saved_and_rejected("[]")
        self.assertEqual(error.messages, ["The response is not a JSON object."])


class GenerateSpecCommandTests(unittest.TestCase):
    def test_invalid_spec_prints_messages_and_path_without_traceback(self):
        response_path = Path("grader") / "invalid_spec_response_20261019_120000_000000.json"
        error = InvalidSpecResponseError(
            ["<root>: Additional properties are not allowed ('$ref' was unexpected)",
             "tasks.0: 'criteria' is a required property"],
            response_path)
        output = io.StringIO()
        with patch("ai_grader.cli.generate_grading_spec", side_effect=error), \
             patch("sys.argv", ["ai_grader", "generate-spec", "demo"]), \
             patch("sys.stdout", output):
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 1)
        text = output.getvalue()
        self.assertIn("failed validation", text)
        self.assertIn("No draft specification was written.", text)
        self.assertIn("  - <root>: Additional properties are not allowed", text)
        self.assertIn("  - tasks.0: 'criteria' is a required property", text)
        self.assertIn(str(response_path), text)
        self.assertNotIn("Traceback", text)


if __name__ == "__main__":
    unittest.main()
