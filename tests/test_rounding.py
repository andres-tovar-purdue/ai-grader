import unittest

from ai_grader.grading_validation import calculate_grading_result
from ai_grader.rounding import apply_rounding, round_up_to_increment


class RoundingTests(unittest.TestCase):
    def test_task_rounding_examples(self):
        cases = [
            (13.00, 13.0),
            (13.01, 13.5),
            (13.50, 13.5),
            (13.51, 14.0),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(
                    round_up_to_increment(value, 0.5, 15),
                    expected,
                )

    def test_task_rounding_does_not_exceed_maximum(self):
        self.assertEqual(round_up_to_increment(14.99, 0.5, 14.9), 14.9)

    def test_final_rounding_examples(self):
        cases = [
            (89.00, 89),
            (89.01, 90),
            (89.25, 90),
            (89.50, 90),
            (100.0, 100),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                result = apply_rounding(
                    [value],
                    [100],
                    100,
                    "generous-v1",
                )
                self.assertEqual(result["final_rounded_grade"], expected)

    def test_calculation_preserves_criteria_and_records_reproducible_totals(self):
        spec = {
            "project": {"total_points": 20},
            "tasks": [{
                "task_id": "T1",
                "title": "Task",
                "max_points": 20,
                "criteria": [
                    {"criterion_id": "C1", "max_points": 10,
                     "deduction_rules": [{"rule_id": "C1_rule", "deduction_points": 3.99}]},
                    {"criterion_id": "C2", "max_points": 10,
                     "deduction_rules": [{"rule_id": "C2_rule", "deduction_points": 3.0}]},
                ],
            }],
        }
        submission = {"student_id": "Student_001", "files": [], "review_flags": []}
        response = {
            "student_id": "Student_001",
            "criteria": [
                self._criterion("C1", 6.01, 3.99),
                self._criterion("C2", 7.0, 3.0),
            ],
            "task_feedback": [{"task_id": "T1", "feedback": "Feedback."}],
            "review_required": False,
            "review_reasons": [],
        }
        result = calculate_grading_result(response, spec, submission)
        criteria = result["tasks"][0]["criteria"]
        self.assertEqual([item["agent_score"] for item in criteria], [6.01, 7.0])
        self.assertEqual(result["tasks"][0]["raw_task_subtotal"], 13.01)
        self.assertEqual(result["tasks"][0]["rounded_task_subtotal"], 13.5)
        self.assertEqual(result["tasks"][0]["agent_score"], 13.5)
        self.assertEqual(result["raw_total_before_rounding"], 13.01)
        self.assertEqual(result["rounded_task_total"], 13.5)
        self.assertEqual(result["final_rounded_grade"], 14)
        self.assertEqual(result["total_agent_score"], 14)
        self.assertEqual(result["total_rounding_adjustment"], 0.99)
        self.assertEqual(
            sum(task["agent_score"] for task in result["tasks"]),
            result["rounded_task_total"],
        )
        reproduced = apply_rounding([13.01], [20], 20, "generous-v1")
        self.assertEqual(
            reproduced["final_rounded_grade"],
            result["final_rounded_grade"],
        )

    @staticmethod
    def _criterion(criterion_id, score, deduction):
        return {
            "criterion_id": criterion_id,
            "agent_score": score,
            "deductions": [{
                "rule_id": criterion_id + "_rule",
                "points": deduction,
                "reason": "Rubric deduction.",
                "deduction_type": "demonstrated_technical_error",
                "cause_id": criterion_id,
                "independent_requirement": True,
            }],
            "justification": "Criterion feedback.",
            "evidence": [],
            "evidence_state": "demonstrated_error",
            "confidence": "high",
            "review_required": False,
            "review_reasons": [],
        }


if __name__ == "__main__":
    unittest.main()
