"""
Itemized deductions for the student's final comment.

The instructor wants every lost point explained in the comment that goes to
Brightspace, with the numbers adding up to the grade:

    Points lost: README has no final recommendation (-0.5); constrained plot
    has no legend (-0.5). Rounded up (+1). Late submission (-20). Grade:
    80/100. <short comment>

generate-report writes a `deductions` column (short labels written by the
model, points taken from the grading run and shown in steps of 0.5) that the
instructor can edit; finalize-grading turns it into the comment and adds the
lines that make the numbers add up: the rounding in the student's favor and
any change the instructor made to the total (labeled with
`adjustment_reason`, e.g. "Late submission").

Points always come from the grading run, never from the model. Labels are
anonymous model output, checked for internal grading language; a label that
fails the checks falls back to the rule's own feedback text.
"""

import json
import math
import re
from pathlib import Path

from ai_grader.ai_client import get_client
from ai_grader.grade_adjustment import _call, _sha256
from ai_grader.student_language import internal_terms


REPOSITORY = Path(__file__).resolve().parents[2]
LABEL_PROMPT = REPOSITORY / "prompts" / "deduction_label_instructions.md"
LABEL_CACHE_FILENAME = "deduction_labels.json"
LABEL_MAX_WORDS = 15
LABEL_BATCH = 10
# The instructor's choice (MSPE 49600 P2): deductions shown in steps of half a
# point, rounded to the nearest step (ties up), never shown as 0.
DISPLAY_STEP = 0.5
FORBIDDEN_LABEL_TERMS = ("artifact_", "student_", "<student_name>", "<username>",
                         "rule_id", "criterion", "rubric")
ITEM = re.compile(r"^(?P<label>.*?)\s*\(\s*(?P<sign>[-+−]?)\s*(?P<value>\d+(?:\.\d+)?)\s*\)\s*$")


def display_points(points, step=DISPLAY_STEP):
    """A deduction as shown to the student: nearest multiple of step, ties up, at least step."""
    units = math.floor(round(float(points) / step, 9) + 0.5)
    return max(1, units) * step


def number(value):
    """1, 0.5, 20.25: no trailing zeros."""
    return f"{value:g}"


def deduction_items(result, spec):
    """
    One student's deductions in task and criterion order:
    [{"task": title, "rule_id", "points", "reason", "feedback"}].
    """
    titles = {task["task_id"]: task["title"] for task in spec.get("tasks", [])}
    templates = {rule["rule_id"]: rule.get("feedback_template", "")
                 for task in spec.get("tasks", []) for criterion in task["criteria"]
                 for rule in criterion.get("deduction_rules", [])}
    items = []
    for task in result.get("tasks", []):
        for criterion in task.get("criteria", []):
            for deduction in criterion.get("deductions", []):
                items.append({
                    "task": titles.get(task.get("task_id"), task.get("title", "")),
                    "rule_id": deduction.get("rule_id", ""),
                    "points": float(deduction["points"]),
                    "reason": deduction.get("reason", ""),
                    "feedback": templates.get(deduction.get("rule_id"), ""),
                })
    return items


def label_problem(label):
    """Why a label can't be shown to the student, or None."""
    text = (label or "").strip()
    if not text:
        return "empty"
    if "\n" in text:
        return "has line breaks"
    if len(text.split()) > LABEL_MAX_WORDS:
        return "too long"
    if "(" in text or ")" in text or ";" in text:
        return "has brackets or semicolons"
    lowered = text.casefold()
    found = [term for term in FORBIDDEN_LABEL_TERMS if term in lowered] + internal_terms(text)
    if found:
        return "mentions " + ", ".join(found)
    return None


def fallback_label(item):
    """The rule's student-facing feedback, first sentence, shortened."""
    for source in (item.get("feedback"), item.get("reason")):
        text = re.split(r"(?<=[.!?])\s", (source or "").strip())[0].rstrip(" .!?")
        text = re.sub(r"[();]", "", text)
        words = text.split()
        if words:
            label = " ".join(words[:LABEL_MAX_WORDS])
            if label_problem(label) is None:
                return label
    return f"{item.get('task') or 'Requirement'} incomplete"


def deduction_labels(run_path, inputs, client, model, use_model=True):
    """
    {student_id: [label per deduction]}, the fallback reasons, and usage.
    inputs: {student_id: deduction_items(...)}. Model labels are cached in
    the run folder by input and model, like the short comments.
    """
    cache_path = Path(run_path) / LABEL_CACHE_FILENAME
    cache = (json.loads(cache_path.read_text(encoding="utf-8"))
             if cache_path.is_file() else {"labels": {}})
    labels, usages, fallbacks, wanted = {}, [], {}, []
    for student_id, items in sorted(inputs.items()):
        if not items:
            labels[student_id] = []
            continue
        visible = [{"index": index, "task": item["task"], "what_was_wrong": item["reason"],
                    "feedback": item["feedback"]} for index, item in enumerate(items)]
        key = _sha256({"items": visible, "model": model})
        cached = cache["labels"].get(student_id)
        if use_model and cached and cached.get("key") == key:
            labels[student_id] = cached["labels"]
        elif use_model:
            wanted.append((student_id, items, visible, key))
        else:
            labels[student_id] = [fallback_label(item) for item in items]
    if wanted:
        client = client or get_client()
        schema = {"type": "object", "additionalProperties": False, "required": ["students"],
                  "properties": {"students": {"type": "array", "items": {
                      "type": "object", "additionalProperties": False,
                      "required": ["student_id", "labels"],
                      "properties": {
                          "student_id": {"type": "string"},
                          "labels": {"type": "array", "items": {
                              "type": "object", "additionalProperties": False,
                              "required": ["index", "label"],
                              "properties": {"index": {"type": "integer"},
                                             "label": {"type": "string"}}}}}}}}}
        for start in range(0, len(wanted), LABEL_BATCH):
            batch = wanted[start:start + LABEL_BATCH]
            payload = {"students": [{"student_id": sid, "deductions": visible}
                                    for sid, _, visible, _ in batch]}
            received = {}
            try:
                text, usage = _call(client, model, LABEL_PROMPT, payload, schema,
                                    "deduction_labels")
                usages.append(usage)
                for student in json.loads(text or "{}").get("students", []):
                    received[student.get("student_id")] = {
                        item.get("index"): item.get("label", "")
                        for item in student.get("labels", [])}
            except Exception as error:      # Keep going with the fallback labels...
                if getattr(error, "status_code", None) in {401, 403, 429}:
                    raise                       # ...but not without access or credits.
            for student_id, items, _, key in batch:
                chosen = []
                for index, item in enumerate(items):
                    label = (received.get(student_id, {}).get(index) or "").strip().rstrip(".")
                    problem = label_problem(label)
                    if problem:
                        fallbacks[problem] = fallbacks.get(problem, 0) + 1
                        label = fallback_label(item)
                    chosen.append(label)
                labels[student_id] = chosen
                cache["labels"][student_id] = {"key": key, "model": model, "labels": chosen}
        temporary = cache_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")
        temporary.replace(cache_path)
    return labels, fallbacks, usages


def deductions_cell(items, labels):
    """The review-sheet cell: 'label (-0.5); label (-1)'."""
    return "; ".join(f"{label} (-{number(display_points(item['points']))})"
                     for item, label in zip(items, labels))


def parse_deductions_cell(text):
    """
    [(label, signed value)] from a deductions cell, and whether every item
    could be read. Values are deductions, so a bare number counts as negative.
    """
    parts = [part.strip() for part in (text or "").split(";") if part.strip()]
    items, readable = [], True
    for part in parts:
        match = ITEM.match(part)
        if not match or not match.group("label"):
            readable = False
            items.append((part, None))
            continue
        value = float(match.group("value"))
        sign = 1 if match.group("sign") == "+" else -1
        items.append((match.group("label").strip(), sign * value))
    return items, readable


def itemized_comment(cell, calculated_grade, approved_total, final_grade, maximum,
                     comment, adjustment_reason=None):
    """
    The student's comment: the itemized deductions, the lines that make the
    numbers add up to the final grade, the grade, then the short comment.
    Returns (text, problem): problem is None, or why the numbers could not be
    made to add up (an item that isn't 'label (-points)').

    calculated_grade: the grade from the task scores (rounded up by policy);
    approved_total: the instructor's grade; final_grade: after any final
    adjustment round (curve).
    """
    items, readable = parse_deductions_cell(cell)
    comment = (comment or "").strip()
    instructor_change = round(approved_total - calculated_grade, 6)
    curve = round(final_grade - approved_total, 6)
    if not items and calculated_grade < maximum - 1e-6:
        # Points were lost but none are itemized (an empty cell): nothing to
        # explain them with, so the comment stays as written.
        return comment, None
    if not items and abs(instructor_change) < 1e-6 and abs(curve) < 1e-6:
        return comment, None
    parts = []
    if items:
        shown = "; ".join(label if value is None else f"{label} ({number(value)})"
                          for label, value in items)
        parts.append(f"Points lost: {shown}.")
    problem = None
    if readable:
        balance = round(calculated_grade - (maximum + sum(value for _, value in items)), 6)
        if abs(balance) >= 1e-6:
            word = "Rounded up" if balance > 0 else "Rounding"
            parts.append(f"{word} ({balance:+g}).")
    else:
        problem = "an item is not written as 'label (-points)'"
    if abs(instructor_change) >= 1e-6:
        reason = (adjustment_reason or "").strip().rstrip(".") or "Instructor adjustment"
        parts.append(f"{reason} ({instructor_change:+g}).")
    if abs(curve) >= 1e-6:
        parts.append(f"Final adjustment ({curve:+g}).")
    parts.append(f"Grade: {number(final_grade)}/{number(maximum)}.")
    if comment:
        parts.append(comment)
    return " ".join(parts), problem
