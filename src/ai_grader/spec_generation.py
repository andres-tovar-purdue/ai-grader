import json
import os
import re
from pathlib import Path

from ai_grader.ai_client import get_client
from ai_grader.project_manifest import build_project_manifest
from ai_grader.source_extraction import extract_pdf
from ai_grader.spec_validation import validate_grading_spec


SOURCE_FOLDERS = {
    "project": "project_instructions",
    "rubric": "rubric",
    "reference": "reference_solution",
    "datasets": "dataset",
}

TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".json",
    ".csv",
    ".py",
    ".m",
}

PDF_EXTENSIONS = {
    ".pdf",
}


def make_source_id(relative_path):
    """
    Convert a relative file path into a stable source identifier.
    """

    source_id = relative_path.as_posix().lower()

    source_id = re.sub(
        r"[^a-z0-9]+",
        "_",
        source_id,
    )

    return source_id.strip("_")


def read_text_file(path):
    """
    Read a text-based project file.
    """

    return path.read_text(
        encoding="utf-8",
        errors="replace",
    )


def read_pdf_file(path):
    """
    Extract text from a PDF while preserving page labels.
    """

    pages = extract_pdf(path)

    return "\n\n".join(
        f"[Page {page['page']}]\n{page['text']}"
        for page in pages
        if page["text"]
    )


def collect_project_sources(project_path):
    """
    Collect instructor-controlled source materials used to
    generate the grading specification.

    Student submissions and grader-generated files are
    intentionally excluded.
    """

    project_path = Path(project_path).resolve()

    sources = []

    for folder_name, source_type in SOURCE_FOLDERS.items():
        folder_path = project_path / folder_name

        if not folder_path.exists():
            continue

        for path in sorted(folder_path.rglob("*")):
            if not path.is_file():
                continue

            extension = path.suffix.lower()

            if extension in TEXT_EXTENSIONS:
                content = read_text_file(path)
            elif extension in PDF_EXTENSIONS and folder_name == "project":
                content = read_pdf_file(path)
            else:
                continue

            relative_path = path.relative_to(project_path)

            sources.append(
                {
                    "source_id": make_source_id(relative_path),
                    "source_type": source_type,
                    "path": relative_path.as_posix(),
                    "content": content,
                }
            )

    return sources


def build_generation_prompt(
    project_path,
    sources,
    schema,
):
    """
    Build the prompt used to generate a draft grading specification.
    """

    project_path = Path(project_path).resolve()

    source_sections = []

    for source in sources:
        source_sections.append(
            "\n".join(
                [
                    "----------------------------------------",
                    f"SOURCE ID: {source['source_id']}",
                    f"SOURCE TYPE: {source['source_type']}",
                    f"PATH: {source['path']}",
                    "CONTENT:",
                    source["content"],
                ]
            )
        )

    source_text = "\n\n".join(source_sections)

    schema_text = json.dumps(
        schema,
        indent=2,
    )

    prompt = f"""
Create a DRAFT project-specific grading specification for an
engineering computing assignment.

PROJECT FOLDER NAME:
{project_path.name}

IMPORTANT RULES:

1. Use only the supplied project materials as authoritative
   evidence about what students were asked to do.

2. Treat the ORIGINAL PUBLISHED ASSIGNMENT material in project/
   as the primary authority for student requirements. Generated
   project_instructions.md, instructor rubric/guidance, and the
   reference solution may clarify grading, but they must not add
   requirements that are absent from the published assignment.

3. Do not invent grading requirements.

4. Instructor rubric/guidance may define grading policy and
   partial-credit rules when the published assignment does not
   specify criterion-level allocations.

5. A reference solution is evidence of one valid approach.
   Do not silently treat it as the only acceptable solution
   unless the published assignment requires that method.

6. Use known_ambiguities ONLY for unresolved grading decisions
   that require explicit instructor judgment before grading can
   be defensibly approved.

7. Do NOT create a blocking ambiguity merely because:
   - the assignment explicitly allows student choice;
   - multiple technically valid implementations are permitted;
   - an engineering judgment is sample-dependent and can be
     evaluated from the student's evidence;
   - a filename/export mechanism is not further specified but
     the required deliverable itself is clear; or
   - the JSON task structure differs from the assignment's
     section labels without changing points or requirements.

8. Represent non-blocking flexibility with acceptable_alternatives,
   evidence requirements, feedback guidance, or review triggers
   rather than known_ambiguities.

9. If the materials are genuinely contradictory, incomplete, or
   do not define enough information for a defensible grading rule,
   record the issue in known_ambiguities and do not silently resolve it.

10. Where criterion-level point allocations are not explicitly
    published, you may propose a reasonable draft allocation.
    Record that allocation as a known ambiguity ONLY when the
    instructor must explicitly accept or revise it before grading.

11. Set:
       schema_version = "1.0"
       spec_version = "0.1"
       status = "draft"

12. Use the exact SOURCE IDs supplied below when populating
    source_refs.

13. Criterion points must sum to their task max_points.

14. Task max_points must sum to project.total_points exactly.

15. If the published assignment assigns points to a separate
    Deliverables section, represent that scored section as a task
    in tasks in addition to listing the required files under
    deliverables. Do not leave published deliverable points outside
    the scored task total.

16. Include evidence requirements and review triggers when
    appropriate.

17. Preserve explicitly required methods, functions,
    software, filenames, variable ordering, plots, model
    structure, deliverables, and reproducibility requirements.

18. Allow technically valid alternatives when the published
    project does not prescribe a unique implementation.

19. Each numeric deduction rule needs a stable rule_id, exact deduction_points,
    and a clear condition. State in notes which deduction amounts are proposed
    instructor policy rather than explicitly published penalties. Set basis to
    filename and deliverable_id to the corresponding listed deliverable for every
    filename rule. Keep filename/missing-export penalties in one owning criterion;
    do not duplicate them in technical content criteria. New filenames are checked
    locally before anonymization. Use max_applications only for independently
    omitted requirements; rule amounts must not exceed the criterion maximum.

20. Preserve every separately scored reflection or interpretation component.
    PDFs and unsupported saved outputs may need manual instructor review; never
    create deductions for the grading tool's inability to inspect them.

21. Return ONLY a JSON object. Do not use Markdown fences,
    explanations, or commentary outside the JSON.

JSON SCHEMA:

{schema_text}

PROJECT MATERIALS:

{source_text}
"""

    return prompt.strip()


def parse_json_response(text):
    """
    Parse the model response as JSON.
    """

    text = text.strip()

    if text.startswith("```"):
        text = re.sub(
            r"^```(?:json)?\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )

        text = re.sub(
            r"\s*```$",
            "",
            text,
        )

    return json.loads(text)


def validate_scoring_totals(spec):
    from ai_grader.spec_validation import validate_scoring_totals as validate
    return validate(spec)


def generate_grading_spec(
    project_path,
    schema_path,
):
    """
    Generate, save, and validate a draft grading specification.
    """

    project_path = Path(project_path).resolve()
    schema_path = Path(schema_path).resolve()

    # Confirm that this is a recognizable project folder.
    build_project_manifest(project_path)

    sources = collect_project_sources(project_path)

    if not sources:
        raise RuntimeError(
            "No readable project materials were found. "
            "Add files to project, rubric, reference, "
            "or datasets."
        )

    with schema_path.open(
        "r",
        encoding="utf-8",
    ) as file:
        schema = json.load(file)

    prompt = build_generation_prompt(
        project_path,
        sources,
        schema,
    )

    client = get_client()

    model = os.getenv(
        "OPENAI_MODEL",
        "gpt-5",
    )

    response = client.responses.create(
        model=model,
        instructions=(
            "You are an instructor-supervised grading "
            "specification agent for engineering computing "
            "projects. Produce conservative, evidence-based "
            "draft grading specifications for instructor review."
        ),
        input=prompt,
        store=False,
    )

    if not response.output_text:
        raise RuntimeError(
            "The model returned no grading specification."
        )

    spec = parse_json_response(
        response.output_text
    )

    spec["status"] = "draft"
    spec.pop("approval", None)
    # Enforce scoring consistency before writing the draft specification.
    validate_scoring_totals(spec)

    grader_path = project_path / "grader"
    grader_path.mkdir(
        parents=True,
        exist_ok=True,
    )

    from ai_grader.spec_validation import validate_spec_data
    validate_spec_data(spec, schema)
    versions = [int(path.stem.rsplit("v", 1)[1])
                for path in grader_path.glob("grading_spec_v[0-9][0-9][0-9].json")]
    output_path = grader_path / f"grading_spec_v{max(versions, default=0) + 1:03d}.json"

    with output_path.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            spec,
            file,
            indent=2,
        )
        file.write("\n")

    # Validate the AI-generated file against
    # our stable local schema.
    validate_grading_spec(
        output_path,
        schema_path,
    )

    return output_path, spec