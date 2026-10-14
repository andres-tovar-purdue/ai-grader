import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ai_grader.finalization import finalize_grading
from ai_grader.grading import build_student_input, grade_submissions, load_grading_inputs
from ai_grader.grading_validation import calculate_grading_result
from ai_grader.spec_validation import latest_spec_path, validate_scoring_totals, validate_spec_data
from ai_grader.spec_approval import approve_grading_spec
from ai_grader.submission_checks import original_filename_checks, refresh_submission_checks, spec_fingerprint
from ai_grader.submission_processing import write_submission_manifest
import test_grading as grading_fixtures
from test_grading import FakeResponses, model_response
import test_grading_validation as validation_fixtures

SCHEMA = Path(__file__).resolve().parents[1] / 'schemas/grading_spec.schema.json'


class SpecificationSafeguardTests(unittest.TestCase):
    def fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            spec, _, _ = grading_fixtures.GradingTests().make_project(Path(directory))
        return spec

    def test_task_criterion_mismatch_is_rejected(self):
        spec = self.fixture()
        spec['tasks'][0]['max_points'] = 2
        spec['project']['total_points'] = 2
        with self.assertRaisesRegex(ValueError, 'criterion points sum'):
            validate_scoring_totals(spec)

    def test_project_task_mismatch_is_rejected(self):
        spec = self.fixture(); spec['project']['total_points'] = 100
        with self.assertRaisesRegex(ValueError, 'Task points sum'):
            validate_scoring_totals(spec)

    def test_duplicate_ids_are_rejected(self):
        for kind in ['task', 'criterion']:
            spec = self.fixture()
            if kind == 'task':
                spec['tasks'].append(copy.deepcopy(spec['tasks'][0]))
                spec['project']['total_points'] = 2
            else:
                spec['tasks'][0]['criteria'].append(copy.deepcopy(spec['tasks'][0]['criteria'][0]))
                spec['tasks'][0]['max_points'] = 2; spec['project']['total_points'] = 2
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'Duplicate'):
                validate_scoring_totals(spec)

    def test_nonfinite_maximum_is_rejected(self):
        spec = self.fixture(); spec['project']['total_points'] = float('nan')
        with self.assertRaisesRegex(ValueError, 'finite'):
            validate_scoring_totals(spec)

    def test_unknown_rule_deliverable_is_rejected(self):
        spec = self.fixture(); spec['tasks'][0]['criteria'][0]['deduction_rules'][0]['deliverable_id'] = 'unknown'
        with self.assertRaisesRegex(ValueError, 'Unknown deduction deliverable_id'):
            validate_spec_data(spec, json.loads(SCHEMA.read_text()))

    def test_approval_cannot_bypass_inconsistent_points(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); spec, _, _ = grading_fixtures.GradingTests().make_project(root, status='draft')
            path = root/'grader/grading_spec_v001.json'
            spec['tasks'][0]['max_points'] = 2; path.write_text(json.dumps(spec))
            with self.assertRaisesRegex(ValueError, 'criterion points sum'):
                approve_grading_spec(path, SCHEMA, 'Instructor', allow_unresolved=True)
            self.assertEqual(json.loads(path.read_text())['status'], 'draft')

    def test_newer_draft_blocks_older_approval_before_api_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); spec, _, _ = grading_fixtures.GradingTests().make_project(root)
            spec['status'] = 'draft'; (root/'grader/grading_spec_v002.json').write_text(json.dumps(spec))
            responses = FakeResponses(model_response())
            with self.assertRaisesRegex(RuntimeError, 'instructor-approved'):
                grade_submissions(root, SimpleNamespace(responses=responses))
            self.assertEqual(responses.calls, [])
            self.assertEqual(latest_spec_path(root/'grader').name, 'grading_spec_v002.json')


class DeductionSafeguardTests(unittest.TestCase):
    def fixture(self, two=False):
        return validation_fixtures.GradingValidationTests()._policy_fixture(two)

    def deduct(self, criterion, **changes):
        criterion['agent_score'] = .5
        criterion['deductions'] = [dict(rule_id='missing_evidence', points=.5,
                                      reason='Missing evidence.', deduction_type='inadequate_required_evidence',
                                      cause_id='missing_one', independent_requirement=True, **changes)]

    def test_unknown_rule_is_rejected(self):
        spec, sub, response = self.fixture(); self.deduct(response['criteria'][0])
        response['criteria'][0]['deductions'][0]['rule_id'] = 'invented'
        with self.assertRaisesRegex(ValueError, 'approved rule_id'):
            calculate_grading_result(response, spec, sub)

    def test_arbitrary_amount_is_rejected(self):
        spec, sub, response = self.fixture(); self.deduct(response['criteria'][0])
        response['criteria'][0]['deductions'][0]['points'] = .25
        response['criteria'][0]['agent_score'] = .75
        with self.assertRaisesRegex(ValueError, 'points do not match'):
            calculate_grading_result(response, spec, sub)

    def test_model_cannot_authorize_shared_cause(self):
        spec, sub, response = self.fixture(two=True)
        for criterion in response['criteria']: self.deduct(criterion)
        with self.assertRaisesRegex(ValueError, 'Repeated deduction cause'):
            calculate_grading_result(response, spec, sub)

    def test_spec_can_authorize_independent_shared_cause(self):
        spec, sub, response = self.fixture(two=True)
        for criterion in spec['tasks'][0]['criteria']:
            criterion['deduction_rules'][0]['allow_shared_cause'] = True
        for criterion in response['criteria']: self.deduct(criterion)
        result = calculate_grading_result(response, spec, sub, rounding_policy='exact-v1')
        self.assertEqual(result['total_agent_score'], 1)

    def test_repeated_rule_with_different_model_causes_is_rejected(self):
        spec, sub, response = self.fixture(); criterion = response['criteria'][0]; self.deduct(criterion)
        other = copy.deepcopy(criterion['deductions'][0]); other['cause_id'] = 'another_invented_cause'
        criterion['deductions'].append(other); criterion['agent_score'] = 0
        with self.assertRaisesRegex(ValueError, 'applied too many times'):
            calculate_grading_result(response, spec, sub)

    def test_filename_checks_prevent_false_penalty(self):
        spec, sub, response = self.fixture(); self.deduct(response['criteria'][0])
        rule = spec['tasks'][0]['criteria'][0]['deduction_rules'][0]
        rule.update(basis='filename', deliverable_id='notebook')
        sub['deliverable_checks'] = dict(method='original_filenames_local_v1', spec_sha256=spec_fingerprint(spec),
                                        checks=[dict(deliverable_id='notebook', status='exact_match', artifact_paths=[])])
        with self.assertRaisesRegex(ValueError, 'contradicts original filename checks'):
            calculate_grading_result(response, spec, sub)
        sub['deliverable_checks']['checks'][0]['status'] = 'no_exact_match'
        result = calculate_grading_result(response, spec, sub, rounding_policy='exact-v1')
        self.assertEqual(result['tasks'][0]['criteria'][0]['deductions'][0]['cause_id'], 'deliverable:notebook')

    def test_same_filename_cannot_be_penalized_twice_with_two_rule_ids(self):
        spec, sub, response = self.fixture(); self.deduct(response['criteria'][0])
        rules = spec['tasks'][0]['criteria'][0]['deduction_rules']
        rules[0].update(basis='filename', deliverable_id='plot')
        rules.append(dict(rule_id='label_disguised_as_name', deduction_points=.5, basis='filename', deliverable_id='plot'))
        criterion = response['criteria'][0]
        other = copy.deepcopy(criterion['deductions'][0]); other.update(rule_id='label_disguised_as_name', cause_id='other')
        criterion['deductions'].append(other); criterion['agent_score']=0
        sub['deliverable_checks'] = dict(method='original_filenames_local_v1', spec_sha256=spec_fingerprint(spec),
                                        checks=[dict(deliverable_id='plot', status='no_exact_match', artifact_paths=[])])
        with self.assertRaisesRegex(ValueError, 'Repeated deduction cause within criterion'):
            calculate_grading_result(response, spec, sub)

    def test_low_confidence_and_uninspected_pdf_require_review(self):
        spec, sub, response = self.fixture(); response['criteria'][0]['confidence'] = 'low'
        sub['files'] = [dict(path='artifact_001_notebook.pdf', extension='.pdf')]
        result = calculate_grading_result(response, spec, sub)
        self.assertTrue(result['review_required'])
        self.assertTrue(any('PDF' in reason for reason in result['review_reasons']))

    def test_negative_and_nonfinite_scores_are_rejected(self):
        for value in [-.5, float('nan'), float('inf')]:
            spec, sub, response = self.fixture(); response['criteria'][0]['agent_score'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'finite and nonnegative'):
                calculate_grading_result(response, spec, sub)


class EvidenceAndFilenameTests(unittest.TestCase):
    def test_original_name_passes_after_tool_anonymization(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder/'abc123_pr01_analysis.csv').write_text('x\n1\n')
            spec={'deliverables':[dict(deliverable_id='analysis_csv',file_patterns=['username_pr01_analysis.csv'])]}
            checks=original_filename_checks(folder,'abc123','Jane Student',spec)
            serialized=json.dumps(checks)
            self.assertNotIn('abc123',serialized); self.assertNotIn('Jane',serialized)
            self.assertEqual(checks['checks'][0]['status'],'exact_match')
            self.assertEqual(checks['checks'][0]['artifact_paths'],['artifact_001_student_pr01_analysis.csv'])

    def test_real_typo_is_preserved_as_failed_compliance(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory); (folder/'abc123_pro1_analysis.csv').write_text('x\n1\n')
            checks=original_filename_checks(folder,'abc123','Jane Student',{'deliverables':[
                dict(deliverable_id='analysis_csv',file_patterns=['username_pr01_analysis.csv'])]})
            self.assertEqual(checks['checks'][0]['status'],'no_exact_match')

    def make_original_project(self, root):
        folder=root/'submissions/123-456 - abc123 Jane Student - Jul 30, 2026 1053 PM'
        folder.mkdir(parents=True); (folder/'abc123_pr01_analysis.csv').write_text('x\n1\n')
        spec,_,_=grading_fixtures.GradingTests().make_project(root/'fixture')
        grader=root/'grader';grader.mkdir();(grader/'grading_spec_v001.json').write_text(json.dumps(spec))
        write_submission_manifest(root)
        return folder

    def test_refresh_keeps_existing_tree_and_private_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.make_original_project(root)
            tree=root/'grader/anonymized_submissions'
            before={str(p.relative_to(tree)):p.read_bytes() for p in tree.rglob('*') if p.is_file()}
            private=(root/'grader/student_map.json').read_bytes()
            path,manifest=refresh_submission_checks(root)
            after={str(p.relative_to(tree)):p.read_bytes() for p in tree.rglob('*') if p.is_file()}
            self.assertEqual(before,after);self.assertEqual(private,(root/'grader/student_map.json').read_bytes())
            self.assertIn('deliverable_checks',manifest['submissions'][0])
            self.assertTrue(path.with_name('submission_manifest.before_checks.json').exists())

    def test_refresh_accepts_windows_crlf_anonymized_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.make_original_project(root)
            copied = next((root / 'grader/anonymized_submissions').rglob('*.csv'))
            # Decode native LF/CRLF first so Windows CRLF is not doubled.
            text = copied.read_text(encoding='utf-8')
            copied.write_bytes(text.replace('\n', '\r\n').encode('utf-8'))
            before = copied.read_bytes()
            self.assertEqual(before, b'x\r\n1\r\n')
            _, manifest = refresh_submission_checks(root)
            self.assertIn('deliverable_checks', manifest['submissions'][0])
            self.assertEqual(copied.read_bytes(), before)

    def test_refresh_still_rejects_changed_anonymized_text_with_crlf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.make_original_project(root)
            copied = next((root / 'grader/anonymized_submissions').rglob('*.csv'))
            copied.write_bytes(b'x\r\n2\r\n')
            before = (root / 'grader/submission_manifest.json').read_bytes()
            with self.assertRaisesRegex(RuntimeError, 'Student_001/artifact_'):
                refresh_submission_checks(root)
            self.assertEqual((root / 'grader/submission_manifest.json').read_bytes(), before)

    def test_refresh_still_rejects_changed_binary_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / 'submissions/123-456 - abc123 Jane Student - Jul 30, 2026 1053 PM'
            folder.mkdir(parents=True); (folder / 'abc123_plot.png').write_bytes(b'ABC')
            grader = root / 'grader'; grader.mkdir()
            with tempfile.TemporaryDirectory() as fixture_dir:
                spec, _, _ = grading_fixtures.GradingTests().make_project(Path(fixture_dir))
            (grader / 'grading_spec_v001.json').write_text(json.dumps(spec))
            write_submission_manifest(root)
            copied = next((grader / 'anonymized_submissions').rglob('*.png'))
            copied.write_bytes(b'XYZ')
            with self.assertRaisesRegex(RuntimeError, 'Artifact content differs'):
                refresh_submission_checks(root)

    def test_refresh_refuses_changed_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);folder=self.make_original_project(root)
            old=(root/'grader/submission_manifest.json').read_bytes()
            (folder/'abc123_pr01_analysis.csv').write_text('x\n2\n')
            with self.assertRaisesRegex(RuntimeError,'artifacts changed'):
                refresh_submission_checks(root)
            self.assertEqual(old,(root/'grader/submission_manifest.json').read_bytes())

    def test_notebook_source_saved_text_and_images_are_preserved_compactly(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            notebook=dict(metadata={'irrelevant':'metadata'*10000},cells=[
                dict(cell_type='markdown',source=['Reflection: cleaning can affect the regression.']),
                dict(cell_type='code',source=['print(2)'],outputs=[dict(output_type='stream',text=['2\n']),
                    dict(output_type='display_data',data={'text/plain':['table'], 'image/png':'cG5n'})])])
            spec,manifest,student_root=grading_fixtures.GradingTests().make_project(root,extra_files=[
                dict(path='artifact_002_notebook.ipynb',file_type='text',bytes=json.dumps(notebook).encode())])
            api_input=build_student_input(spec,manifest['submissions'][0],student_root,'instructions')
            content=api_input[0]['content'];text='\n'.join(item['text'] for item in content if item['type']=='input_text')
            self.assertIn('Reflection:',text);self.assertIn('print(2)',text);self.assertIn('table',text)
            self.assertIn('CELL 2',text);self.assertNotIn('metadata'*100,text)
            self.assertTrue(any(item['type']=='input_image' for item in content))

    def test_failed_model_validation_still_saves_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);grading_fixtures.GradingTests().make_project(root)
            response=model_response();response['criteria'][0]['deductions'][0]['rule_id']='invented'
            with self.assertRaisesRegex(ValueError,'approved rule_id'):
                grade_submissions(root,SimpleNamespace(responses=FakeResponses(response)))
            self.assertEqual(len(list((root/'grader/debug_responses').glob('*.json'))),1)
            self.assertFalse((root/'grader/grading_runs').exists())

    def test_empty_manifest_blocks_api_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);_,manifest,_=grading_fixtures.GradingTests().make_project(root)
            manifest['submissions']=[];manifest['submission_count']=0
            (root/'grader/submission_manifest.json').write_text(json.dumps(manifest))
            responses=FakeResponses(model_response())
            with self.assertRaisesRegex(RuntimeError,'no submissions'):
                grade_submissions(root,SimpleNamespace(responses=responses))
            self.assertEqual(responses.calls,[])

    def test_finalization_uses_snapshot_and_handles_final_rounding(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);grading_fixtures.GradingTests().make_project(root)
            # Score 0.25 rounds at task level to .5 and then project level to 1.
            spec_path=root/'grader/grading_spec_v001.json';spec=json.loads(spec_path.read_text())
            spec['tasks'][0]['criteria'][0]['deduction_rules'][0]['deduction_points']=.75
            spec_path.write_text(json.dumps(spec))
            response=model_response();response['criteria'][0]['agent_score']=.25
            response['criteria'][0]['deductions'][0]['points']=.75
            run_path,_,_,run=grade_submissions(root,SimpleNamespace(responses=FakeResponses(response)))
            (root/'grader/student_map.json').write_text(json.dumps({'Student_001':{'full_name':'Jane Student'}}))
            # An unrelated later file edit must not change this run's denominator or rubric.
            spec_path.write_text('{}')
            output,csv_path,txt_paths,validation=finalize_grading(root,run_path.name,{'Student_001':1})
            self.assertTrue(all(validation.values()))
            self.assertIn('Final Grade: 1/1',txt_paths[0].read_text())
            self.assertEqual(csv_path.name,'demo_instructor_summary.csv')
            with self.assertRaisesRegex(ValueError,'outside its point range'):
                finalize_grading(root,run_path.name,{'Student_001':1},{('Student_001','T1_C1'):2})
            snapshot=run_path/'grading_spec_snapshot.json';snapshot.write_text('{}')
            with self.assertRaises(ValueError):
                finalize_grading(root,run_path.name,{'Student_001':1})


if __name__=='__main__':unittest.main()
