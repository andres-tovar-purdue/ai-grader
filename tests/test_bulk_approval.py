import csv
import tempfile
import unittest
from pathlib import Path

from ai_grader.finalization import load_approved_scores


class BulkApprovalTests(unittest.TestCase):
    def write_csv(self, root, rows, fields=None):
        path = root / 'review.csv'
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields or ['student_id', 'total_instructor_score'])
            writer.writeheader(); writer.writerows(rows)
        return path

    def test_reads_160_explicit_approved_scores(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(Path(directory), [{'student_id': f'Student_{i:03d}',
                        'total_instructor_score': '100'} for i in range(1, 161)])
            scores = load_approved_scores(path)
            self.assertEqual(len(scores), 160)
            self.assertEqual(scores['Student_160'], 100)

    def test_blank_score_never_uses_preliminary_score(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_csv(Path(directory), [{'student_id': 'Student_001',
                      'total_instructor_score': '', 'total_agent_score': '100'}],
                      ['student_id', 'total_instructor_score', 'total_agent_score'])
            with self.assertRaisesRegex(ValueError, 'blank'):
                load_approved_scores(path)

    def test_duplicate_and_nonfinite_scores_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [{'student_id': 'Student_001', 'total_instructor_score': '100'}] * 2
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                load_approved_scores(self.write_csv(root, rows))
            for value in ['nan', 'inf', '-1']:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    load_approved_scores(self.write_csv(root, [{'student_id': 'Student_001',
                                          'total_instructor_score': value}]))

    def test_missing_approval_column_and_empty_file_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, 'requires'):
                load_approved_scores(self.write_csv(root, [{'student_id': 'Student_001'}], ['student_id']))
            with self.assertRaisesRegex(ValueError, 'no students'):
                load_approved_scores(self.write_csv(root, []))
