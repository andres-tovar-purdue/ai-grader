"""
A course's standing grading policy, saved once and reused for every project.

The policy is a Markdown file the instructor (or Claude, from the
instructor's decisions) writes in plain words, kept in
projects\\_policies\\<course>.md, where <course> is the start of the project
name (mspe_49600 for mspe_49600_fa26_prj3). When a project is set up, it is
copied to the project's rubric\\grading_policy.md, where the rubric drafting
and the specification generation read it like any other rubric text.

A few settings are also read by code, written as "key: value" lines:

    minimum_attempted_fraction: 0.5   every attempted rubric item keeps at
                                      least this share of its points
    point_step: 0.5                   deductions shown to students in
                                      steps of this many points

minimum_attempted_fraction is enforced on the generated specification: each
item deduction becomes an "attempted but incomplete or incorrect" rule worth
that share and a "completely missing" rule worth all of it.
"""

import copy
import re
import shutil
from pathlib import Path


POLICIES_FOLDER = "_policies"
PROJECT_POLICY = Path("rubric") / "grading_policy.md"
SETTINGS = {"minimum_attempted_fraction": (0.0, 1.0), "point_step": (0.0, 100.0)}
SETTING_LINE = re.compile(r"^\s*[-*]?\s*`?(?P<key>[a-z_]+)`?\s*:\s*`?(?P<value>\d+(?:\.\d+)?)`?\s*$",
                          re.MULTILINE)
PARTIAL_SUFFIX, ABSENT_SUFFIX = "_partial", "_absent"


def course_name(project_name):
    """mspe_49600_fa26_prj3 -> mspe_49600 (the first two parts)."""
    parts = project_name.split("_")
    return "_".join(parts[:2]) if len(parts) >= 3 else project_name


def course_policy_path(project_path):
    project_path = Path(project_path)
    return project_path.parent / POLICIES_FOLDER / f"{course_name(project_path.name)}.md"


def project_policy_path(project_path):
    return Path(project_path) / PROJECT_POLICY


def install_policy(project_path):
    """
    Copy the course policy into the project if it has none yet. Returns the
    project's policy path, or None if there is no policy for this course.
    """
    target = project_policy_path(project_path)
    if target.is_file():
        return target
    source = course_policy_path(project_path)
    if not source.is_file():
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return target


def policy_text(project_path):
    path = project_policy_path(project_path)
    return path.read_text(encoding="utf-8-sig") if path.is_file() else ""


def policy_settings(text):
    """The known "key: value" settings in a policy text, within their ranges."""
    settings = {}
    for match in SETTING_LINE.finditer(text or ""):
        key, value = match.group("key"), float(match.group("value"))
        if key in SETTINGS:
            low, high = SETTINGS[key]
            if low < value <= high:
                settings[key] = value
    return settings


def enforce_attempt_minimum(spec, fraction):
    """
    Make every rubric item keep at least `fraction` of its points when it was
    attempted. Each technical deduction smaller than its criterion becomes
    two alternatives: "<id>_partial" (attempted but incomplete or incorrect,
    worth (1 - fraction) of the item) and "<id>_absent" (completely missing,
    the whole item). A criterion whose only rule takes all its points gets a
    "_partial" alternative too. Rules for missing or misnamed files are left
    as they are. Idempotent: criteria already split are skipped.
    Returns (new spec, number of items split).
    """
    spec = copy.deepcopy(spec)
    split = 0
    keep = 1.0 - fraction
    for task in spec.get("tasks", []):
        for criterion in task.get("criteria", []):
            rules = criterion.get("deduction_rules", [])
            if any(rule["rule_id"].endswith((PARTIAL_SUFFIX, ABSENT_SUFFIX)) for rule in rules):
                continue
            maximum = criterion["max_points"]
            technical = [rule for rule in rules if rule.get("basis", "technical") == "technical"]
            items = [rule for rule in technical if rule["deduction_points"] < maximum - 1e-9]
            new_rules = []
            for rule in rules:
                if rule not in items:
                    new_rules.append(rule)
                    continue
                partial, absent = copy.deepcopy(rule), copy.deepcopy(rule)
                partial["rule_id"] = rule["rule_id"] + PARTIAL_SUFFIX
                partial["deduction_points"] = round(rule["deduction_points"] * keep, 6)
                partial["condition"] = (
                    f"Item attempted but incomplete or incorrect (at least "
                    f"{fraction:.0%} of this item's points are kept): {rule['condition']}")
                absent["rule_id"] = rule["rule_id"] + ABSENT_SUFFIX
                absent["condition"] = ("Item completely missing, with no attempt at all: "
                                       + rule["condition"])
                new_rules += [partial, absent]
                split += 1
            whole = [rule for rule in technical if rule["deduction_points"] >= maximum - 1e-9]
            if not items and whole:
                rule = whole[0]
                partial = copy.deepcopy(rule)
                partial["rule_id"] = rule["rule_id"] + PARTIAL_SUFFIX
                partial["deduction_points"] = round(maximum * keep, 6)
                partial["condition"] = (
                    f"Attempted but incomplete or incorrect (at least {fraction:.0%} of the "
                    f"points are kept); the whole requirement is otherwise: {rule['condition']}")
                new_rules.append(partial)
                split += 1
            criterion["deduction_rules"] = new_rules
    if split:
        note = (f"AI Grader applied the course policy: every attempted rubric item keeps at "
                f"least {fraction:.0%} of its points ({split} items split into "
                f"'attempted but incomplete' and 'completely missing' rules).")
        spec["notes"] = f"{spec.get('notes', '').rstrip()} {note}".strip()
    return spec, split


def apply_policy(spec, settings):
    """The policy settings that change a specification. Returns (spec, items split)."""
    fraction = settings.get("minimum_attempted_fraction")
    if not fraction:
        return spec, 0
    return enforce_attempt_minimum(spec, fraction)
