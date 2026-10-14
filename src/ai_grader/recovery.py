"""Recover saved model responses offline without accepting invalid grading."""
import hashlib
import json
from pathlib import Path

from ai_grader.grading import (
    load_grading_inputs, parse_json_response, write_grading_run,
)
from ai_grader.grading_validation import (
    calculate_grading_result, validate_model_grading_response,
)
from ai_grader.rounding import DEFAULT_ROUNDING_POLICY
from ai_grader.submission_checks import spec_fingerprint


def _unscored_result(submission, spec, error, raw_response):
    reason = "Saved model response failed validation: " + str(error)
    return {
        "student_id": submission["student_id"],
        "validation_status": "invalid_model_response",
        "validation_error": str(error),
        "raw_model_response": raw_response,
        "total_agent_score": None,
        "raw_total_before_rounding": None,
        "total_rounding_adjustment": None,
        "total_instructor_score": None,
        "project_total_points": spec["project"]["total_points"],
        "review_required": True,
        "review_reasons": [reason],
        "tasks": [{
            "task_id": task["task_id"], "title": task["title"],
            "max_points": task["max_points"], "agent_score": None,
            "feedback": "Score withheld pending review of the saved model response.",
            "criteria": [{"criterion_id": criterion["criterion_id"],
                          "max_points": criterion["max_points"], "agent_score": None}
                         for criterion in task["criteria"]],
        } for task in spec["tasks"]],
    }


def recover_grading(project_path, response_pattern,
                    rounding_policy=DEFAULT_ROUNDING_POLICY):
    """Require exactly one saved response per student, then validate and publish.

    Never call an API, repair model deductions, or read the private identity map.
    Legacy saves lacking input fingerprints are explicitly flagged for review.
    """
    root = Path(project_path).resolve()
    spec_path, spec, manifest_path, manifest, _ = load_grading_inputs(root)
    if (not response_pattern or '/' in response_pattern or '\\' in response_pattern
            or '..' in response_pattern or not response_pattern.endswith('.json')):
        raise ValueError("Response pattern must be a JSON filename pattern without directories.")
    debug_root = (root / 'grader/debug_responses').resolve()
    saves = {}
    for path in sorted(debug_root.glob(response_pattern)):
        if not path.is_file() or path.resolve().parent != debug_root:
            raise ValueError("Saved response must be a file inside debug_responses.")
        saved = json.loads(path.read_text(encoding='utf-8'))
        student_id = saved.get('student_id')
        if student_id in saves:
            raise ValueError(f"Multiple saved responses matched for {student_id}; use a narrower pattern.")
        saves[student_id] = (path, saved)
    students = {item['student_id'] for item in manifest['submissions']}
    if set(saves) != students:
        raise ValueError("Saved responses must match every manifest student exactly once; "
                         f"missing={sorted(students - set(saves))}, "
                         f"unexpected={sorted(set(saves) - students)}.")
    models = {saved.get('model') for _, saved in saves.values()}
    if len(models) != 1 or not all(isinstance(model, str) and model for model in models):
        raise ValueError("Saved responses must identify one common model.")
    schema_path = Path(__file__).resolve().parents[2] / 'schemas/grading_result.schema.json'
    results, usage_records, sources = [], [], []
    legacy_ids = []
    for submission in manifest['submissions']:
        student_id = submission['student_id']
        path, saved = saves[student_id]
        for field, expected in [('grading_spec_sha256', spec_fingerprint(spec)),
                                ('submission_sha256', spec_fingerprint(submission))]:
            if field in saved and saved[field] != expected:
                raise ValueError(f"Saved response input fingerprint mismatch for {student_id}: {field}.")
        legacy = any(field not in saved for field in
                     ['grading_spec_sha256', 'submission_sha256'])
        if legacy:
            legacy_ids.append(student_id)
        if not isinstance(saved.get('output_text'), str):
            raise ValueError(f"Saved response for {student_id} has no output_text.")
        raw = None
        try:
            raw = parse_json_response(saved['output_text'])
            validate_model_grading_response(raw, schema_path)
            calculated = calculate_grading_result(raw, spec, submission, rounding_policy)
            calculated['validation_status'] = 'validated'
        except (ValueError, json.JSONDecodeError) as error:
            calculated = _unscored_result(submission, spec, error,
                                          raw if raw is not None else saved['output_text'])
        if legacy:
            calculated['review_required'] = True
            calculated['review_reasons'].append(
                "Legacy saved response has no complete input fingerprints; confirm the "
                "approved specification and submission were unchanged when it was generated.")
        usage = saved.get('usage')
        calculated['api_usage'] = usage
        results.append(calculated)
        usage_records.append({'student_id': student_id, 'available': usage is not None,
                              **(usage or {})})
        sources.append({'student_id': student_id, 'filename': path.name,
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    recovery = {'method': 'saved_responses_offline_v1', 'new_api_calls': 0,
                'sources': sources, 'legacy_response_students': legacy_ids}
    return write_grading_run(root, spec_path, spec, manifest_path, results,
                             usage_records, next(iter(models)), rounding_policy,
                             recovery=recovery)
