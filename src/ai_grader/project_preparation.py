import json
import re
from pathlib import Path

from ai_grader.ai_client import get_client, resolve_model
from ai_grader.dataset_inspection import collect_csv_datasets
from ai_grader.source_extraction import discover_project_sources


OUTPUT_PATHS = {
    "project_instructions": Path("project/project_instructions.md"),
    "instructor_rubric": Path("rubric/instructor_rubric.md"),
    "reference_solution": Path("reference/reference_solution.md"),
}

REQUIRED_HEADINGS = {
    "project_instructions": [
        "# Draft Project Instructions",
        "## Instructor Review Required",
    ],
    "instructor_rubric": [
        "# Draft Instructor Rubric",
        "## Instructor Review Required",
        "## Proposed Allocations Requiring Instructor Review",
    ],
    "reference_solution": [
        "# Draft Reference Solution",
        "## Instructor Review Required",
    ],
}


def parse_json_response(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def _format_sources(sources):
    sections = []
    for source in sources:
        if source["source_type"] == "pdf":
            content = "\n\n".join(
                f"[Page {page['page']}]\n{page['text']}"
                for page in source["pages"]
                if page["text"]
            )
        else:
            content = source["content"]
        sections.append(f"SOURCE: {source['path']}\n{content}")
    return "\n\n---\n\n".join(sections)


def _format_datasets(datasets):
    if not datasets:
        return "No CSV datasets were supplied."
    sections = []
    for dataset in datasets:
        limitation = dataset["limitation"] or (
            "None; full CSV content is included."
        )
        sections.append("\n".join([
            f"DATASET: {dataset['path']}",
            f"MODE: {dataset['mode']}",
            f"SIZE BYTES: {dataset['size_bytes']}",
            f"LIMITATION: {limitation}",
            "CONTENT:",
            dataset["content"],
        ]))
    return "\n\n---\n\n".join(sections)


def build_preparation_prompt(project_path, sources, datasets, instructions):
    return f"""
Prepare three instructor-reviewable Markdown drafts for the engineering project
named {Path(project_path).resolve().name}.

{instructions}

Return ONLY a JSON object with exactly these string fields:
project_instructions, instructor_rubric, reference_solution.

ORIGINAL PROJECT MATERIALS:
{_format_sources(sources)}

CSV DATASETS:
{_format_datasets(datasets)}
""".strip()


def validate_artifacts(artifacts):
    if not isinstance(artifacts, dict) or set(artifacts) != set(OUTPUT_PATHS):
        raise ValueError(
            "Preparation response must contain exactly: "
            + ", ".join(OUTPUT_PATHS)
        )

    for name, headings in REQUIRED_HEADINGS.items():
        content = artifacts[name]
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"Preparation artifact is empty: {name}")
        for heading in headings:
            if heading not in content:
                raise ValueError(
                    f"Preparation artifact {name} is missing required heading: "
                    f"{heading}"
                )

    reference = artifacts["reference_solution"].lower()
    if "not the only acceptable solution" not in reference:
        raise ValueError(
            "Reference solution must state that it is not the only "
            "acceptable solution."
        )


def prepare_project(project_path, client=None, full_csv_max_bytes=200_000):
    """Generate three draft artifacts without reading student submissions."""
    project_path = Path(project_path).resolve()
    if not project_path.exists():
        raise FileNotFoundError(f"Project folder does not exist: {project_path}")
    if not project_path.is_dir():
        raise NotADirectoryError(f"Project path is not a directory: {project_path}")

    output_paths = {
        name: project_path / relative_path
        for name, relative_path in OUTPUT_PATHS.items()
    }
    existing = [path for path in output_paths.values() if path.exists()]
    if existing:
        raise FileExistsError(
            "Refusing to overwrite instructor-reviewable artifact(s): "
            + ", ".join(str(path) for path in existing)
        )

    sources = discover_project_sources(
        project_path,
        excluded_paths=output_paths.values(),
    )
    datasets = collect_csv_datasets(project_path, full_csv_max_bytes)

    prompt_path = (
        Path(__file__).resolve().parents[2]
        / "prompts"
        / "project_preparation_instructions.md"
    )
    instructions = prompt_path.read_text(encoding="utf-8")
    prompt = build_preparation_prompt(
        project_path, sources, datasets, instructions
    )

    client = client or get_client()
    response = client.responses.create(
        model=resolve_model(),
        instructions=(
            "You prepare conservative, source-grounded engineering project "
            "drafts for instructor review. Do not grade student submissions."
        ),
        input=prompt,
        store=False,
    )
    if not response.output_text:
        raise RuntimeError(
            "The model returned no project preparation artifacts."
        )

    artifacts = parse_json_response(response.output_text)
    validate_artifacts(artifacts)

    for name, path in output_paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            artifacts[name].rstrip() + "\n",
            encoding="utf-8",
        )

    limitations = [
        dataset["limitation"]
        for dataset in datasets
        if dataset["limitation"]
    ]
    return {
        "output_paths": output_paths,
        "source_paths": [source["path"] for source in sources],
        "dataset_paths": [dataset["path"] for dataset in datasets],
        "limitations": limitations,
    }
