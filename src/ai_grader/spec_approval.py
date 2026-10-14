import json
from datetime import datetime
from pathlib import Path

from ai_grader.spec_validation import (
    load_json,
    validate_grading_spec,
)


def approve_grading_spec(
    spec_path,
    schema_path,
    approved_by,
    allow_unresolved=False,
):
    """
    Approve an instructor-reviewed grading specification.

    By default, approval is blocked if unresolved ambiguities remain.
    """

    spec_path = Path(spec_path).resolve()
    schema_path = Path(schema_path).resolve()

    # First verify that the draft is structurally valid.
    validate_grading_spec(
        spec_path,
        schema_path,
    )

    spec = load_json(spec_path)

    if spec["status"] == "approved":
        raise RuntimeError(
            "This grading specification is already approved."
        )

    if spec["status"] != "draft":
        raise RuntimeError(
            "Only a draft grading specification can be approved."
        )

    unresolved = [
        ambiguity
        for ambiguity in spec.get("known_ambiguities", [])
        if ambiguity.get("status") == "unresolved"
    ]

    if unresolved and not allow_unresolved:
        lines = [
            "The grading specification contains unresolved ambiguities:"
        ]

        for ambiguity in unresolved:
            lines.append(
                f"- {ambiguity['ambiguity_id']}: "
                f"{ambiguity['description']}"
            )

        lines.append(
            "\nReview or resolve these ambiguities before approval, "
            "or explicitly allow unresolved ambiguities."
        )

        raise RuntimeError(
            "\n".join(lines)
        )

    # Record instructor approval.
    spec["status"] = "approved"

    spec["approval"] = {
        "approved_by": approved_by,
        "approved_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "notes": "Reviewed and approved for grading.",
    }

    # Remove "draft" language from the general note when present.
    notes = spec.get("notes", "")

    if notes.startswith(
        "This is a conservative draft grading specification"
    ):
        spec["notes"] = notes.replace(
            "This is a conservative draft grading specification",
            "This grading specification",
            1,
        )

    # Save.
    with spec_path.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            spec,
            file,
            indent=2,
        )
        file.write("\n")

    # Verify that approval did not make the file invalid.
    validate_grading_spec(
        spec_path,
        schema_path,
    )

    return spec_path, spec, unresolved