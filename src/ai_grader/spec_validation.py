import json
import math
from pathlib import Path

from jsonschema import Draft202012Validator


def load_json(path):
    with Path(path).open("r", encoding="utf-8") as file:
        return json.load(file)


def validate_scoring_totals(spec):
    """Reject inconsistent maxima, nonfinite values, and duplicate scoring IDs."""
    errors = []
    task_ids = set()
    criterion_ids = set()
    task_total = 0.0
    for task in spec["tasks"]:
        task_id = task["task_id"]
        if task_id in task_ids:
            errors.append(f"Duplicate task ID: {task_id}.")
        task_ids.add(task_id)
        maximum = float(task["max_points"])
        criterion_total = 0.0
        for criterion in task["criteria"]:
            criterion_id = criterion["criterion_id"]
            if criterion_id in criterion_ids:
                errors.append(f"Duplicate criterion ID: {criterion_id}.")
            criterion_ids.add(criterion_id)
            points = float(criterion["max_points"])
            if not math.isfinite(points) or points < 0:
                errors.append(f"{criterion_id} max_points must be finite and nonnegative.")
            criterion_total += points
            rule_ids = set()
            for rule in criterion.get("deduction_rules", []):
                if rule["rule_id"] in rule_ids:
                    errors.append(f"Duplicate deduction rule in {criterion_id}: {rule['rule_id']}.")
                rule_ids.add(rule["rule_id"])
                deduction = float(rule["deduction_points"])
                if not math.isfinite(deduction) or not 0 <= deduction <= points:
                    errors.append(f"{rule['rule_id']} deduction_points is outside criterion bounds.")
                if rule.get("basis") == "filename" and not rule.get("deliverable_id"):
                    errors.append(f"{rule['rule_id']} filename rule needs a deliverable_id.")
                if rule.get("basis") == "file_presence" and not rule.get("deliverable_id"):
                    errors.append(f"{rule['rule_id']} file presence rule needs a deliverable_id.")
        if not math.isfinite(maximum) or maximum < 0:
            errors.append(f"{task_id} max_points must be finite and nonnegative.")
        if not math.isfinite(criterion_total) or abs(criterion_total - maximum) > 1e-6:
            errors.append(f"Task {task_id} criterion points sum to {criterion_total:g}, "
                          f"but task max_points is {maximum:g}.")
        task_total += maximum
    project_total = float(spec["project"]["total_points"])
    if not math.isfinite(project_total) or project_total <= 0:
        errors.append("Project total_points must be finite and positive.")
    if not math.isfinite(task_total) or abs(task_total - project_total) > 1e-6:
        errors.append(f"Task points sum to {task_total:g}, but project total_points is "
                      f"{project_total:g}. Include separately scored deliverables as a task.")
    if errors:
        raise ValueError("Invalid grading specification:\n" + "\n".join(errors))
    return True


def validate_spec_data(spec, schema):
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(spec),
                    key=lambda error: tuple(str(item) for item in error.absolute_path))
    if errors:
        messages = [f"{'.'.join(map(str, error.absolute_path)) or '<root>'}: {error.message}"
                    for error in errors]
        raise ValueError("Invalid grading specification:\n" + "\n".join(messages))
    validate_scoring_totals(spec)
    source_ids = {item["source_id"] for item in spec["sources"]}
    deliverable_ids = {item["deliverable_id"] for item in spec["deliverables"]}
    task_ids = {item["task_id"] for item in spec["tasks"]}
    criterion_ids = {c["criterion_id"] for t in spec["tasks"] for c in t["criteria"]}
    if len(source_ids) != len(spec["sources"]) or len(deliverable_ids) != len(spec["deliverables"]):
        raise ValueError("Duplicate source or deliverable ID in grading specification.")
    for task in spec["tasks"]:
        for item in [task, *task["criteria"]]:
            if set(item.get("source_refs", [])) - source_ids:
                raise ValueError("Unknown source reference in grading specification.")
        for criterion in task["criteria"]:
            if set(criterion.get("dependencies", [])) - criterion_ids:
                raise ValueError("Unknown criterion dependency in grading specification.")
            for rule in criterion.get("deduction_rules", []):
                if (spec.get('grading_policy', {}).get('filename_policy') == 'warning_only'
                        and rule.get('basis') == 'filename'):
                    raise ValueError('Warning-only filename policy cannot contain filename deduction rules.')
                if rule.get("deliverable_id") and rule["deliverable_id"] not in deliverable_ids:
                    raise ValueError("Unknown deduction deliverable_id in grading specification.")
                if rule.get('basis') == 'file_presence':
                    deliverable = next(item for item in spec['deliverables']
                                       if item['deliverable_id'] == rule['deliverable_id'])
                    if not deliverable.get('presence_patterns'):
                        raise ValueError('File presence rules require deliverable presence_patterns.')
    for deliverable in spec["deliverables"]:
        if set(deliverable.get("applies_to_tasks", [])) - task_ids:
            raise ValueError("Unknown deliverable task reference in grading specification.")
    return True


def validate_grading_spec(spec_path, schema_path):
    return validate_spec_data(load_json(spec_path), load_json(schema_path))


def latest_spec_path(grader_path):
    """Use the latest revision; a newer draft must block use of an older approval."""
    paths = sorted(Path(grader_path).glob("grading_spec_v[0-9][0-9][0-9].json"))
    if not paths:
        raise FileNotFoundError("No grading specification found. Run generate-spec first.")
    return paths[-1]
