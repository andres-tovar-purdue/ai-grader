import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_grader.cli import main
from ai_grader.spec_generation import (
    InvalidSpecResponseError, derived_presence_deliverables, fill_presence_patterns,
    generate_grading_spec, presence_patterns_from, strip_schema_metadata,
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


class SpecProjectBase(unittest.TestCase):
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

    def assert_saved_and_rejected(self, output_text):
        with self.assertRaises(InvalidSpecResponseError) as raised:
            self.generate(output_text)
        error = raised.exception
        self.assertEqual(self.invalid_responses(), [error.response_path])
        self.assertEqual(error.response_path.read_text(encoding="utf-8"), output_text)
        self.assertEqual(self.specs_written(), [])
        return error


class SpecGenerationTests(SpecProjectBase):
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


def spec_with_presence_rule(file_patterns=("username_pr01_notebook.ipynb",)):
    """VALID_SPEC with a file_presence rule on the notebook deliverable, which
    has file_patterns but no presence_patterns (what gpt-5.5 produced)."""
    spec = json.loads(json.dumps(VALID_SPEC))
    deliverable = next(item for item in spec["deliverables"]
                       if item["deliverable_id"] == "notebook_ipynb")
    deliverable.pop("presence_patterns", None)
    deliverable["file_patterns"] = list(file_patterns)
    criterion = spec["tasks"][0]["criteria"][0]
    rule = criterion["deduction_rules"][0]
    criterion["deduction_rules"].append({
        "rule_id": "t1_c1_missing_notebook",
        "condition": "The notebook is missing entirely.",
        "deduction_points": rule["deduction_points"],
        "feedback_template": "Submit the notebook.",
        "basis": "file_presence",
        "deliverable_id": "notebook_ipynb",
    })
    return spec


class PresencePatternTests(SpecProjectBase):
    def test_missing_presence_patterns_are_derived_from_file_patterns(self):
        (output_path, spec), _ = self.generate(json.dumps(spec_with_presence_rule()))
        deliverable = next(item for item in spec["deliverables"]
                           if item["deliverable_id"] == "notebook_ipynb")
        self.assertEqual(deliverable["presence_patterns"], ["*_pr01_notebook.ipynb"])
        self.assertIn("derived by AI Grader", deliverable["notes"])
        self.assertEqual(derived_presence_deliverables(spec), ["notebook_ipynb"])
        others = [item for item in spec["deliverables"]
                  if item["deliverable_id"] != "notebook_ipynb"]
        self.assertFalse(any(item.get("presence_patterns") for item in others))
        self.assertEqual(json.loads(output_path.read_text(encoding="utf-8")), spec)

    def test_patterns_match_anonymized_names(self):
        import fnmatch
        patterns = presence_patterns_from(["username_pr02_notebook.ipynb",
                                           "figures/USERNAME_pr02_validation.png"])
        self.assertEqual(patterns, ["*_pr02_notebook.ipynb", "*_pr02_validation.png"])
        for name in ["artifact_003_student_pr02_notebook.ipynb",
                     "x.zip_contents/artifact_010_Student_PR02_Notebook.ipynb"]:
            self.assertTrue(fnmatch.fnmatchcase(name.lower(), patterns[0].lower()), name)

    def test_existing_presence_patterns_are_kept(self):
        spec = spec_with_presence_rule()
        next(item for item in spec["deliverables"]
             if item["deliverable_id"] == "notebook_ipynb")["presence_patterns"] = ["*.ipynb"]
        self.assertEqual(fill_presence_patterns(spec), [])

    def test_without_file_patterns_the_rule_is_still_rejected(self):
        spec = spec_with_presence_rule(file_patterns=())
        error = self.assert_saved_and_rejected(json.dumps(spec))
        self.assertIn("File presence rules require deliverable presence_patterns.",
                      error.messages)

    def test_prompt_explains_presence_patterns(self):
        _, responses = self.generate(json.dumps(VALID_SPEC))
        self.assertIn("presence_patterns", responses.calls[0]["input"])
        self.assertIn("*_pr02_notebook.ipynb", responses.calls[0]["input"])


class SavedResponseTests(SpecProjectBase):
    def test_saved_response_is_used_without_a_model_request(self):
        self.grader.mkdir()
        saved = self.grader / "invalid_spec_response_20261109_144641_240110.json"
        saved.write_text(json.dumps(spec_with_presence_rule()), encoding="utf-8")
        with patch("ai_grader.spec_generation.get_client",
                   side_effect=AssertionError("no model request expected")):
            output_path, spec = generate_grading_spec(self.project, SCHEMA_PATH,
                                                      response_path=saved)
        self.assertEqual(output_path.name, "grading_spec_v001.json")
        self.assertEqual(spec["status"], "draft")
        self.assertTrue(saved.is_file())

    def test_invalid_saved_response_is_not_saved_again(self):
        self.grader.mkdir()
        saved = self.grader / "invalid_spec_response_20261109_000000_000000.json"
        saved.write_text("[]", encoding="utf-8")
        with self.assertRaises(InvalidSpecResponseError) as raised:
            generate_grading_spec(self.project, SCHEMA_PATH, response_path=saved)
        self.assertFalse(raised.exception.saved_again)
        self.assertEqual(self.invalid_responses(), [saved])
        self.assertEqual(self.specs_written(), [])

    def test_cli_from_response(self):
        self.grader.mkdir()
        saved = self.grader / "invalid_spec_response_20261109_144641_240110.json"
        saved.write_text(json.dumps(spec_with_presence_rule()), encoding="utf-8")
        output = io.StringIO()
        with patch("ai_grader.spec_generation.get_client",
                   side_effect=AssertionError("no model request expected")), \
             patch("sys.argv", ["ai_grader", "generate-spec", str(self.project),
                                "--from-response", str(saved)]), \
             patch("sys.stdout", output):
            main()
        text = output.getvalue()
        self.assertIn("No AI request: using the saved response", text)
        self.assertNotIn("Model:", text)
        self.assertIn("File-presence patterns filled in by AI Grader for 1 deliverable(s): "
                      "notebook_ipynb", text)
        self.assertIn("grading_spec_v001.json", text)

    def test_cli_missing_saved_response(self):
        output = io.StringIO()
        with patch("sys.argv", ["ai_grader", "generate-spec", str(self.project),
                                "--from-response", str(self.grader / "nope.json")]), \
             patch("sys.stdout", output), self.assertRaises(SystemExit):
            main()
        self.assertIn("Saved response not found", output.getvalue())


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
