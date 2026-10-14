import csv
import json
import tempfile
import unittest
from pathlib import Path

from ai_grader.finalization import finalize_grading


class FinalizationTests(unittest.TestCase):
    def test_writes_valid_named_outputs_without_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            grader = root / "grader"
            run_path = grader / "grading_runs" / "run_v003"
            run_path.mkdir(parents=True)
            spec = {
                "schema_version": "1.0", "spec_version": "1", "status": "approved",
                "project": {"project_id": "demo", "title": "Demo Assignment", "total_points": 10},
                "sources": [{"source_id": "published", "source_type": "project_instructions", "path": "project/instructions.md"}],
                "deliverables": [], "known_ambiguities": [],
                "tasks": [{"task_id": "T1", "title": "Task One", "max_points": 10,
                           "criteria": [{"criterion_id": "C1", "max_points": 10,
                                         "description": "Work", "requirement_type": "technical_correctness",
                                         "full_credit_condition": "Complete work."}]}],
            }
            (grader / "grading_spec_v001.json").write_text(
                json.dumps(spec), encoding="utf-8"
            )
            (grader / "student_map.json").write_text(json.dumps({
                "Student_001": {"full_name": "Avery Student"},
            }), encoding="utf-8")
            run = {
                "results": [{
                    "student_id": "Student_001",
                    "total_agent_score": 8,
                    "tasks": [{
                        "task_id": "T1",
                        "title": "Task One",
                        "agent_score": 8,
                        "max_points": 10,
                        "feedback": "Strong work; one required label is missing.",
                        "criteria": [{
                            "criterion_id": "C1",
                            "agent_score": 8,
                        }],
                    }],
                }],
            }
            (run_path / "grading_results.json").write_text(
                json.dumps(run), encoding="utf-8"
            )

            output, csv_path, txt_paths, validation = finalize_grading(
                root,
                "run_v003",
                {"Student_001": 9},
                {("Student_001", "C1"): 9},
            )

            self.assertEqual(output.name, "finalization_v001")
            self.assertEqual(len(txt_paths), 1)
            self.assertTrue(all(validation.values()))
            with csv_path.open(encoding="utf-8", newline="") as file:
                row = next(csv.DictReader(file))
            self.assertEqual(row["actual_student_name"], "Avery Student")
            self.assertEqual(float(row["final_instructor_score"]), 9)
            text = txt_paths[0].read_text(encoding="utf-8")
            self.assertEqual(text.count("Final Grade: 9/10"), 2)
            self.assertNotIn("agent", text.casefold())


if __name__ == "__main__":
    unittest.main()
