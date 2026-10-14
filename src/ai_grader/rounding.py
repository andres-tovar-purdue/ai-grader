import math


ROUNDING_TOLERANCE = 1e-9
DEFAULT_ROUNDING_POLICY = "generous-v1"
ROUNDING_POLICIES = {
    "generous-v1": {
        "identifier": "generous-v1",
        "version": "1.0",
        "task_increment": 0.5,
        "final_increment": 1.0,
        "tolerance": ROUNDING_TOLERANCE,
    },
    "exact-v1": {
        "identifier": "exact-v1",
        "version": "1.0",
        "task_increment": None,
        "final_increment": None,
        "tolerance": ROUNDING_TOLERANCE,
    },
}


def get_rounding_policy(identifier):
    try:
        return dict(ROUNDING_POLICIES[identifier])
    except KeyError as error:
        raise ValueError(f"Unknown rounding policy: {identifier}") from error


def round_up_to_increment(value, increment, maximum, tolerance=ROUNDING_TOLERANCE):
    """Round upward while treating near-boundary floating values as exact."""
    value = float(value)
    maximum = float(maximum)
    if increment is None:
        return round(min(value, maximum), 10)
    rounded = math.ceil((value - tolerance) / increment) * increment
    return round(min(rounded, maximum), 10)


def apply_rounding(raw_task_subtotals, task_maxima, assignment_maximum, identifier):
    """Apply a versioned policy to raw task subtotals and the project total."""
    policy = get_rounding_policy(identifier)
    raw_tasks = [float(value) for value in raw_task_subtotals]
    maxima = [float(value) for value in task_maxima]
    if len(raw_tasks) != len(maxima):
        raise ValueError("Task subtotals and maxima do not align.")

    rounded_tasks = [
        round_up_to_increment(
            raw,
            policy["task_increment"],
            maximum,
            policy["tolerance"],
        )
        for raw, maximum in zip(raw_tasks, maxima)
    ]
    raw_total = round(sum(raw_tasks), 10)
    rounded_task_total = round(sum(rounded_tasks), 10)
    final_grade = round_up_to_increment(
        rounded_task_total,
        policy["final_increment"],
        assignment_maximum,
        policy["tolerance"],
    )
    return {
        "rounding_policy": policy,
        "raw_task_subtotals": raw_tasks,
        "rounded_task_subtotals": rounded_tasks,
        "raw_total_before_rounding": raw_total,
        "rounded_task_total": rounded_task_total,
        "final_rounded_grade": final_grade,
        "total_rounding_adjustment": round(final_grade - raw_total, 10),
    }
