import unittest

from ai_grader.submission_checks import deliverable_presence_checks
from ai_grader.grading_validation import calculate_grading_result
import test_grading_validation as fixtures


class DeliverablePresenceTests(unittest.TestCase):
    def fixture(self):
        return {'deliverables': [
            {'deliverable_id': 'analysis_csv', 'presence_patterns': ['*analysis*.csv']},
            {'deliverable_id': 'correlation_png', 'presence_patterns': ['*correlation*.png']},
            {'deliverable_id': 'pairplot_png', 'presence_patterns': ['*pairplot*.png']},
        ]}

    def test_case_and_project_spelling_do_not_change_presence(self):
        sub = {'files': [{'path': 'artifact_001_student_pro1_ANALYSIS.csv'},
                         {'path': 'artifact_002_student_CORRELATION (1).png'},
                         {'path': 'artifact_003_student_pairplot.png'}]}
        checks = deliverable_presence_checks(sub, self.fixture())['checks']
        self.assertEqual([c['status'] for c in checks], ['present', 'present', 'present'])

    def test_missing_role_is_distinguished_from_unrecognizable_name(self):
        sub = {'files': [{'path': 'artifact_001_student_analysis.csv'},
                         {'path': 'artifact_002_student_pairplot.png'}]}
        checks = {c['deliverable_id']: c for c in deliverable_presence_checks(sub, self.fixture())['checks']}
        self.assertEqual(checks['correlation_png']['status'], 'missing')
        sub['files'].append({'path': 'artifact_003_generic_plot.png'})
        checks = {c['deliverable_id']: c for c in deliverable_presence_checks(sub, self.fixture())['checks']}
        self.assertEqual(checks['correlation_png']['status'], 'ambiguous')

    def test_one_file_claimed_by_two_roles_is_ambiguous(self):
        sub = {'files': [{'path': 'artifact_001_correlation_pairplot.png'}]}
        checks = deliverable_presence_checks(sub, self.fixture())['checks']
        self.assertEqual([c['status'] for c in checks[1:]], ['ambiguous', 'ambiguous'])

    def test_duplicate_role_name_cannot_prove_another_file_is_missing(self):
        sub = {'files': [{'path': 'artifact_001_student_analysis.csv'},
                         {'path': 'artifact_002_student_pairplot.png'},
                         {'path': 'artifact_003_student_pairplot (1).png'}]}
        checks = {c['deliverable_id']: c for c in deliverable_presence_checks(sub, self.fixture())['checks']}
        self.assertEqual(checks['correlation_png']['status'], 'ambiguous')

    def test_missing_file_deduction_requires_confirmed_local_absence(self):
        for state in ['present', 'ambiguous', 'missing']:
            with self.subTest(state=state):
                spec, sub, response = fixtures.GradingValidationTests()._policy_fixture(False)
                spec['deliverables'] = [{'deliverable_id': 'plot', 'presence_patterns': ['*correlation*.png']}]
                rule = spec['tasks'][0]['criteria'][0]['deduction_rules'][0]
                rule.update(basis='file_presence', deliverable_id='plot')
                sub['files'] = ([{'path': 'artifact_001_correlation.png'}] if state == 'present'
                                else [{'path': 'artifact_001_generic.png'}] if state == 'ambiguous' else [])
                criterion = response['criteria'][0]; criterion['agent_score'] = 0.5
                criterion['evidence'] = []
                criterion['deductions'] = [{'rule_id': 'missing_evidence', 'points': 0.5,
                    'reason': 'Missing file', 'deduction_type': 'missing_required_deliverable',
                    'cause_id': 'missing_plot', 'independent_requirement': True}]
                if state == 'missing':
                    result = calculate_grading_result(response, spec, sub)
                    self.assertEqual(result['tasks'][0]['criteria'][0]['deductions'][0]['cause_id'], 'deliverable:plot')
                else:
                    with self.assertRaisesRegex(ValueError, 'contradicts local presence'):
                        calculate_grading_result(response, spec, sub)
