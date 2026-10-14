import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_grader.grading import (
    DEFAULT_GRADING_MODEL,
    build_student_input,
    grade_submissions,
    load_grading_inputs,
)


class FakeResponses:
    def __init__(self, output, usage=None):
        self.output = output
        self.usage = usage
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_text=json.dumps(self.output),
            usage=self.usage,
        )


def model_response():
    return {
        "student_id": "Student_001",
        "criteria": [{
            "criterion_id": "T1_C1",
            "agent_score": 0.75,
            "deductions": [{
                "rule_id": "minor_issue_rule",
                "points": 0.25,
                "reason": "Minor issue.",
                "deduction_type": "demonstrated_technical_error",
                "cause_id": "minor_issue",
                "independent_requirement": True,
            }],
            "justification": "The source provides most required evidence.",
            "evidence": [{
                "artifact_path": "artifact_001_student_project2_matlab.m",
                "evidence_type": "source_code",
                "location": "line 1",
            }],
            "evidence_state": "demonstrated_error",
            "confidence": "high",
            "review_required": False,
            "review_reasons": [],
        }],
        "task_feedback": [{"task_id": "T1", "feedback": "Mostly complete."}],
        "review_required": False,
        "review_reasons": [],
    }


class GradingTests(unittest.TestCase):
    def make_project(self, root, status="approved", extra_files=None):
        grader = root / "grader"
        student_root = grader / "anonymized_submissions" / "Student_001"
        student_root.mkdir(parents=True)
        code_path = student_root / "artifact_001_student_project2_matlab.m"
        code_content = "disp('safe anonymized code')\n"
        code_path.write_text(code_content, encoding="utf-8")

        files = [{
            "path": code_path.name,
            "file_type": "text",
            "extension": ".m",
            "size_bytes": len(code_content.encode("utf-8")),
            "content": code_content,
        }]
        for item in extra_files or []:
            path = student_root / item["path"]
            path.write_bytes(item["bytes"])
            files.append({
                "path": item["path"],
                "file_type": item["file_type"],
                "extension": path.suffix,
                "size_bytes": len(item["bytes"]),
            })

        spec = {
            "schema_version": "1.0",
            "spec_version": "0.1",
            "status": status,
            "project": {
                "project_id": "demo",
                "title": "Demo",
                "total_points": 1,
            },
            "sources": [{
                "source_id": "instructions",
                "source_type": "project_instructions",
                "path": "project/project_instructions.md",
            }],
            "deliverables": [],
            "tasks": [{
                "task_id": "T1",
                "title": "Task 1",
                "max_points": 1,
                "criteria": [{
                    "criterion_id": "T1_C1",
                    "description": "Criterion",
                    "requirement_type": "technical_correctness",
                    "max_points": 1,
                    "full_credit_condition": "Requirement is satisfied.",
                    "deduction_rules": [{"rule_id": "minor_issue_rule", "condition": "Minor issue.", "deduction_points": 0.25}],
                }],
            }],
            "known_ambiguities": [],
            "approval": {
                "approved_by": "Instructor",
                "approved_at": "2026-08-10T12:00:00-04:00",
            },
        }
        (grader / "grading_spec_v001.json").write_text(
            json.dumps(spec), encoding="utf-8"
        )
        manifest = {
            "manifest_version": "2.0",
            "anonymization": {
                "status": "validated",
                "artifact_root": "grader/anonymized_submissions",
                "known_text_identities_redacted": True,
                "image_pixels_redacted": False,
            },
            "submission_count": 1,
            "unparsed_folder_count": 0,
            "unparsed_folders": [],
            "submissions": [{
                "student_id": "Student_001",
                "attempt_count": 1,
                "selected_attempt": 1,
                "file_count": len(files),
                "files": files,
                "review_flags": [],
            }],
        }
        (grader / "submission_manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (grader / "student_map.json").write_text(
            "IDENTITY TRAP", encoding="utf-8"
        )
        original = root / "submissions" / "identity-bearing-folder"
        original.mkdir(parents=True)
        (original / "secret.txt").write_text(
            "ORIGINAL IDENTITY SECRET", encoding="utf-8"
        )
        return spec, manifest, student_root

    def test_grades_without_reading_identity_files_and_versions_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_project(root)
            responses = FakeResponses(model_response())
            client = SimpleNamespace(responses=responses)
            original_open = Path.open

            def guarded_open(path, *args, **kwargs):
                if path.name == "student_map.json":
                    raise AssertionError("student_map.json was accessed")
                return original_open(path, *args, **kwargs)

            with patch.object(Path, "open", guarded_open):
                run_one, json_path, csv_path, run = grade_submissions(root, client)
                run_two, _, _, _ = grade_submissions(root, client)

            self.assertEqual(run_one.name, "run_v001")
            self.assertEqual(run_two.name, "run_v002")
            self.assertEqual(len(responses.calls), 2)
            request_text = json.dumps(responses.calls)
            self.assertNotIn("ORIGINAL IDENTITY SECRET", request_text)
            self.assertNotIn("identity-bearing-folder", request_text)
            self.assertEqual(run["results"][0]["total_agent_score"], 1.0)
            self.assertEqual(run["results"][0]["raw_total_before_rounding"], 0.75)
            self.assertEqual(run["results"][0]["rounded_task_total"], 1.0)
            self.assertEqual(
                run["results"][0]["rounding_policy"]["identifier"],
                "generous-v1",
            )
            self.assertIsNone(run["results"][0]["total_instructor_score"])
            self.assertTrue(json_path.is_file())
            rows = list(csv.DictReader(io.StringIO(csv_path.read_text(encoding="utf-8"))))
            self.assertEqual(rows[0]["total_instructor_score"], "")
            self.assertEqual(float(rows[0]["maximum_score"]), 1)
            self.assertEqual(float(rows[0]["task_1_score"]), 1)
            self.assertEqual(float(rows[0]["task_1_max_points"]), 1)
            self.assertEqual(float(rows[0]["raw_total_before_rounding"]), .75)

    def test_default_model_and_usage_are_recorded_without_real_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_project(root)
            usage = {
                "input_tokens": 100,
                "input_tokens_details": {"cached_tokens": 25},
                "output_tokens": 40,
                "output_tokens_details": {"reasoning_tokens": 10},
                "total_tokens": 140,
            }
            responses = FakeResponses(model_response(), usage=usage)
            with patch.dict("os.environ", {"OPENAI_MODEL": ""}):
                run_path, json_path, _, run = grade_submissions(
                    root,
                    SimpleNamespace(responses=responses),
                )
            self.assertEqual(run_path.name, "run_v001")
            self.assertEqual(responses.calls[0]["model"], DEFAULT_GRADING_MODEL)
            self.assertEqual(run["model"], "gpt-5.4-mini")
            self.assertEqual(run["api_usage"]["totals"]["input_tokens"], 100)
            self.assertEqual(
                run["api_usage"]["totals"]["cached_input_tokens"],
                25,
            )
            self.assertEqual(run["api_usage"]["totals"]["reasoning_tokens"], 10)
            saved = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["model"], "gpt-5.4-mini")
            self.assertEqual(
                saved["results"][0]["api_usage"]["total_tokens"],
                140,
            )
            self.assertIsNone(saved["estimated_cost"])
            self.assertIsNone(saved["pricing_assumptions"])

    def test_explicit_model_override_is_used_without_real_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_project(root)
            responses = FakeResponses(model_response())
            _, _, _, run = grade_submissions(
                root,
                SimpleNamespace(responses=responses),
                model="explicit-test-model",
            )
            self.assertEqual(responses.calls[0]["model"], "explicit-test-model")
            self.assertEqual(run["model"], "explicit-test-model")

    def test_environment_model_override_is_preserved_without_real_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_project(root)
            responses = FakeResponses(model_response())
            with patch.dict("os.environ", {"OPENAI_MODEL": "environment-model"}):
                _, _, _, run = grade_submissions(
                    root,
                    SimpleNamespace(responses=responses),
                )
            self.assertEqual(responses.calls[0]["model"], "environment-model")
            self.assertEqual(run["model"], "environment-model")

    def test_refuses_draft_before_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_project(root, status="draft")
            responses = FakeResponses(model_response())

            with self.assertRaisesRegex(RuntimeError, "instructor-approved"):
                grade_submissions(root, SimpleNamespace(responses=responses))

            self.assertEqual(responses.calls, [])

    def test_rejects_identity_bearing_manifest_field(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_project(root)
            manifest_path = root / "grader" / "submission_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["submissions"][0]["username"] = "forbidden"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "forbidden identity-bearing"):
                load_grading_inputs(root)

    def test_images_are_sent_and_slx_receives_structural_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            slx_stream = io.BytesIO()
            with zipfile.ZipFile(slx_stream, "w") as package:
                package.writestr("[Content_Types].xml", "<Types/>")
                package.writestr("simulink/blockdiagram.xml", "<ModelInformation/>")
            spec, manifest, student_root = self.make_project(root, extra_files=[
                {
                    "path": "artifact_002_student_plot.png",
                    "file_type": "image",
                    "bytes": b"png-bytes",
                },
                {
                    "path": "artifact_003_student_model.slx",
                    "file_type": "model",
                    "bytes": slx_stream.getvalue(),
                },
            ])
            api_input = build_student_input(
                spec, manifest["submissions"][0], student_root, "instructions"
            )

            content = api_input[0]["content"]
            self.assertTrue(any(item["type"] == "input_image" for item in content))
            text = "\n".join(
                item["text"] for item in content if item["type"] == "input_text"
            )
            self.assertIn("structural_artifact_preflight", text)
            self.assertIn("apparently_valid_not_technically_inspected", text)
            self.assertIn("Do not infer model blocks", text)
            self.assertNotIn("<ModelInformation/>", text)

    def test_duplicate_slx_deliverables_are_flagged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            slx_stream = io.BytesIO()
            with zipfile.ZipFile(slx_stream, "w") as package:
                package.writestr("[Content_Types].xml", "<Types/>")
                package.writestr("simulink/blockdiagram.xml", "<ModelInformation/>")
            payload = slx_stream.getvalue()
            spec, manifest, student_root = self.make_project(root, extra_files=[
                {"path": "artifact_002_dynamic.slx", "file_type": "model", "bytes": payload},
                {"path": "artifact_003_control.slx", "file_type": "model", "bytes": payload},
            ])
            api_input = build_student_input(
                spec, manifest["submissions"][0], student_root, "instructions"
            )
            text = "\n".join(
                item["text"] for item in api_input[0]["content"]
                if item["type"] == "input_text"
            )
            self.assertIn("wrong_or_mislabeled_deliverable", text)
            self.assertIn("byte-identical", text)


if __name__ == "__main__":
    unittest.main()
