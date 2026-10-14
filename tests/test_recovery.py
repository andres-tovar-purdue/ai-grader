import copy
import csv
import json
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_grader.cli import main
from ai_grader.finalization import finalize_grading
from ai_grader.grading import grade_submissions
from ai_grader.recovery import recover_grading
from ai_grader.submission_checks import spec_fingerprint
import test_grading as fixtures
from types import SimpleNamespace


class RecoveryTests(unittest.TestCase):
    def make_saved_project(self, root, provenance=True):
        spec, manifest, _ = fixtures.GradingTests().make_project(root)
        debug = root / 'grader/debug_responses'
        debug.mkdir()
        saved = {'student_id': 'Student_001', 'model': 'saved-model',
                 'usage': {'input_tokens': 12, 'output_tokens': 8, 'total_tokens': 20},
                 'output_text': json.dumps(fixtures.model_response())}
        if provenance:
            saved['grading_spec_sha256'] = spec_fingerprint(spec)
            saved['submission_sha256'] = spec_fingerprint(manifest['submissions'][0])
        path = debug / 'Student_001_batch.json'
        path.write_text(json.dumps(saved), encoding='utf-8')
        return spec, manifest, saved, path

    def test_recovers_offline_preserves_saves_and_versions_without_identity_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, _, path = self.make_saved_project(root)
            before = path.read_bytes()
            old = root / 'grader/grading_runs/run_v001'
            old.mkdir(parents=True)
            (old / 'sentinel').write_bytes(b'preserve')
            original_open = Path.open
            def guarded_open(path, *args, **kwargs):
                if path.name == 'student_map.json' or 'submissions' in path.parts:
                    raise AssertionError('Private identity or original submissions were accessed')
                return original_open(path, *args, **kwargs)
            with patch('ai_grader.grading.get_client', side_effect=AssertionError('API client created')), \
                 patch.object(Path, 'open', guarded_open):
                run_path, _, csv_path, run = recover_grading(root, '*_batch.json')
            self.assertEqual(run_path.name, 'run_v002')
            self.assertEqual(run['model'], 'saved-model')
            self.assertEqual(run['results'][0]['total_agent_score'], 1)
            self.assertEqual(run['api_usage']['totals']['total_tokens'], 20)
            self.assertEqual(run['recovery']['new_api_calls'], 0)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual((old / 'sentinel').read_bytes(), b'preserve')
            rows = list(csv.DictReader(io.StringIO(csv_path.read_text())))
            self.assertEqual(rows[0]['validation_status'], 'validated')
            self.assertEqual(rows[0]['total_instructor_score'], '')

    def test_invalid_response_is_unscored_without_losing_valid_student(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, manifest, saved, path = self.make_saved_project(root)
            second = copy.deepcopy(manifest['submissions'][0]); second['student_id'] = 'Student_002'
            manifest['submissions'].append(second); manifest['submission_count'] = 2
            (root / 'grader/submission_manifest.json').write_text(json.dumps(manifest))
            shutil.copytree(root / 'grader/anonymized_submissions/Student_001',
                            root / 'grader/anonymized_submissions/Student_002')
            invalid = copy.deepcopy(saved)
            invalid['student_id'] = 'Student_002'
            invalid['submission_sha256'] = spec_fingerprint(second)
            response = fixtures.model_response(); response['student_id'] = 'Student_002'
            response['criteria'][0]['deductions'][0]['points'] = 0.5
            response['criteria'][0]['agent_score'] = 0.5
            invalid['output_text'] = json.dumps(response)
            second_path = path.with_name('Student_002_batch.json')
            second_path.write_text(json.dumps(invalid))
            run_path, _, csv_path, run = recover_grading(root, '*_batch.json')
            self.assertEqual(run['results'][0]['total_agent_score'], 1)
            result = run['results'][1]
            self.assertIsNone(result['total_agent_score'])
            self.assertEqual(result['raw_model_response'], response)
            self.assertEqual(result['validation_status'], 'invalid_model_response')
            self.assertIn('approved 0.25', result['validation_error'])
            self.assertEqual(run['unscored_submission_count'], 1)
            rows = list(csv.DictReader(io.StringIO(csv_path.read_text())))
            self.assertEqual(rows[1]['total_agent_score'], '')
            self.assertEqual(rows[1]['task_1_score'], '')
            with self.assertRaisesRegex(ValueError, 'Cannot finalize unscored'):
                finalize_grading(root, run_path.name, {'Student_001': 1, 'Student_002': 1})

    def test_input_fingerprint_mismatch_blocks_publication(self):
        for field in ['grading_spec_sha256', 'submission_sha256']:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); _, _, saved, path = self.make_saved_project(root)
                saved[field] = 'changed'; path.write_text(json.dumps(saved))
                with self.assertRaisesRegex(ValueError, 'fingerprint mismatch'):
                    recover_grading(root, '*_batch.json')
                self.assertFalse((root / 'grader/grading_runs').exists())

    def test_legacy_responses_are_explicitly_flagged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.make_saved_project(root, provenance=False)
            _, _, _, run = recover_grading(root, '*_batch.json')
            self.assertEqual(run['recovery']['legacy_response_students'], ['Student_001'])
            self.assertTrue(run['results'][0]['review_required'])
            self.assertTrue(any('input fingerprints' in reason for reason in run['results'][0]['review_reasons']))

    def test_duplicate_missing_and_unexpected_responses_block_publication(self):
        for situation in ['duplicate', 'missing', 'unexpected']:
            with self.subTest(situation=situation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); _, _, saved, path = self.make_saved_project(root)
                if situation == 'duplicate':
                    path.with_name('duplicate_batch.json').write_bytes(path.read_bytes())
                elif situation == 'missing':
                    path.unlink()
                else:
                    saved['student_id'] = 'Student_099'; path.write_text(json.dumps(saved))
                with self.assertRaises(ValueError):
                    recover_grading(root, '*_batch.json')
                self.assertFalse((root / 'grader/grading_runs').exists())

    def test_unsafe_pattern_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.make_saved_project(root)
            for pattern in ['../*.json', '..\\*.json', '*.txt']:
                with self.subTest(pattern=pattern), self.assertRaisesRegex(ValueError, 'filename pattern'):
                    recover_grading(root, pattern)

    def test_unknown_rule_remains_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); _, _, saved, path = self.make_saved_project(root)
            response = fixtures.model_response(); response['criteria'][0]['deductions'][0]['rule_id'] = 'invented'
            saved['output_text'] = json.dumps(response); path.write_text(json.dumps(saved))
            _, _, _, run = recover_grading(root, '*_batch.json')
            self.assertIsNone(run['results'][0]['total_agent_score'])
            self.assertIn('approved rule_id', run['results'][0]['validation_error'])

    def test_malformed_json_is_preserved_as_unscored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); _, _, saved, path = self.make_saved_project(root)
            saved['output_text'] = 'not JSON'; path.write_text(json.dumps(saved))
            _, _, _, run = recover_grading(root, '*_batch.json')
            self.assertIsNone(run['results'][0]['total_agent_score'])
            self.assertEqual(run['results'][0]['raw_model_response'], 'not JSON')

    def test_new_grading_saves_input_fingerprints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); spec, manifest, _ = fixtures.GradingTests().make_project(root)
            grade_submissions(root, SimpleNamespace(responses=fixtures.FakeResponses(fixtures.model_response())))
            saved = json.loads(next((root / 'grader/debug_responses').glob('*.json')).read_text())
            self.assertEqual(saved['grading_spec_sha256'], spec_fingerprint(spec))
            self.assertEqual(saved['submission_sha256'], spec_fingerprint(manifest['submissions'][0]))

    def test_cli_recovery_uses_only_saved_responses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.make_saved_project(root)
            with patch('sys.argv', ['ai-grader', 'recover-grading', str(root),
                                    '--response-pattern', '*_batch.json']), \
                 patch('ai_grader.grading.get_client', side_effect=AssertionError('API called')):
                main()
            self.assertTrue((root / 'grader/grading_runs/run_v001/grading_results.json').exists())

    def test_inspect_does_not_offer_finalization_for_unscored_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); _, _, saved, path = self.make_saved_project(root)
            for folder in ['project', 'datasets', 'rubric', 'reference']:
                (root / folder).mkdir()
            for filename in ['project/project_instructions.md', 'rubric/instructor_rubric.md',
                             'reference/reference_solution.md']:
                (root / filename).write_text('Prepared fixture')
            response = fixtures.model_response(); response['criteria'][0]['deductions'][0]['points'] = 0.5
            saved['output_text'] = json.dumps(response); path.write_text(json.dumps(saved))
            recover_grading(root, '*_batch.json')
            output = io.StringIO()
            with patch('sys.argv', ['ai-grader', 'inspect', str(root)]), patch('sys.stdout', output):
                main()
            self.assertIn('validation review required', output.getvalue())
            self.assertIn('Student_001', output.getvalue())
            self.assertNotIn('finalize-grading', output.getvalue())
