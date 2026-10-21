"""
Final grade adjustment rounds: the instructor's instructions -> a plan ->
a new instructor_review_v###.csv with adjusted grades and short comments.

Each round reads the latest review file of a run (or --from), takes every
student's grade before adjustment (total_instructor_score where the
instructor filled it, otherwise the AI's total_agent_score), and writes the
next review version with:

    grade_before_adjustment, grade_source, adjusted_grade, adjustment,
    adjusted_by, final_comment

next to the original review columns, plus a record
instructor_review_v###_adjustments.json (instructions, plan, curve equation,
target checks, statistics, model usage, the values written). Rounds are
cumulative: new instructions revise the previous round's plan, always applied
to the grades before adjustment, so curves never stack. Hand edits to
adjusted_grade or final_comment in the latest file are kept as instructor
overrides in the next round.

Only anonymous IDs and grades are sent to the model; student names in the
instructions are replaced with IDs first (adjustments.anonymize_instructions).
"""

import copy
import csv
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from ai_grader.adjustments import (
    AdjustmentError, anonymize_instructions, apply_plan, compose_comment, empty_plan,
    plan_schema, upgrade_plan, validate_plan,
)
from ai_grader.ai_client import get_client, resolve_model
from ai_grader.student_language import internal_terms, remove_instructor_notes


REPOSITORY = Path(__file__).resolve().parents[2]
INTERPRETATION_PROMPT = REPOSITORY / "prompts" / "grade_adjustment_instructions.md"
COMMENT_PROMPT = REPOSITORY / "prompts" / "short_comment_instructions.md"
COMMENT_CACHE_FILENAME = "short_comments.json"
REVIEW_PATTERN = re.compile(r"instructor_review_v([0-9]{3})\.csv")
ADJUSTMENT_COLUMNS = ["adjusted_grade", "adjustment", "adjusted_by", "final_comment"]
# Written by earlier versions; dropped from new review files (kept in the record).
LEGACY_COLUMNS = ("grade_before_adjustment", "grade_source")
# Rounding detail stays in preliminary_grading_report.csv and grading_results.json.
HIDDEN_COLUMNS = ("raw_total_before_rounding", "total_rounding_adjustment")
GRADE_COLUMNS = ["student_name", "student_id", "total_agent_score", "total_instructor_score"]
REQUIRED_REVIEW_COLUMNS = {"student_id", "total_agent_score", "total_instructor_score"}
COMMENT_BATCH = 10
COMMENT_MAX_WORDS = 60
FORBIDDEN_COMMENT_TERMS = ("student_", "<student_name>", "rubric id")


def _number(value):
    """A finite number from a CSV cell, or None for a blank cell."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return float("nan")
    return number


def _sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Review files
# ---------------------------------------------------------------------------

def review_versions(run_path):
    found = []
    for path in Path(run_path).glob("instructor_review_v[0-9][0-9][0-9].csv"):
        match = REVIEW_PATTERN.fullmatch(path.name)
        if match:
            found.append((int(match.group(1)), path))
    return [path for _, path in sorted(found)]


def review_fieldnames(fields, adjusted):
    """
    Review file column order: who, the AI grade, your grade, the adjusted
    grade (after an adjustment round), the comment, then the details.
    """
    lead = [name for name in GRADE_COLUMNS if name in fields or name == "student_name"]
    middle = ["adjusted_grade", "adjustment", "adjusted_by"] if adjusted else []
    skip = (set(lead) | set(ADJUSTMENT_COLUMNS) | set(LEGACY_COLUMNS) | set(HIDDEN_COLUMNS)
            | {"flag"})
    return (lead + ["flag"] + middle + ["final_comment"]
            + [name for name in fields if name not in skip])


def review_flag(row):
    """
    What needs your attention for one student: notes the grader wrote for you
    inside the task feedback (they are removed from what students see) and
    the grader's review reasons. Empty when there is nothing to check.
    """
    notes = []
    number = 1
    while f"task_{number}_feedback" in row:
        _, removed = remove_instructor_notes(row.get(f"task_{number}_feedback") or "")
        notes.extend(f"Task {number}: {note}" for note in removed)
        number += 1
    if str(row.get("review_required", "")).strip().casefold() == "true":
        reasons = (row.get("review_reasons") or "").strip()
        notes.append(reasons or "The grader asked for a review.")
    return " | ".join(notes)


def sidecar_path(review_path):
    review_path = Path(review_path)
    return review_path.with_name(review_path.stem + "_adjustments.json")


def read_review(path):
    path = Path(path)
    if not path.is_file():
        raise AdjustmentError(f"Review file not found: {path}. Run the command from the "
                              "workspace folder (the one that contains projects\\).")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    missing = REQUIRED_REVIEW_COLUMNS - set(fields)
    if missing:
        raise AdjustmentError(f"{path.name} is missing columns: {', '.join(sorted(missing))}.")
    ids = [row["student_id"] for row in rows]
    if len(set(ids)) != len(ids) or not all(re.fullmatch(r"Student_[0-9]{3}", i) for i in ids):
        raise AdjustmentError(f"{path.name} has missing, invalid or duplicate student IDs.")
    if any(None in row for row in rows):
        raise AdjustmentError(f"{path.name} has malformed rows (extra cells).")
    return fields, rows


def base_grades(rows, maximum):
    """
    {student_id: (grade before adjustment, 'instructor'|'ai')}. The
    instructor's total_instructor_score wins; otherwise the AI total, which
    must be a validated score.
    """
    base, problems = {}, []
    for row in rows:
        student_id = row["student_id"]
        instructor = _number(row.get("total_instructor_score"))
        if instructor is not None:
            value, source = instructor, "instructor"
        else:
            if row.get("validation_status", "validated") == "invalid_model_response":
                problems.append(f"{student_id} has no valid AI score; enter "
                                "total_instructor_score for it")
                continue
            value, source = _number(row.get("total_agent_score")), "ai"
        if value is None or not math.isfinite(value) or not 0 <= value <= maximum:
            problems.append(f"{student_id} has no usable grade (0-{maximum:g})")
            continue
        base[student_id] = (value, source)
    if problems:
        raise AdjustmentError("Can't adjust yet: " + "; ".join(problems) + ".")
    return base


def hand_edits(review_path, rows):
    """
    Changes the instructor made by hand to adjusted_grade or final_comment in
    an adjusted review file, compared with what AI Grader wrote there.
    """
    record_path = sidecar_path(review_path)
    if not record_path.is_file():
        return {}, {}
    written = {item["student_id"]: item for item in
               json.loads(record_path.read_text(encoding="utf-8"))["written"]}
    grades, comments = {}, {}
    for row in rows:
        before = written.get(row["student_id"])
        if before is None:
            continue
        grade = _number(row.get("adjusted_grade"))
        if grade is not None and math.isfinite(grade) and abs(grade - before["adjusted_grade"]) > 1e-9:
            grades[row["student_id"]] = grade
        comment = (row.get("final_comment") or "").strip()
        if comment and comment != before["final_comment"].strip():
            comments[row["student_id"]] = comment
    return grades, comments


# ---------------------------------------------------------------------------
# Model calls
# ---------------------------------------------------------------------------

def _strict(schema):
    schema = copy.deepcopy(schema)
    for key in ("$schema", "$id", "$comment", "title"):
        schema.pop(key, None)
    return schema


def _usage(response):
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    return {"input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0)}


def _call(client, model, prompt_path, payload, schema, name):
    response = client.responses.create(
        model=model,
        instructions=Path(prompt_path).read_text(encoding="utf-8"),
        input=json.dumps(payload, indent=1),
        text={"format": {"type": "json_schema", "name": name,
                         "schema": _strict(schema), "strict": True}},
        store=False,
    )
    return response.output_text, _usage(response)


def interpret_instructions(new_instructions, previous_instructions, previous_plan,
                           base, maximum, client, model):
    """Ask the model for the complete plan for this round (anonymous input only)."""
    grades = [value for value, _ in base.values()]
    ordered = sorted(grades)
    middle = len(ordered) // 2
    payload = {
        "maximum_points": maximum,
        "students": [{"student_id": student_id, "grade_before_adjustment": base[student_id][0]}
                     for student_id in sorted(base)],
        "statistics": {"count": len(grades), "lowest": min(grades),
                       "mean": round(sum(grades) / len(grades), 2),
                       "median": ordered[middle] if len(ordered) % 2
                       else (ordered[middle - 1] + ordered[middle]) / 2,
                       "highest": max(grades)},
        "previous_plan": previous_plan,
        "previous_instructions": previous_instructions,
        "new_instructions": new_instructions,
    }
    text, usage = _call(client, model, INTERPRETATION_PROMPT, payload, plan_schema(),
                        "grade_adjustment_plan")
    try:
        plan = json.loads(text or "")
    except json.JSONDecodeError:
        raise AdjustmentError("The model's plan was not valid JSON; nothing was changed. "
                              "Run the command again.") from None
    return plan, usage


def template_comment(tasks):
    """Deterministic fallback: the strongest task and where points were lost."""
    tasks = [task for task in tasks if task["max_points"] > 0]
    if not tasks:
        return ""
    lost = sorted((task for task in tasks if task["score"] < task["max_points"] - 1e-9),
                  key=lambda task: task["max_points"] - task["score"], reverse=True)
    if not lost:
        return "Every part of the assignment met the requirements."
    if len(tasks) == 1:
        return f"Points were lost in {tasks[0]['title']}; see the detailed feedback."
    strongest = max(tasks, key=lambda task: (task["score"] / task["max_points"],
                                             task["max_points"]))
    titles = [task["title"] for task in lost if task is not strongest][:2]
    joined = titles[0] if len(titles) == 1 else " and ".join(titles)
    return (f"Your strongest work was in {strongest['title']}. "
            f"Most points were lost in {joined}.")


def comment_problem(comment):
    """Why a model-written comment can't be used, or None."""
    text = (comment or "").strip()
    if not text:
        return "empty"
    if "\n" in text:
        return "has line breaks"
    if len(text.split()) > COMMENT_MAX_WORDS:
        return "too long"
    lowered = text.casefold()
    found = [term for term in FORBIDDEN_COMMENT_TERMS if term in lowered] + internal_terms(text)
    if found:
        return "mentions " + ", ".join(found)
    return None


def comment_inputs(run, spec):
    """Anonymous itemized scores and feedback per student, from the grading run."""
    titles = {task["task_id"]: task["title"] for task in spec["tasks"]}
    inputs = {}
    for result in run["results"]:
        tasks = [{"title": titles.get(task["task_id"], task.get("title", task["task_id"])),
                  "score": task.get("agent_score"),
                  "max_points": task["max_points"],
                  "feedback": remove_instructor_notes(task.get("feedback", ""))[0]}
                 for task in result.get("tasks", [])]
        inputs[result["student_id"]] = tasks
    return inputs


def short_comments(run_path, inputs, client, model, use_model=True):
    """
    {student_id: (comment, 'ai'|'template')}. Model comments are cached in
    the run folder by input and model, so later rounds cost nothing.
    Invalid or missing model comments fall back to the template.
    Returns (comments, usage list, fallback count).
    """
    cache_path = Path(run_path) / COMMENT_CACHE_FILENAME
    cache = (json.loads(cache_path.read_text(encoding="utf-8"))
             if cache_path.is_file() else {"comments": {}})
    comments, usages, fallbacks = {}, [], {}
    wanted = []
    for student_id, tasks in sorted(inputs.items()):
        usable = [task for task in tasks if isinstance(task["score"], (int, float))]
        key = _sha256({"tasks": usable, "model": model})
        cached = cache["comments"].get(student_id)
        if use_model and cached and cached.get("key") == key:
            comments[student_id] = (cached["comment"], "ai")
        elif use_model and usable:
            wanted.append((student_id, usable, key))
        else:
            comments[student_id] = (template_comment(usable), "template")
    if wanted:
        client = client or get_client()
        schema = {"type": "object", "additionalProperties": False, "required": ["comments"],
                  "properties": {"comments": {"type": "array", "items": {
                      "type": "object", "additionalProperties": False,
                      "required": ["student_id", "comment"],
                      "properties": {"student_id": {"type": "string"},
                                     "comment": {"type": "string"}}}}}}
        for start in range(0, len(wanted), COMMENT_BATCH):
            batch = wanted[start:start + COMMENT_BATCH]
            payload = {"students": [{"student_id": sid, "tasks": tasks}
                                    for sid, tasks, _ in batch]}
            received = {}
            try:
                text, usage = _call(client, model, COMMENT_PROMPT, payload, schema,
                                    "short_comments")
                usages.append(usage)
                for item in json.loads(text or "{}").get("comments", []):
                    received[item.get("student_id")] = item.get("comment", "")
            except Exception as error:      # Keep going with the template...
                if getattr(error, "status_code", None) in {401, 403, 429}:
                    raise                       # ...but not without access or credits.
            for student_id, tasks, key in batch:
                comment = (received.get(student_id) or "").strip()
                if comment_problem(comment) is None:
                    comments[student_id] = (comment, "ai")
                    cache["comments"][student_id] = {"key": key, "model": model,
                                                     "comment": comment}
                else:
                    comments[student_id] = (template_comment(tasks), "template")
                    reason = comment_problem(comment) if comment else "no comment returned"
                    fallbacks[reason] = fallbacks.get(reason, 0) + 1
        temporary = cache_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")
        temporary.replace(cache_path)
    return comments, usages, fallbacks


# ---------------------------------------------------------------------------
# One round
# ---------------------------------------------------------------------------

def _load_spec(grader, run_path, run):
    snapshot = run_path / "grading_spec_snapshot.json"
    name = run.get("grading_spec", "grading_spec_v001.json")
    path = snapshot if snapshot.is_file() else grader / name
    return json.loads(path.read_text(encoding="utf-8"))


def _students_for_names(grader, rows):
    student_map = json.loads((grader / "student_map.json").read_text(encoding="utf-8-sig"))
    students = []
    for row in rows:
        entry = student_map.get(row["student_id"], {})
        students.append((row["student_id"], row.get("student_name") or entry.get("full_name", ""),
                         entry.get("username", "")))
    return students


def _next_review_path(run_path):
    versions = [int(REVIEW_PATTERN.fullmatch(path.name).group(1))
                for path in review_versions(run_path)]
    return Path(run_path) / f"instructor_review_v{max(versions, default=0) + 1:03d}.csv"


def _keep_hand_edits(plan, edited_grades, edited_comments, source):
    """
    Hand edits in the latest review file are the instructor's most recent
    decisions: they stay in the plan even if the model's revision left them
    out. (Edit the cell back, or use --restart, to undo one.)
    """
    plan = copy.deepcopy(plan)
    if edited_grades:
        plan["student_overrides"] = [
            item for item in plan["student_overrides"]
            if item["student_id"] not in edited_grades] + [
            {"student_id": sid, "set_grade": grade, "add_points": None,
             "relative_to": "after", "reason": f"Changed by hand in {source}"}
            for sid, grade in sorted(edited_grades.items())]
    if edited_comments:
        plan["student_comments"] = [
            item for item in plan["student_comments"]
            if item["student_id"] not in edited_comments] + [
            {"student_id": sid, "text": text, "replace": True}
            for sid, text in sorted(edited_comments.items())]
    return plan


def adjust_grades(project_path, run_version, instructions=None, plan=None,
                  source_review=None, restart=False, use_model_comments=True,
                  client=None, model=None):
    """
    One adjustment round. Give the instructor's words (instructions) or a
    ready plan (dict, e.g. from --plan-file). Writes the next
    instructor_review_v###.csv and its record; returns a summary with
    anonymous IDs only.
    """
    if (instructions is None) == (plan is None):
        raise AdjustmentError("Give either instructions or a plan file.")
    if not re.fullmatch(r"run_v[0-9]{3}", run_version or ""):
        raise AdjustmentError("Invalid grading run version.")
    grader = Path(project_path).resolve() / "grader"
    run_path = grader / "grading_runs" / run_version
    results_path = run_path / "grading_results.json"
    if not results_path.is_file():
        raise AdjustmentError(f"{run_version} was not found in {grader}. Run the command from "
                              "the workspace folder (the one that contains projects\\).")
    run = json.loads(results_path.read_text(encoding="utf-8"))
    spec = _load_spec(grader, run_path, run)
    maximum = float(spec["project"]["total_points"])

    if source_review is None:
        versions = review_versions(run_path)
        if not versions:
            raise AdjustmentError("This run has no review file yet. Run generate-report first.")
        source_review = versions[-1]
    source_review = Path(source_review)
    fields, rows = read_review(source_review)
    if {row["student_id"] for row in rows} != {r["student_id"] for r in run["results"]}:
        raise AdjustmentError(f"{source_review.name} and {run_version} list different students.")
    base = base_grades(rows, maximum)
    base_values = {student_id: value for student_id, (value, _) in base.items()}

    previous_record_path = sidecar_path(source_review)
    previous = (json.loads(previous_record_path.read_text(encoding="utf-8"))
                if previous_record_path.is_file() and not restart else None)
    previous_plan = upgrade_plan(copy.deepcopy(previous["plan"])) if previous else None
    history = list(previous["instruction_history"]) if previous else []
    edited_grades, edited_comments = hand_edits(source_review, rows) if not restart else ({}, {})
    if previous_plan is not None and (edited_grades or edited_comments):
        previous_plan = _keep_hand_edits(previous_plan, edited_grades, edited_comments,
                                         source_review.name)
        previous_plan["summary"] = previous_plan["summary"] + [
            f"Keep the hand edit to {sid}'s {what} from {source_review.name}."
            for sid, what in sorted({**{s: "grade" for s in edited_grades},
                                     **{s: "comment" for s in edited_comments}}.items())]

    usage = []
    readings = []
    selected_model = resolve_model(model)
    if instructions is not None:
        instructions = instructions.strip()
        if not instructions:
            raise AdjustmentError("The instructions are empty.")
        anonymous, readings = anonymize_instructions(
            instructions, _students_for_names(grader, rows))
        client = client or get_client()
        plan, call_usage = interpret_instructions(
            anonymous, [item["anonymized"] for item in history], previous_plan,
            base, maximum, client, selected_model)
        usage.append(call_usage)
        history.append({"round": _next_review_path(run_path).name,
                        "instructions": instructions, "anonymized": anonymous})
    else:
        history.append({"round": _next_review_path(run_path).name,
                        "instructions": "(plan file)", "anonymized": "(plan file)"})
    plan = _keep_hand_edits(upgrade_plan(plan), edited_grades, edited_comments,
                            source_review.name)
    validate_plan(plan, base_values, maximum)
    result = apply_plan(base_values, plan, maximum)

    comments, comment_usage, fallbacks = short_comments(
        run_path, comment_inputs(run, spec), client, selected_model, use_model_comments)
    usage.extend(comment_usage)

    output = _next_review_path(run_path)
    by_id = {student["student_id"]: student for student in result["students"]}
    written = []
    out_rows = []
    for row in rows:
        student = by_id[row["student_id"]]
        comment = compose_comment(comments[row["student_id"]][0], student["opener"],
                                  student["student_comments"])
        new_row = {key: value for key, value in row.items()
                   if key not in ADJUSTMENT_COLUMNS and key not in LEGACY_COLUMNS}
        new_row.update({
            "flag": review_flag(row),
            "adjusted_grade": f"{student['adjusted_grade']:g}",
            "adjustment": f"{student['change']:+g}",
            "adjusted_by": student["set_by"],
            "final_comment": comment,
        })
        out_rows.append(new_row)
        written.append({"student_id": row["student_id"],
                        "grade_before_adjustment": student["grade_before_adjustment"],
                        "grade_source": base[row["student_id"]][1],
                        "adjusted_grade": student["adjusted_grade"],
                        "final_comment": comment,
                        "comment_source": comments[row["student_id"]][1]})

    with output.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=review_fieldnames(fields, adjusted=True),
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(out_rows)

    record = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run": run_version,
        "source_review": source_review.name,
        "review": output.name,
        "model": selected_model if (instructions is not None or use_model_comments) else None,
        "instruction_history": history,
        "name_readings": readings,
        "hand_edits_kept": {"grades": sorted(edited_grades), "comments": sorted(edited_comments)},
        "plan": plan,
        "curve": {"description": result["description"], "equation": result["equation"],
                  "parameter": result["parameter"]},
        "checks": result["checks"],
        "attention": result["attention"],
        "before": result["before"],
        "after": result["after"],
        "reordered_pairs": result["reordered_pairs"],
        "comments": {"ai": sum(1 for c, s in comments.values() if s == "ai"),
                     "template": sum(1 for c, s in comments.values() if s == "template"),
                     "fallbacks": sum(fallbacks.values()),
                     "fallback_reasons": fallbacks},
        "usage": [item for item in usage if item],
        "written": written,
    }
    sidecar_path(output).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return {
        "path": output,
        "record_path": sidecar_path(output),
        "source_review": source_review.name,
        "plan": plan,
        "readings": readings,
        "hand_edits": record["hand_edits_kept"],
        "instructor_totals": sum(1 for _, source in base.values() if source == "instructor"),
        "comments": record["comments"],
        "maximum": maximum,
        **result,
    }


def review_decisions(review_path, maximum=None):
    """
    For finalization from a review file. Returns (approved_scores,
    decisions), or (None, None) for a plain scores CSV that is not a review
    file (student_id and total_instructor_score only).

    - A review file without an adjustment round: every student needs
      total_instructor_score (your grade; copy total_agent_score where you
      agree); that is the final grade, and final_comment (if present and
      filled) is the comment.
    - An adjusted review file: adjusted_grade is the final grade and
      final_comment the comment; total_instructor_score must not have
      changed since the round. Hand edits are allowed and reported.

    decisions[student_id]: final_grade, comment (None = build from the task
    scores), ai_grade, grade_edited_by_hand, comment_edited_by_hand.
    """
    review_path = Path(review_path)
    if not review_path.is_file():
        raise AdjustmentError(f"File not found: {review_path}. Run the command from the "
                              "workspace folder (the one that contains projects\\).")
    with review_path.open(encoding="utf-8-sig", newline="") as stream:
        header = next(csv.reader(stream), [])
    if "total_agent_score" not in header:
        return None, None
    fields, rows = read_review(review_path)
    adjusted = "adjusted_grade" in fields
    written = {}
    record_path = sidecar_path(review_path)
    if adjusted and record_path.is_file():
        written = {item["student_id"]: item for item in
                   json.loads(record_path.read_text(encoding="utf-8"))["written"]}
    approved, decisions, problems, blank = {}, {}, [], []
    for row in rows:
        student_id = row["student_id"]
        ai_grade = _number(row.get("total_agent_score"))
        instructor = _number(row.get("total_instructor_score"))
        comment = (row.get("final_comment") or "").strip() or None
        if instructor is not None and (not math.isfinite(instructor) or instructor < 0
                                       or (maximum is not None and instructor > maximum + 1e-9)):
            problems.append(f"{student_id}: total_instructor_score is not a valid grade")
            continue
        if not adjusted:
            if instructor is None:
                blank.append(student_id)
                continue
            approved[student_id] = instructor
            decisions[student_id] = {"final_grade": instructor, "comment": comment,
                                     "ai_grade": ai_grade, "grade_edited_by_hand": False,
                                     "comment_edited_by_hand": False}
            continue
        before = written.get(student_id)
        recorded = (before["grade_before_adjustment"] if before
                    else _number(row.get("grade_before_adjustment")))
        current = instructor if instructor is not None else ai_grade
        if recorded is None or current is None or not math.isfinite(current) \
                or abs(recorded - current) > 1e-9:
            problems.append(f"{student_id}: total_instructor_score changed after the "
                            "adjustment; run adjust-grades again so the curve uses it")
            continue
        final = _number(row["adjusted_grade"])
        if final is None or not math.isfinite(final) or final < 0 or \
                (maximum is not None and final > maximum + 1e-9):
            problems.append(f"{student_id}: adjusted_grade is missing or out of range")
            continue
        if comment is None:
            problems.append(f"{student_id}: final_comment is empty")
            continue
        approved[student_id] = recorded
        decisions[student_id] = {
            "final_grade": final,
            "comment": comment,
            "ai_grade": ai_grade,
            "grade_edited_by_hand": bool(before and abs(before["adjusted_grade"] - final) > 1e-9),
            "comment_edited_by_hand": bool(before and before["final_comment"].strip() != comment),
        }
    if blank:
        problems.insert(0, f"total_instructor_score is blank for {len(blank)} student(s) "
                           f"({', '.join(blank)}); enter your grade, or copy total_agent_score "
                           "where you agree with the AI")
    if problems:
        raise AdjustmentError("Can't finalize from " + review_path.name + ": "
                              + "; ".join(problems) + ".")
    return approved, decisions


def adjusted_review_decisions(review_path, maximum=None):
    """Earlier name of review_decisions."""
    return review_decisions(review_path, maximum)


def comments_for_run(project_path, run_version, client=None, model=None, use_model=False):
    """Short comments for every student of a run: ({id: (text, source)}, fallback reasons)."""
    grader = Path(project_path).resolve() / "grader"
    run_path = grader / "grading_runs" / run_version
    run = json.loads((run_path / "grading_results.json").read_text(encoding="utf-8-sig"))
    try:
        spec = _load_spec(grader, run_path, run)
    except (OSError, ValueError):
        spec = {"tasks": []}             # Task titles then come from the run itself.
    comments, _, fallbacks = short_comments(run_path, comment_inputs(run, spec), client,
                                            resolve_model(model), use_model)
    return comments, fallbacks
