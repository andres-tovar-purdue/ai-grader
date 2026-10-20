import json
import math
from collections import Counter
from pathlib import Path

from jsonschema import Draft202012Validator

from ai_grader.spec_validation import validate_scoring_totals
from ai_grader.submission_processing import load_notebook

from ai_grader.rounding import (
    DEFAULT_ROUNDING_POLICY,
    apply_rounding,
)


def load_json(path):
    with Path(path).open("r", encoding="utf-8") as file:
        return json.load(file)


def validate_model_grading_response(response, schema_path):
    schema = load_json(schema_path)
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(response),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        messages = []
        for error in errors:
            location = ".".join(str(item) for item in error.absolute_path)
            messages.append(f"{location or '<root>'}: {error.message}")
        raise ValueError("Invalid grading response:\n" + "\n".join(messages))


def artifact_review_reasons(submission):
    """Review reasons for artifacts the grader could not fully read."""
    reasons = []
    for file in submission["files"]:
        extension = file.get("extension", "").lower()
        if extension == ".pdf":
            extraction = file.get("text_extraction")
            if extraction is None:  # Prepared before PDF text extraction.
                reasons.append(
                    f"PDF contents of {file['path']} were not inspected; "
                    "verify the PDF manually.")
            elif extraction["status"] != "extracted":
                reasons.append(
                    f"No text could be extracted from {file['path']}; "
                    "verify the PDF manually.")
            elif extraction.get("truncated"):
                reasons.append(
                    f"Only part of the text of {file['path']} was given to the "
                    "grader; review the rest of the PDF manually.")
        elif extension == ".ipynb" and "content" in file:
            if load_notebook(file["content"])[0] is None:
                reasons.append(
                    f"{file['path']} could not be read as a notebook; its cells "
                    "and outputs were not graded. Review it manually.")
    return reasons


def calculate_grading_result(
    response,
    spec,
    submission,
    rounding_policy=DEFAULT_ROUNDING_POLICY,
):
    """Validate semantic references and calculate all totals locally."""
    validate_scoring_totals(spec)
    if response["student_id"] != submission["student_id"]:
        raise ValueError("Grading response student_id does not match submission.")

    expected_criteria = {
        criterion["criterion_id"]: (task["task_id"], criterion)
        for task in spec["tasks"]
        for criterion in task["criteria"]
    }
    returned = {item["criterion_id"]: item for item in response["criteria"]}
    if len(returned) != len(response["criteria"]):
        raise ValueError("Grading response contains duplicate criterion IDs.")
    if set(returned) != set(expected_criteria):
        raise ValueError("Grading response criterion IDs do not match the spec.")

    feedback = {item["task_id"]: item["feedback"] for item in response["task_feedback"]}
    expected_tasks = {task["task_id"] for task in spec["tasks"]}
    if len(feedback) != len(response["task_feedback"]) or set(feedback) != expected_tasks:
        raise ValueError("Grading response task feedback does not match the spec.")

    artifact_paths = {file["path"] for file in submission["files"]}
    task_results = []
    raw_task_subtotals = []
    all_review_reasons = list(submission.get("review_flags", []))
    all_review_reasons.extend(response["review_reasons"])
    all_review_reasons.extend(artifact_review_reasons(submission))
    any_review = bool(all_review_reasons) or response["review_required"]
    deduction_causes = {}

    for task in spec["tasks"]:
        criterion_results = []
        task_score = 0.0
        for criterion in task["criteria"]:
            result = dict(returned[criterion["criterion_id"]])
            result["deductions"] = [dict(item) for item in result["deductions"]]
            result["review_reasons"] = list(result["review_reasons"])
            score = float(result["agent_score"])
            maximum = float(criterion["max_points"])
            if not math.isfinite(score) or score < 0:
                raise ValueError(f"{criterion['criterion_id']} score must be finite and nonnegative.")
            if score > maximum:
                raise ValueError(
                    f"{criterion['criterion_id']} score exceeds max_points."
                )
            deduction_total = sum(
                float(item["points"]) for item in result["deductions"]
            )
            approved_rules = {rule["rule_id"]: rule for rule in criterion.get("deduction_rules", [])}
            rule_uses = Counter()
            criterion_causes = set()
            for deduction in result["deductions"]:
                deduction_type = deduction["deduction_type"]
                if deduction_type in {
                    "hidden_detail_unverifiable",
                    "downstream_consequence",
                }:
                    raise ValueError(
                        f"{criterion['criterion_id']} uses prohibited deduction "
                        f"type {deduction_type}."
                    )
                rule_id = deduction.get("rule_id")
                if rule_id not in approved_rules:
                    raise ValueError(f"{criterion['criterion_id']} deduction must cite an approved rule_id: {rule_id}.")
                rule = approved_rules[rule_id]
                points = float(deduction["points"])
                if not math.isfinite(points) or points <= 0:
                    raise ValueError(f"{rule_id} deduction must be finite and positive.")
                if abs(points - float(rule["deduction_points"])) > 1e-6:
                    raise ValueError(f"{rule_id} deduction points do not match the approved rule: "
                                     f"received {points:g}, approved {float(rule['deduction_points']):g}.")
                rule_uses[rule_id] += 1
                if rule_uses[rule_id] > rule.get("max_applications", 1):
                    raise ValueError(f"{rule_id} deduction rule applied too many times.")
                cause_id = deduction["cause_id"]
                if rule.get('basis') == 'file_presence':
                    from ai_grader.submission_checks import deliverable_presence_checks
                    checks = deliverable_presence_checks(submission, spec)['checks']
                    check = next((item for item in checks
                                  if item['deliverable_id'] == rule['deliverable_id']), None)
                    if not check or check['status'] != 'missing':
                        raise ValueError('Missing-file deduction contradicts local presence checks; '
                                         'present or ambiguously named files cannot be penalized as missing.')
                    cause_id = 'deliverable:' + rule['deliverable_id']
                    deduction['cause_id'] = cause_id
                if rule.get("basis") == "filename":
                    from ai_grader.submission_checks import spec_fingerprint
                    checks = submission.get("deliverable_checks", {})
                    if (checks.get("method") != "original_filenames_local_v1"
                            or checks.get("spec_sha256") != spec_fingerprint(spec)):
                        raise ValueError("Filename deductions require current original filename checks.")
                    check = next((item for item in checks.get("checks", [])
                                  if item["deliverable_id"] == rule["deliverable_id"]), None)
                    if not check or check["status"] != "no_exact_match":
                        raise ValueError("Filename deduction contradicts original filename checks.")
                    # A missing/misnamed deliverable is one cause, even if the model invents two IDs.
                    cause_id = "deliverable:" + rule["deliverable_id"]
                    deduction["cause_id"] = cause_id
                if cause_id in criterion_causes:
                    raise ValueError(f"Repeated deduction cause within criterion: {cause_id}.")
                criterion_causes.add(cause_id)
                deduction_causes.setdefault(cause_id, []).append(
                    (criterion["criterion_id"], rule.get("allow_shared_cause", False))
                )
            if abs((score + deduction_total) - maximum) > 1e-6:
                raise ValueError(
                    f"{criterion['criterion_id']} score plus deductions does not equal max_points: score={score}, "
                    f"deductions={deduction_total}, max_points={maximum}; "
                    f"score + deductions={score + deduction_total}."
                )

            for evidence in result["evidence"]:
                path = evidence["artifact_path"]
                if path is not None and path not in artifact_paths:
                    raise ValueError(
                        f"Unknown evidence path for {criterion['criterion_id']}: {path}"
                    )
                if evidence["evidence_type"] == "unverifiable":
                    result["review_required"] = True
                    if not result["review_reasons"]:
                        result["review_reasons"] = [
                            "Required evidence could not be verified."
                        ]

            if result["confidence"] == "low" or result["evidence_state"] == "hidden_detail_unverifiable":
                result["review_required"] = True
                result["review_reasons"].append("Low-confidence or unverifiable evidence needs instructor review.")
            result["max_points"] = criterion["max_points"]
            result["deduction_total"] = round(deduction_total, 6)
            task_score += score
            if result["review_required"]:
                any_review = True
                all_review_reasons.extend(result["review_reasons"])
            criterion_results.append(result)

        if abs(task_score - sum(item["agent_score"] for item in criterion_results)) > 1e-6:
            raise ValueError(f"Local arithmetic failed for task {task['task_id']}.")
        raw_task_subtotals.append(round(task_score, 10))
        task_results.append({
            "task_id": task["task_id"],
            "title": task["title"],
            "agent_score": None,
            "raw_task_subtotal": round(task_score, 10),
            "rounded_task_subtotal": None,
            "max_points": task["max_points"],
            "feedback": feedback[task["task_id"]],
            "criteria": criterion_results,
        })

    repeated_causes = {
        cause_id: uses
        for cause_id, uses in deduction_causes.items()
        if len({criterion_id for criterion_id, _ in uses}) > 1
        and not all(independent for _, independent in uses)
    }
    if repeated_causes:
        cause_id = sorted(repeated_causes)[0]
        raise ValueError(
            "Repeated deduction cause is not marked as independently scored: "
            f"{cause_id}."
        )

    declared_total = float(spec["project"]["total_points"])
    rounding = apply_rounding(
        raw_task_subtotals,
        [task["max_points"] for task in spec["tasks"]],
        declared_total,
        rounding_policy,
    )
    for task, rounded in zip(
        task_results,
        rounding["rounded_task_subtotals"],
    ):
        task["rounded_task_subtotal"] = rounded
        task["agent_score"] = rounded

    total = rounding["final_rounded_grade"]
    if total < 0 or total > declared_total:
        raise ValueError("Calculated total is outside the project point range.")

    return {
        "student_id": submission["student_id"],
        "total_agent_score": total,
        "raw_total_before_rounding": rounding["raw_total_before_rounding"],
        "rounded_task_total": rounding["rounded_task_total"],
        "final_rounded_grade": rounding["final_rounded_grade"],
        "total_rounding_adjustment": rounding["total_rounding_adjustment"],
        "rounding_policy": rounding["rounding_policy"],
        "total_instructor_score": None,
        "project_total_points": spec["project"]["total_points"],
        "tasks": task_results,
        "review_required": any_review,
        "review_reasons": list(dict.fromkeys(all_review_reasons)),
    }
