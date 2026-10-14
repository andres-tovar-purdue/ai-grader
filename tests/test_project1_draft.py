import json
import unittest
from pathlib import Path

from ai_grader.spec_validation import validate_spec_data
from ai_grader.grading_validation import calculate_grading_result


class Project1DraftTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent
        self.spec = json.loads((root / "fixtures/mspe_49600_pr01_corrected_draft.json").read_text())
        self.criteria = {c["criterion_id"]: c for t in self.spec["tasks"] for c in t["criteria"]}

    def test_all_published_maxima_are_present_and_full_credit_is_100(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / "schemas/grading_spec.schema.json").read_text())
        validate_spec_data(self.spec, schema)
        self.assertEqual([t["max_points"] for t in self.spec["tasks"]], [25,20,15,15,20,5])
        self.assertEqual(len(self.criteria), 20)
        self.assertEqual(self.criteria["t1_c5"]["max_points"], 5)
        response = dict(student_id="Student_001", criteria=[dict(
            criterion_id=c["criterion_id"], agent_score=c["max_points"], deductions=[],
            justification="All requirements demonstrated.", evidence=[], evidence_state="verified",
            confidence="high", review_required=False, review_reasons=[]) for c in self.criteria.values()],
            task_feedback=[dict(task_id=t["task_id"],feedback="Complete.") for t in self.spec["tasks"]],
            review_required=False,review_reasons=[])
        result = calculate_grading_result(response, self.spec, dict(student_id="Student_001",files=[]))
        self.assertEqual(result["raw_total_before_rounding"],100)
        self.assertEqual(result["total_agent_score"],100)

    def test_lap_standard_deviation_and_sorting_rulings_are_explicit(self):
        self.assertIn("including lap",self.criteria["t1_c2"]["full_credit_condition"])
        self.assertIn("std_sector_3_time_s",self.criteria["t2_c2"]["full_credit_condition"])
        self.assertIn("Sorted and unsorted",self.criteria["t2_c2"]["acceptable_alternatives"][0])

    def test_filename_penalties_have_one_owner_and_draft_has_no_approval(self):
        filenames = [(c["criterion_id"],r["deliverable_id"]) for c in self.criteria.values()
                     for r in c.get("deduction_rules",[]) if r.get("basis")=="filename"]
        self.assertEqual(len(filenames),10)
        self.assertEqual({c for c,_ in filenames},{"t6_c1"})
        self.assertEqual(len({d for _,d in filenames}),10)
        self.assertEqual(self.spec["status"],"draft")
        self.assertNotIn("approval",self.spec)
        self.assertIn("proposed partial-credit policy",self.spec["notes"])


if __name__=="__main__":unittest.main()
