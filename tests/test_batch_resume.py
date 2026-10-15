import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_grader.grading import grade_submissions
import test_grading as fixtures


class SequenceResponses:
    def __init__(self, items):
        self.items = iter(items)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = next(self.items)
        if isinstance(item, BaseException):
            raise item
        return SimpleNamespace(output_text=json.dumps(item), usage=None)


class BatchResumeTests(unittest.TestCase):
    def make_two_students(self, root):
        _, manifest, student_root = fixtures.GradingTests().make_project(root)
        second = copy.deepcopy(manifest['submissions'][0]); second['student_id'] = 'Student_002'
        manifest['submissions'].append(second); manifest['submission_count'] = 2
        (root / 'grader/submission_manifest.json').write_text(json.dumps(manifest))
        shutil.copytree(student_root, student_root.with_name('Student_002'))
        first = fixtures.model_response()
        second_response = copy.deepcopy(first); second_response['student_id'] = 'Student_002'
        return first, second_response

    def test_invalid_student_does_not_abort_batch_and_resume_makes_no_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); first, second = self.make_two_students(root)
            second['criteria'][0]['deductions'][0]['points'] = 0.5
            client = SimpleNamespace(responses=SequenceResponses([first, second]))
            _, _, _, run = grade_submissions(root, client, model='test-model', resume=True)
            self.assertEqual(run['submission_count'], 2)
            self.assertEqual(run['unscored_submission_count'], 1)
            self.assertEqual(run['results'][0]['total_agent_score'], 1)
            self.assertIsNone(run['results'][1]['total_agent_score'])
            with patch('ai_grader.grading.get_client', side_effect=AssertionError('API client created')):
                _, _, _, resumed = grade_submissions(root, model='test-model', resume=True)
            self.assertEqual(resumed['batch_execution'], {'api_calls_made': 0, 'reused_responses': 2})
            self.assertEqual(resumed['unscored_submission_count'], 1)

    def test_retry_failed_reuses_valid_student_and_calls_only_failed_student(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); first, second = self.make_two_students(root)
            invalid = copy.deepcopy(second); invalid['criteria'][0]['deductions'][0]['rule_id'] = 'unknown'
            grade_submissions(root, SimpleNamespace(responses=SequenceResponses([first, invalid])),
                              model='test-model', resume=True)
            retry = SequenceResponses([second])
            _, _, _, run = grade_submissions(root, SimpleNamespace(responses=retry),
                                             model='test-model', resume=True, retry_failed=True)
            self.assertEqual(len(retry.calls), 1)
            self.assertEqual(run['batch_execution'], {'api_calls_made': 1, 'reused_responses': 1})
            self.assertEqual(run['unscored_submission_count'], 0)

    def test_interrupted_batch_resumes_from_atomic_student_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); first, second = self.make_two_students(root)
            interrupted = SequenceResponses([first, KeyboardInterrupt()])
            with self.assertRaises(KeyboardInterrupt):
                grade_submissions(root, SimpleNamespace(responses=interrupted), model='test-model', resume=True)
            self.assertEqual(len(list((root / 'grader/debug_responses').glob('*.json'))), 1)
            self.assertFalse(list((root / 'grader/debug_responses').glob('*.tmp')))
            remaining = SequenceResponses([second])
            _, _, _, run = grade_submissions(root, SimpleNamespace(responses=remaining), model='test-model', resume=True)
            self.assertEqual(len(remaining.calls), 1)
            self.assertEqual(run['batch_execution']['reused_responses'], 1)
            self.assertEqual(run['submission_count'], 2)

    def test_api_error_is_checkpointed_and_other_students_continue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); _, second = self.make_two_students(root)
            responses = SequenceResponses([RuntimeError('temporary service failure'), second])
            _, _, _, run = grade_submissions(root, SimpleNamespace(responses=responses), model='test-model', resume=True)
            self.assertEqual(run['unscored_submission_count'], 1)
            self.assertIn('API request failed', run['results'][0]['validation_error'])
            self.assertEqual(run['results'][1]['total_agent_score'], 1)
            with patch('ai_grader.grading.get_client', side_effect=AssertionError('API client created')):
                _, _, _, resumed = grade_submissions(root, model='test-model', resume=True)
            self.assertEqual(resumed['batch_execution']['api_calls_made'], 0)

    def test_changed_model_spec_prompt_or_artifact_never_reuses_stale_result(self):
        for change in ['model', 'spec', 'prompt', 'artifact']:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); fixtures.GradingTests().make_project(root)
                grade_submissions(root, SimpleNamespace(responses=SequenceResponses([fixtures.model_response()])),
                                  model='original-model', resume=True)
                model = 'new-model' if change == 'model' else 'original-model'
                if change == 'spec':
                    path = root / 'grader/grading_spec_v001.json'
                    spec = json.loads(path.read_text()); spec['notes'] = 'New instructor ruling'
                    path.write_text(json.dumps(spec))
                if change == 'artifact':
                    path = root / 'grader/submission_manifest.json'
                    manifest = json.loads(path.read_text()); file = manifest['submissions'][0]['files'][0]
                    file['content'] += '% changed code\n'; file['size_bytes'] = len(file['content'].encode())
                    (root / 'grader/anonymized_submissions/Student_001' / file['path']).write_text(file['content'])
                    path.write_text(json.dumps(manifest))
                from ai_grader.grading import build_student_input
                def changed_input(*args):
                    data = build_student_input(*args)
                    if change == 'prompt':
                        data[0]['content'].append({'type': 'input_text', 'text': 'Changed grading instruction'})
                    return data
                responses = SequenceResponses([fixtures.model_response()])
                with patch('ai_grader.grading.build_student_input', side_effect=changed_input):
                    _, _, _, run = grade_submissions(root, SimpleNamespace(responses=responses), model=model, resume=True)
                self.assertEqual(len(responses.calls), 1)
                self.assertEqual(run['batch_execution']['reused_responses'], 0)

    def test_retry_failed_requires_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); fixtures.GradingTests().make_project(root)
            with self.assertRaisesRegex(ValueError, 'requires resume'):
                grade_submissions(root, retry_failed=True)
