"""
Deterministic final grade adjustments (curves, mappings, overrides, comment
openers) applied to reviewed grades.

The plan format is schemas/grade_adjustment_plan.schema.json. The model only
translates the instructor's words into a plan; every number is computed here,
so the same plan and grades always give the same result.

Curves are order-preserving equations:

- add:    new = old + P
- scale:  new = old + F x (max - old)  =  (1 - F) x old + F x max
          (each student gains the same share of the points they were missing;
          a perfect score stays perfect)
- linear: new = a x old + b  (an equation the instructor gives)

With solve_for_targets, the smallest P or F that meets every target (lowest
grade, mean, median, a student's grade) is used, so the curve is never larger
than asked. Curved grades are rounded up, capped at the maximum and never
below the grade before the curve. Score mappings (99 -> 100) only raise
grades. Instructor overrides for one student are applied last and may set any
grade from 0 to the maximum, always with a reason.
"""

import difflib
import json
import math
import re
from pathlib import Path

from jsonschema import Draft202012Validator


TOLERANCE = 1e-9
ROUNDING = {"whole": 1.0, "half": 0.5, "none": None}
PLAN_SCHEMA_PATH = (Path(__file__).resolve().parents[2] / "schemas"
                    / "grade_adjustment_plan.schema.json")
STUDENT_ID = re.compile(r"Student_[0-9]{3}")
LETTER = r"[^\W\d_]"


class AdjustmentError(ValueError):
    pass


def empty_plan():
    return {
        "summary": [],
        "curve": {"method": "none", "solve_for_targets": False, "points": None,
                  "fraction": None, "slope": None, "intercept": None},
        "targets": {"min_grade": None, "mean": None, "median": None, "student_floors": []},
        "score_mappings": [],
        "student_overrides": [],
        "rounding": "whole",
        "comment_rules": [],
        "student_comments": [],
        "not_supported": [],
    }


def plan_schema():
    return json.loads(PLAN_SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_plan(plan, student_ids, maximum):
    """Check the plan's shape and meaning. Raises AdjustmentError listing every problem."""
    problems = [error.message for error in
                Draft202012Validator(plan_schema()).iter_errors(plan)]
    if problems:
        raise AdjustmentError("The adjustment plan is malformed: " + "; ".join(problems[:5]))
    student_ids = set(student_ids)

    def number(value, label, low=0.0, high=maximum):
        if value is None:
            return
        if not math.isfinite(value) or not low - TOLERANCE <= value <= high + TOLERANCE:
            problems.append(f"{label} {value:g} is outside {low:g}-{high:g}.")

    def student(value, label):
        if not STUDENT_ID.fullmatch(value) or value not in student_ids:
            problems.append(f"{label} refers to an unknown student ({value}).")

    curve = plan["curve"]
    method = curve["method"]
    targets = plan["targets"]
    has_targets = any(targets[key] is not None for key in ("min_grade", "mean", "median")) \
        or bool(targets["student_floors"])
    if curve["solve_for_targets"]:
        if method not in ("add", "scale"):
            problems.append("Only an add or scale curve can be solved for targets.")
        if not has_targets:
            problems.append("The curve is to be solved for targets, but there are none.")
    elif method == "add" and curve["points"] is None:
        problems.append("An add curve needs points (or solve_for_targets).")
    elif method == "scale" and curve["fraction"] is None:
        problems.append("A scale curve needs a fraction (or solve_for_targets).")
    elif method == "linear" and (curve["slope"] is None or curve["intercept"] is None):
        problems.append("A linear curve needs a slope and an intercept.")
    number(curve["points"], "Curve points", 0, maximum)
    number(curve["fraction"], "Curve fraction", 0, 1)
    if curve["slope"] is not None and (not math.isfinite(curve["slope"]) or curve["slope"] <= 0):
        problems.append("A linear curve's slope must be positive, so students keep their order.")
    if curve["intercept"] is not None and not math.isfinite(curve["intercept"]):
        problems.append("A linear curve's intercept must be a finite number.")
    for key in ("min_grade", "mean", "median"):
        number(targets[key], f"Target {key.replace('_', ' ')}")
    for floor in targets["student_floors"]:
        student(floor["student_id"], "A student target")
        number(floor["at_least"], f"Target for {floor['student_id']}")
    for mapping in plan["score_mappings"]:
        number(mapping["from_min"], "Mapping start")
        number(mapping["from_max"], "Mapping end")
        number(mapping["to"], "Mapped grade")
        if mapping["from_max"] < mapping["from_min"]:
            problems.append("A score mapping's range is reversed.")
        elif mapping["to"] < mapping["from_max"] - TOLERANCE:
            problems.append(f"Mapping {mapping['from_min']:g}-{mapping['from_max']:g} -> "
                            f"{mapping['to']:g} would lower grades; use a student override.")
    for override in plan["student_overrides"]:
        student(override["student_id"], "An override")
        if (override["set_grade"] is None) == (override["add_points"] is None):
            problems.append(f"The override for {override['student_id']} needs exactly one "
                            "of set_grade or add_points.")
        number(override["set_grade"], f"Override grade for {override['student_id']}")
        if override["add_points"] is not None and not math.isfinite(override["add_points"]):
            problems.append(f"Override points for {override['student_id']} must be finite.")
        if not override["reason"].strip():
            problems.append(f"The override for {override['student_id']} needs a reason.")
    for rule in plan["comment_rules"]:
        number(rule["from_min"], "Comment rule start")
        number(rule["from_max"], "Comment rule end")
        if rule["from_max"] < rule["from_min"]:
            problems.append("A comment rule's range is reversed.")
        if not rule["opener"].strip():
            problems.append("A comment rule has no text.")
    for comment in plan["student_comments"]:
        student(comment["student_id"], "A student comment")
        if not comment["text"].strip():
            problems.append(f"The comment for {comment['student_id']} is empty.")
    if problems:
        raise AdjustmentError("The adjustment plan can't be applied: " + " ".join(problems))
    return plan


# ---------------------------------------------------------------------------
# Applying a plan
# ---------------------------------------------------------------------------

def _round_up(value, increment, maximum):
    if increment is None:
        return round(min(value, maximum), 6)
    return round(min(math.ceil((value - TOLERANCE) / increment) * increment, maximum), 6)


def _curve_value(base, maximum, method, parameter, curve):
    if method == "add":
        return base + parameter
    if method == "scale":
        return base + parameter * max(maximum - base, 0.0)
    if method == "linear":
        return curve["slope"] * base + curve["intercept"]
    return base


def _grades(order, base, plan, maximum, parameter):
    """(grades, set_by) for every student in order, with this curve parameter."""
    increment = ROUNDING[plan["rounding"]]
    method = plan["curve"]["method"]
    overrides = {}
    for override in plan["student_overrides"]:
        overrides.setdefault(override["student_id"], []).append(override)
    grades, set_by = {}, {}
    for student_id in order:
        before = base[student_id]
        grade = before
        rule = "none"
        if method != "none":
            curved = _round_up(_curve_value(before, maximum, method, parameter, plan["curve"]),
                               increment, maximum)
            if curved > before + TOLERANCE:
                grade, rule = curved, "curve"
        for mapping in plan["score_mappings"]:
            if (mapping["from_min"] - TOLERANCE <= before <= mapping["from_max"] + TOLERANCE
                    and mapping["to"] > grade + TOLERANCE):
                grade, rule = min(mapping["to"], maximum), "mapping"
        for override in overrides.get(student_id, []):
            if override["set_grade"] is not None:
                grade = override["set_grade"]
            else:
                grade = grade + override["add_points"]
            grade, rule = min(max(grade, 0.0), maximum), "override"
        grades[student_id] = round(grade, 6)
        set_by[student_id] = rule
    return grades, set_by


def _median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def statistics(values):
    values = list(values)
    return {"count": len(values), "min": min(values), "mean": round(sum(values) / len(values), 2),
            "median": _median(values), "max": max(values)}


def check_targets(grades, targets):
    values = list(grades.values())
    checks = []
    for key, label, achieved in (
            ("min_grade", "lowest grade", min(values)),
            ("mean", "class mean", sum(values) / len(values)),
            ("median", "median", _median(values))):
        if targets[key] is not None:
            checks.append({"target": label, "at_least": targets[key],
                           "achieved": round(achieved, 2),
                           "met": achieved >= targets[key] - TOLERANCE})
    for floor in targets["student_floors"]:
        achieved = grades[floor["student_id"]]
        checks.append({"target": f"{floor['student_id']} grade", "at_least": floor["at_least"],
                       "achieved": achieved, "met": achieved >= floor["at_least"] - TOLERANCE})
    return checks


def _solve(order, base, plan, maximum):
    """Smallest add points or scale fraction that meets every target."""
    method = plan["curve"]["method"]

    def meets(parameter):
        grades, _ = _grades(order, base, plan, maximum, parameter)
        return all(check["met"] for check in check_targets(grades, plan["targets"]))

    if meets(0.0):
        return 0.0
    if method == "add":
        step = ROUNDING[plan["rounding"]] or 0.01
        for count in range(1, int(math.ceil(maximum / step)) + 1):
            if meets(round(count * step, 6)):
                return round(count * step, 6)
    elif meets(1.0):
        low, high = 0.0, 1.0
        for _ in range(60):
            middle = (low + high) / 2
            if meets(middle):
                high = middle
            else:
                low = middle
        return high
    raise AdjustmentError(
        "No curve of this kind meets all the targets without going above the maximum "
        "(or a student override keeps a target out of reach). Lower a target, change "
        "an override, or choose another kind of curve.")


def describe_curve(plan, parameter, maximum):
    curve = plan["curve"]
    method = curve["method"]
    if method == "none" or (method in ("add", "scale") and parameter == 0):
        return "no curve", "new = old"
    if method == "add":
        return (f"+{parameter:g} points for every student",
                f"new = old + {parameter:g}  (at most {maximum:g})")
    if method == "scale":
        return (f"every student gains {parameter * 100:.1f}% of the points they were missing",
                f"new = {1 - parameter:.4f} x old + {parameter * maximum:.2f}  "
                f"(at most {maximum:g})")
    return ("linear equation",
            f"new = {curve['slope']:g} x old + {curve['intercept']:g}  (at most {maximum:g})")


def opener_for(before, after, rules):
    for rule in rules:
        grade = before if rule["based_on"] == "before" else after
        if rule["from_min"] - TOLERANCE <= grade <= rule["from_max"] + TOLERANCE:
            return rule["opener"].strip()
    return ""


def apply_plan(base, plan, maximum):
    """
    Apply a validated plan to {student_id: grade before adjustment}. Returns
    the curve parameter, its description and equation, per-student results
    (grade, change, set_by, opener, extra comment), target checks, and
    statistics before and after. Anonymous IDs only.
    """
    order = sorted(base)
    curve = plan["curve"]
    if curve["solve_for_targets"]:
        parameter = _solve(order, base, plan, maximum)
    elif curve["method"] == "add":
        parameter = curve["points"]
    elif curve["method"] == "scale":
        parameter = curve["fraction"]
    else:
        parameter = 0.0
    grades, set_by = _grades(order, base, plan, maximum, parameter)
    description, equation = describe_curve(plan, parameter, maximum)
    extra = {}
    for comment in plan["student_comments"]:
        extra.setdefault(comment["student_id"], []).append(comment)
    students = []
    for student_id in order:
        before, after = base[student_id], grades[student_id]
        students.append({
            "student_id": student_id,
            "grade_before_adjustment": before,
            "adjusted_grade": after,
            "change": round(after - before, 6),
            "set_by": set_by[student_id],
            "opener": opener_for(before, after, plan["comment_rules"]),
            "student_comments": extra.get(student_id, []),
        })
    reordered = sum(1 for a in order for b in order
                    if base[a] < base[b] - TOLERANCE and grades[a] > grades[b] + TOLERANCE)
    return {
        "parameter": parameter,
        "description": description,
        "equation": equation,
        "students": students,
        "checks": check_targets(grades, plan["targets"]),
        "before": statistics(base.values()),
        "after": statistics(grades.values()),
        "reordered_pairs": reordered,
    }


def compose_comment(short_comment, opener="", student_comments=()):
    """Opener + short comment, then any student-specific text (or its replacement)."""
    replacement = [c["text"].strip() for c in student_comments if c["replace"]]
    if replacement:
        return replacement[-1]
    parts = [opener.strip(), short_comment.strip(),
             *[c["text"].strip() for c in student_comments]]
    return " ".join(part for part in parts if part)


# ---------------------------------------------------------------------------
# Student names in the instructor's instructions
# ---------------------------------------------------------------------------

def _name_parts(full_name):
    return [part for part in re.split(r"[\s\-]+", full_name or "")
            if sum(character.isalpha() for character in part) >= 2]


def anonymize_instructions(text, students):
    """
    Replace student names and usernames in the instructions with Student_###
    before anything is sent to the model. students: list of (student_id,
    full_name, username). Full names, usernames and single name parts are
    matched as whole words, ignoring case; a name part shared by several
    students is refused (ask for the full name). A capitalized word that
    closely resembles exactly one student's name part (a typo such as one
    letter missing) is read as that student and reported as "closest".
    Returns (text, readings) with readings [{"student_id", "match"}].
    """
    # Full names and usernames match in any case. A single name part must be
    # capitalized as a name, so "will" or "mark" in a sentence is not a student.
    tokens = {}
    for student_id, full_name, username in students:
        for token in (full_name, username):
            if token and token.strip():
                tokens.setdefault((token.strip().casefold(), True), set()).add(student_id)
        for part in _name_parts(full_name):
            name = part[0].upper() + part[1:]
            tokens.setdefault((name, False), set()).add(student_id)
    readings = []
    for token, any_case in sorted(tokens, key=lambda item: (-len(item[0]), item)):
        pattern = re.compile(f"(?<![\\w]){re.escape(token)}(?![\\w])",
                             re.IGNORECASE if any_case else 0)
        if not pattern.search(text):
            continue
        owners = tokens[(token, any_case)]
        if len(owners) > 1:
            raise AdjustmentError(
                f"A name in your instructions matches {len(owners)} students. Use the "
                "student's full name or Brightspace username.")
        owner = next(iter(owners))
        text = pattern.sub(owner, text)
        readings.append({"student_id": owner, "match": "exact"})

    parts = {}
    for student_id, full_name, _ in students:
        for part in _name_parts(full_name):
            if len(part) >= 4:
                parts.setdefault(part.casefold(), set()).add(student_id)

    def closest(match):
        word = match.group(0)
        scored = [(difflib.SequenceMatcher(None, word.casefold(), part).ratio(), owners)
                  for part, owners in parts.items()]
        scored = [(ratio, owners) for ratio, owners in scored if ratio >= 0.85]
        if not scored:
            return word
        best = max(ratio for ratio, _ in scored)
        owners = set().union(*[owners for ratio, owners in scored if ratio == best])
        if len(owners) != 1:
            return word
        owner = next(iter(owners))
        readings.append({"student_id": owner, "match": "closest"})
        return owner

    text = re.sub(rf"(?<![\w])[A-Z]{LETTER}{{3,}}(?![\w])", closest, text)
    unique = []
    for reading in readings:
        if reading not in unique:
            unique.append(reading)
    return text, unique
