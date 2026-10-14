import unittest

from ai_grader.grading_validation import calculate_grading_result


class GradingValidationTests(unittest.TestCase):
    def test_rejects_model_arithmetic(self):
        spec = {
            "project": {"total_points": 1},
            "tasks": [{
                "task_id": "T1",
                "title": "Task",
                "max_points": 1,
                "criteria": [{"criterion_id": "C1", "max_points": 1}],
            }],
        }
        submission = {"student_id": "Student_001", "files": [], "review_flags": []}
        response = {
            "student_id": "Student_001",
            "criteria": [{
                "criterion_id": "C1",
                "agent_score": 0.5,
                "deductions": [],
                "justification": "Partial evidence.",
                "evidence": [],
                "evidence_state": "inadequate_required_evidence",
                "confidence": "medium",
                "review_required": False,
                "review_reasons": [],
            }],
            "task_feedback": [{"task_id": "T1", "feedback": "Review."}],
            "review_required": False,
            "review_reasons": [],
        }

        with self.assertRaisesRegex(ValueError, "score plus deductions"):
            calculate_grading_result(response, spec, submission)

    def test_rejects_unverifiable_hidden_detail_deduction(self):
        spec, submission, response = self._policy_fixture()
        response["criteria"][0]["deductions"] = [{
            "points": 0.5,
            "reason": "Hidden setting was not inspectable.",
            "deduction_type": "hidden_detail_unverifiable",
            "cause_id": "hidden_setting",
            "independent_requirement": False,
        }]
        response["criteria"][0]["agent_score"] = 0.5
        with self.assertRaisesRegex(ValueError, "prohibited deduction type"):
            calculate_grading_result(response, spec, submission)

    def test_rejects_repeated_nonindependent_cause(self):
        spec, submission, response = self._policy_fixture(two_criteria=True)
        for item in response["criteria"]:
            item["agent_score"] = 0.5
            item["deductions"] = [{
                "rule_id": "missing_evidence",
                "points": 0.5,
                "reason": "Same missing screenshot.",
                "deduction_type": "inadequate_required_evidence",
                "cause_id": "missing_model_png",
                "independent_requirement": False,
            }]
        with self.assertRaisesRegex(ValueError, "Repeated deduction cause"):
            calculate_grading_result(response, spec, submission)

    def _policy_fixture(self, two_criteria=False):
        criteria = [{"criterion_id": "C1", "max_points": 1,
                     "deduction_rules": [{"rule_id": "missing_evidence", "deduction_points": 0.5}]}]
        if two_criteria:
            criteria.append({"criterion_id": "C2", "max_points": 1,
                             "deduction_rules": [{"rule_id": "missing_evidence", "deduction_points": 0.5}]})
        spec = {
            "project": {"total_points": len(criteria)},
            "tasks": [{
                "task_id": "T1",
                "title": "Task",
                "max_points": len(criteria),
                "criteria": criteria,
            }],
        }
        submission = {"student_id": "Student_001", "files": [], "review_flags": []}
        response_criteria = [{
            "criterion_id": criterion["criterion_id"],
            "agent_score": 1,
            "deductions": [],
            "justification": "Visible evidence.",
            "evidence": [],
            "evidence_state": "verified",
            "confidence": "high",
            "review_required": False,
            "review_reasons": [],
        } for criterion in criteria]
        response = {
            "student_id": "Student_001",
            "criteria": response_criteria,
            "task_feedback": [{"task_id": "T1", "feedback": "Review."}],
            "review_required": False,
            "review_reasons": [],
        }
        return spec, submission, response


if __name__ == "__main__":
    unittest.main()
