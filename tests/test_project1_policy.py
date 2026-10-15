import copy
import json
import tempfile
import unittest
from pathlib import Path

from ai_grader.spec_validation import validate_spec_data
from ai_grader.grading import build_student_input
import test_grading as fixtures

ROOT = Path(__file__).resolve().parents[1]


class RevisedPolicyTests(unittest.TestCase):
    def test_instructor_revision_validates_and_preserves_technical_deductions(self):
        spec = json.loads((ROOT / 'tests/fixtures/mspe_49600_pr01_policy_v003.json').read_text())
        schema = json.loads((ROOT / 'schemas/grading_spec.schema.json').read_text())
        validate_spec_data(spec, schema)
        self.assertEqual(spec['project']['total_points'], 100)
        self.assertEqual(spec['grading_policy']['discussion_policy'], 'presence_only')
        self.assertEqual(spec['grading_policy']['filename_policy'], 'warning_only')
        criteria = {c['criterion_id']: c for t in spec['tasks'] for c in t['criteria']}
        rules = {r['rule_id']: r for r in criteria['t1_c3']['deduction_rules']}
        self.assertIn('t1_c3_formula_error', rules)
        missing_metric = next(r for r in criteria['t2_c4']['deduction_rules']
                              if r['rule_id'] == 't2_c4_missing_value')
        self.assertEqual(missing_metric['deduction_points'], 1)

    def test_filename_penalties_cannot_reappear_in_warning_only_specification(self):
        spec = json.loads((ROOT / 'tests/fixtures/mspe_49600_pr01_policy_v003.json').read_text())
        schema = json.loads((ROOT / 'schemas/grading_spec.schema.json').read_text())
        rule = next(r for t in spec['tasks'] for c in t['criteria'] for r in c['deduction_rules']
                    if r.get('basis') == 'file_presence')
        rule['basis'] = 'filename'
        with self.assertRaisesRegex(ValueError, 'Warning-only filename'):
            validate_spec_data(spec, schema)

    def test_exported_vba_code_is_read_as_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code = b'Function BeamMoment(x As Double) As Double\nBeamMoment = 10*x\nEnd Function\n'
            spec, manifest, student_root = fixtures.GradingTests().make_project(root, extra_files=[
                {'path': 'artifact_002_student_module.bas', 'file_type': 'text', 'bytes': code}])
            result = build_student_input(spec, manifest['submissions'][0], student_root, 'instructions')
            texts = '\n'.join(item['text'] for item in result[0]['content'] if item['type'] == 'input_text')
            self.assertIn('Function BeamMoment', texts)
