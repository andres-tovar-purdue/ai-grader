import base64
import csv
import io
import json
import mimetypes
import re
from datetime import datetime
from pathlib import Path

from ai_grader.ai_client import get_client, resolve_model
from ai_grader.artifact_validation import preflight_slx_artifacts
from ai_grader.grading_validation import (
    calculate_grading_result,
    validate_model_grading_response,
)
from ai_grader.spec_validation import latest_spec_path, validate_grading_spec
from ai_grader.submission_checks import spec_fingerprint, deliverable_presence_checks
from ai_grader.submission_processing import load_notebook
from ai_grader.rounding import DEFAULT_ROUNDING_POLICY, ROUNDING_POLICIES


SUBMISSION_MANIFEST_FILENAME = "submission_manifest.json"
ANONYMIZED_FOLDER = "anonymized_submissions"
GRADING_RUNS_FOLDER = "grading_runs"
DEBUG_RESPONSES_FOLDER = "debug_responses"
# archive-grading moves runs to ARCHIVE_FOLDER/<name>/grading_runs/.
ARCHIVE_FOLDER = "archive"
TEXT_EXTENSIONS = {".m", ".py", ".bas", ".txt", ".md", ".csv", ".json", ".ipynb"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
FORBIDDEN_MANIFEST_KEYS = {
    "username",
    "full_name",
    "selected_source_folder",
    "newest_source_folder",
    "original_path",
    "source_folder",
    "brightspace_prefix",
}


def parse_json_response(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def _usage_value(value, key, default=None):
    if value is None:
        return default
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def extract_response_usage(response):
    """Normalize Responses API usage when the SDK returns it."""
    usage = _usage_value(response, "usage")
    if usage is None:
        return None
    input_details = _usage_value(usage, "input_tokens_details", {})
    output_details = _usage_value(usage, "output_tokens_details", {})
    return {
        "input_tokens": int(_usage_value(usage, "input_tokens", 0) or 0),
        "cached_input_tokens": int(
            _usage_value(input_details, "cached_tokens", 0) or 0
        ),
        "output_tokens": int(_usage_value(usage, "output_tokens", 0) or 0),
        "reasoning_tokens": int(
            _usage_value(output_details, "reasoning_tokens", 0) or 0
        ),
        "total_tokens": int(_usage_value(usage, "total_tokens", 0) or 0),
    }


def _find_forbidden_keys(value):
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() in FORBIDDEN_MANIFEST_KEYS:
                found.add(key)
            found.update(_find_forbidden_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_find_forbidden_keys(item))
    return found


def load_grading_inputs(project_path):
    """Load only the approved spec, sanitized manifest, and anonymized tree."""
    project_path = Path(project_path).resolve()
    grader_path = project_path / "grader"
    spec_path = latest_spec_path(grader_path)
    manifest_path = grader_path / SUBMISSION_MANIFEST_FILENAME
    anonymized_root = grader_path / ANONYMIZED_FOLDER

    schema_path = (
        Path(__file__).resolve().parents[2]
        / "schemas"
        / "grading_spec.schema.json"
    )
    validate_grading_spec(spec_path, schema_path)
    with spec_path.open("r", encoding="utf-8") as file:
        spec = json.load(file)
    if spec.get("status") != "approved":
        raise RuntimeError(
            "grade-submissions requires an instructor-approved grading specification."
        )
    approval = spec.get("approval") or {}
    if not approval.get("approved_by") or not approval.get("approved_at"):
        raise RuntimeError("Approved grading specification lacks approval metadata.")

    with manifest_path.open("r", encoding="utf-8") as file:
        manifest = json.load(file)
    anonymization = manifest.get("anonymization") or {}
    if (
        manifest.get("manifest_version") != "2.0"
        or anonymization.get("status") != "validated"
        or anonymization.get("artifact_root") != "grader/anonymized_submissions"
        or anonymization.get("known_text_identities_redacted") is not True
    ):
        raise RuntimeError(
            "Submission manifest is not certified for anonymized AI grading. "
            "Run prepare-submissions first."
        )
    forbidden = _find_forbidden_keys(manifest)
    if forbidden:
        raise RuntimeError(
            "Submission manifest contains forbidden identity-bearing fields: "
            + ", ".join(sorted(forbidden))
        )
    if not anonymized_root.is_dir():
        raise FileNotFoundError(
            f"Anonymized submissions folder does not exist: {anonymized_root}"
        )

    if manifest.get("submission_count") != len(manifest.get("submissions", [])):
        raise RuntimeError("Submission count does not match manifest entries.")
    expected_students = set()
    for submission in manifest.get("submissions", []):
        student_id = submission.get("student_id", "")
        if not re.fullmatch(r"Student_\d{3}", student_id):
            raise RuntimeError(f"Invalid anonymized student ID: {student_id}")
        if student_id in expected_students:
            raise RuntimeError("Duplicate anonymized student ID in submission manifest.")
        expected_students.add(student_id)
        student_root = (anonymized_root / student_id).resolve()
        if not student_root.is_dir() or student_root.parent != anonymized_root.resolve():
            raise RuntimeError(f"Invalid anonymized student folder: {student_id}")

        if submission.get("file_count") != len(submission.get("files", [])):
            raise RuntimeError("File count does not match submission manifest entries.")
        seen_paths = set()
        for file_info in submission.get("files", []):
            relative_path = Path(file_info["path"])
            if (
                relative_path.is_absolute()
                or ".." in relative_path.parts
                or not relative_path.name.startswith("artifact_")
            ):
                raise RuntimeError(
                    f"Unsafe anonymized artifact path: {file_info['path']}"
                )
            artifact_path = (student_root / relative_path).resolve()
            if artifact_path.parent != student_root and student_root not in artifact_path.parents:
                raise RuntimeError(
                    f"Artifact escapes anonymized student folder: {file_info['path']}"
                )
            if not artifact_path.is_file():
                raise FileNotFoundError(
                    f"Anonymized artifact does not exist: {artifact_path}"
                )
            if file_info["path"] in seen_paths:
                raise RuntimeError("Duplicate anonymized artifact path in manifest.")
            seen_paths.add(file_info["path"])
            text_path = (file_info.get("text_extraction") or {}).get("text_path")
            if text_path is not None:
                if text_path != file_info["path"] + ".txt":
                    raise RuntimeError(
                        f"Unexpected extracted text path: {text_path}"
                    )
                if not (student_root / text_path).is_file():
                    raise FileNotFoundError(
                        f"Extracted PDF text does not exist: {text_path}"
                    )
                seen_paths.add(text_path)

        actual_paths = {p.relative_to(student_root).as_posix()
                        for p in student_root.rglob("*") if p.is_file()}
        if actual_paths != seen_paths:
            raise RuntimeError("Anonymized artifact tree does not match manifest entries.")
    if not expected_students:
        raise RuntimeError("Submission manifest has no submissions to grade.")
    filename_deliverables = {
        rule["deliverable_id"] for task in spec["tasks"] for criterion in task["criteria"]
        for rule in criterion.get("deduction_rules", []) if rule.get("basis") == "filename"
    }
    if filename_deliverables:
        for submission in manifest["submissions"]:
            checks = submission.get("deliverable_checks", {})
            if (checks.get("method") != "original_filenames_local_v1"
                    or checks.get("spec_sha256") != spec_fingerprint(spec)
                    or filename_deliverables - {item["deliverable_id"]
                                               for item in checks.get("checks", [])}):
                raise RuntimeError("Original filename checks are missing or stale. Run "
                                   "refresh-submission-checks for this project before grading.")

    actual_students = {
        path.name for path in anonymized_root.iterdir() if path.is_dir()
    }
    if actual_students != expected_students:
        raise RuntimeError(
            "Anonymized student folders do not match submission manifest."
        )

    return spec_path, spec, manifest_path, manifest, anonymized_root


def build_student_input(spec, submission, student_root, instructions):
    """Build one multimodal request without parsing or executing SLX models."""
    content = []
    artifact_inventory = []
    text_sections = []
    image_limitations = []
    slx_preflight = preflight_slx_artifacts(submission, student_root)

    for file_info in submission["files"]:
        relative_path = file_info["path"]
        path = student_root / relative_path
        extension = path.suffix.lower()
        artifact_inventory.append({
            "path": relative_path,
            "file_type": file_info["file_type"],
            "extension": extension,
            "size_bytes": file_info["size_bytes"],
        })

        if extension in TEXT_EXTENSIONS:
            text = path.read_text(encoding="utf-8", errors="replace")
            if "content" in file_info and text != file_info["content"]:
                raise RuntimeError(
                    f"Anonymized text differs from manifest: {relative_path}"
                )
            notebook = None
            if extension == ".ipynb":
                notebook, notebook_error = load_notebook(path.read_bytes())
                if notebook is None:
                    text = (
                        f"NOTEBOOK COULD NOT BE READ: {notebook_error}. Its cells "
                        "and saved outputs were not given to the grader. Do not "
                        "treat this as missing work; require instructor review."
                    )
            if notebook is not None:
                sections = [
                    "NOTEBOOK EVIDENCE: saved source and outputs; "
                    "the notebook was not executed by the grader."
                ]

                def join_text(value):
                    return "".join(value) if isinstance(value, list) else str(value)

                for cell_index, cell in enumerate(notebook.get("cells", []), 1):
                    sections.append(
                        f"CELL {cell_index} ({cell.get('cell_type', 'unknown')}):\n"
                        + join_text(cell.get("source", ""))
                    )
                    if cell.get("attachments"):
                        sections.append(
                            "Cell attachments were not extracted; require "
                            "instructor review if relevant to a grading criterion."
                        )

                    for output_index, output in enumerate(cell.get("outputs", []), 1):
                        if "text" in output:
                            sections.append("SAVED OUTPUT:\n" + join_text(output["text"]))
                        if output.get("output_type") == "error":
                            sections.append(
                                "SAVED ERROR:\n"
                                + join_text(output.get("ename", ""))
                                + ": " + join_text(output.get("evalue", ""))
                                + "\n" + join_text(output.get("traceback", []))
                            )

                        data = output.get("data", {})
                        if "text/plain" in data:
                            sections.append(
                                "SAVED OUTPUT:\n" + join_text(data["text/plain"])
                            )

                        for mime in ("image/png", "image/jpeg"):
                            if mime in data:
                                label = (
                                    f"{relative_path}, cell {cell_index}, "
                                    f"output {output_index}"
                                )
                                encoded = "".join(join_text(data[mime]).split())
                                content.append({
                                    "type": "input_text",
                                    "text": (
                                        f"NOTEBOOK IMAGE: {label}. Treat only "
                                        "visible content as evidence. Flag "
                                        "suspected visible identity."
                                    ),
                                })
                                content.append({
                                    "type": "input_image",
                                    "image_url": f"data:{mime};base64,{encoded}",
                                    "detail": "auto",
                                })
                                image_limitations.append(label)
                                break

                        if data and not any(
                            key in data
                            for key in ("text/plain", "image/png", "image/jpeg")
                        ):
                            sections.append(
                                "Unsupported saved output formats: "
                                + ", ".join(data)
                                + ". Require instructor review if relevant."
                            )

                text = "\n\n".join(sections)

            if file_info.get("source") == "github_repository":
                # Facts files sit at the student root; repository files are
                # inside a github_repo folder.
                evidence_type = ("github_repository_facts"
                                 if "/" not in relative_path
                                 else "github_repository_file")
            elif extension == ".ipynb":
                evidence_type = "notebook_source_and_saved_outputs"
            else:
                evidence_type = "source_code"
            text_sections.append(
                f"ARTIFACT: {relative_path}\n"
                f"EVIDENCE TYPE: {evidence_type}\n"
                f"{text}"
            )
        elif extension in IMAGE_EXTENSIONS:
            mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            content.append({
                "type": "input_text",
                "text": (
                    f"IMAGE ARTIFACT: {relative_path}. Treat only visible "
                    "content as image evidence. Flag suspected visible identity."
                ),
            })
            content.append({
                "type": "input_image",
                "image_url": f"data:{mime_type};base64,{encoded}",
                "detail": "auto",
            })
            image_limitations.append(relative_path)
        elif extension == ".pdf" and file_info.get("text_extraction"):
            extraction = file_info["text_extraction"]
            if extraction["status"] == "extracted":
                text = (student_root / extraction["text_path"]).read_text(encoding="utf-8")
                if text != file_info.get("extracted_text"):
                    raise RuntimeError(
                        f"Extracted PDF text differs from manifest: {relative_path}"
                    )
                notes = [
                    "PDF TEXT: extracted locally with pypdf and redacted like other "
                    "text. Figures, images, tables drawn as graphics, and page "
                    "layout were not extracted, and text order may differ from "
                    "the page. If a criterion depends on a figure or image in the "
                    "PDF, require instructor review instead of penalizing."
                ]
                if extraction["truncated"]:
                    notes.append(
                        f"PDF TEXT TRUNCATED: only the first "
                        f"{extraction['character_limit']:,} of "
                        f"{extraction['characters']:,} characters are included. "
                        "Do not treat content beyond that point as missing."
                    )
                text_sections.append(
                    f"ARTIFACT: {relative_path}\n"
                    "EVIDENCE TYPE: pdf_extracted_text\n"
                    + "\n".join(notes) + "\n" + text
                )
            else:
                text_sections.append(
                    f"ARTIFACT: {relative_path}\nEVIDENCE TYPE: file_presence\n"
                    "No text could be extracted from this PDF (status: "
                    f"{extraction['status']}); it may be scanned or damaged. Presence "
                    "only was verified; its contents require instructor review."
                )
        elif extension == ".slx":
            preflight = slx_preflight[relative_path]
            text_sections.append(
                f"ARTIFACT: {relative_path}\n"
                "EVIDENCE TYPE: structural_artifact_preflight\n"
                f"SLX PREFLIGHT: {json.dumps(preflight, sort_keys=True)}\n"
                "This preflight establishes artifact/container validity only. "
                "Do not infer model blocks, connections, parameters, settings, "
                "or behavior from the SLX package."
            )
        else:
            text_sections.append(
                f"ARTIFACT: {relative_path}\nEVIDENCE TYPE: file_presence\n"
                "Unsupported artifact; presence only was verified."
            )

    prompt = "\n\n".join([
        instructions,
        "APPROVED GRADING SPECIFICATION:\n" + json.dumps(spec, indent=2),
        "ANONYMIZED ARTIFACT INVENTORY:\n" + json.dumps(artifact_inventory, indent=2),
        "LOCAL DELIVERABLE PRESENCE (independent of required original filenames):\n"
        + json.dumps(deliverable_presence_checks(submission, spec), indent=2)
        + "\nWhen filename_policy is warning_only, naming differences earn reminders, "
        "not deductions. A missing-file deduction requires status missing; ambiguous "
        "roles require instructor review without a missing-file deduction.",
        "ORIGINAL FILENAME COMPLIANCE (checked locally before anonymization):\n"
        + json.dumps(submission.get("deliverable_checks", {"status": "not_available"}), indent=2),
        "Anonymized names and <username> placeholders are tool transformations. "
        "Never infer filename errors from them or from redacted save commands. "
        "Use only the original filename compliance checks. no_exact_match means "
        "the exact required original filename was not found; it does not prove "
        "the artifact content is missing. PDF text was extracted locally where possible; "
        "figures, images and layout were not. If needed evidence is in a PDF figure or a "
        "PDF without extractable text, flag instructor review without penalizing a tool limitation.",
        "TEXT AND PRESENCE EVIDENCE:\n" + "\n\n---\n\n".join(text_sections),
        (
            "IMAGE PRIVACY LIMITATION:\nImage filenames and filesystem paths are "
            "anonymized, but image pixels were not redacted. If visible identity "
            "is suspected in any image, require instructor review. Images supplied: "
            + ", ".join(image_limitations)
        ),
        (
            "Return only one JSON object for student_id "
            f"{submission['student_id']} matching the required response schema."
        ),
    ])
    content.insert(0, {"type": "input_text", "text": prompt})
    return [{"role": "user", "content": content}]


def next_grading_run_path(grader_path):
    """
    The next run_v### folder. Archived runs count too, so a new run never
    reuses an archived run's number (finalizations are filed by number).
    """
    runs_path = grader_path / GRADING_RUNS_FOLDER
    existing_versions = []
    folders = [runs_path, *(grader_path / ARCHIVE_FOLDER).glob(f"*/{GRADING_RUNS_FOLDER}")]
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            match = re.fullmatch(r"run_v(\d{3})", path.name)
            if path.is_dir() and match:
                existing_versions.append(int(match.group(1)))
    version = max(existing_versions, default=0) + 1
    return runs_path / f"run_v{version:03d}"


def render_preliminary_csv(results, spec):
    task_columns = [column for index, _ in enumerate(spec["tasks"], start=1)
                    for column in (f"task_{index}_score", f"task_{index}_max_points", f"task_{index}_feedback")]
    fieldnames = [
        "student_id",
        "total_agent_score",
        "maximum_score",
        "raw_total_before_rounding",
        "total_rounding_adjustment",
        "total_instructor_score",
        "validation_status",
        *task_columns,
        "review_required",
        "review_reasons",
    ]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames)
    writer.writeheader()
    for result in results:
        row = {
            "student_id": result["student_id"],
            "total_agent_score": result["total_agent_score"],
            "maximum_score": spec["project"]["total_points"],
            "raw_total_before_rounding": result["raw_total_before_rounding"],
            "total_rounding_adjustment": result["total_rounding_adjustment"],
            "total_instructor_score": "",
            "validation_status": result.get("validation_status", "validated"),
            "review_required": str(result["review_required"]).lower(),
            "review_reasons": " | ".join(result["review_reasons"]),
        }
        for index, task in enumerate(result["tasks"], start=1):
            row[f"task_{index}_score"] = task["agent_score"]
            row[f"task_{index}_max_points"] = task["max_points"]
            row[f"task_{index}_feedback"] = task["feedback"]
        writer.writerow(row)
    return stream.getvalue()


def grade_submissions(
    project_path,
    client=None,
    model=None,
    rounding_policy=DEFAULT_ROUNDING_POLICY,
    resume=False,
    retry_failed=False,
):
    """Grade each anonymized submission independently and write a draft run."""
    project_path = Path(project_path).resolve()
    (
        spec_path,
        spec,
        manifest_path,
        manifest,
        anonymized_root,
    ) = load_grading_inputs(project_path)

    repository_root = Path(__file__).resolve().parents[2]
    response_schema_path = repository_root / "schemas" / "grading_result.schema.json"
    response_schema = json.loads(response_schema_path.read_text(encoding="utf-8"))
    instructions = (
        repository_root / "prompts" / "submission_grading_instructions.md"
    ).read_text(encoding="utf-8")

    selected_model = resolve_model(model)
    results = []
    per_student_usage = []
    api_calls_made = 0
    reused_responses = 0
    api_instructions = ("You produce preliminary, evidence-based grading for instructor "
                        "review. Never produce a final instructor grade.")
    if retry_failed and not resume:
        raise ValueError('retry_failed requires resume=True.')
    from ai_grader.recovery import calculate_saved_response

    for index, submission in enumerate(manifest["submissions"], start=1):
        print(f"Grading {submission['student_id']} ({index}/{len(manifest['submissions'])})...", flush=True)
        student_root = anonymized_root / submission["student_id"]
        api_input = build_student_input(
            spec,
            submission,
            student_root,
            instructions,
        )
        debug_path = project_path / "grader" / DEBUG_RESPONSES_FOLDER
        debug_path.mkdir(parents=True, exist_ok=True)
        input_sha256 = spec_fingerprint({'input': api_input, 'response_schema': response_schema,
                                         'instructions': api_instructions})
        saved = None
        if resume:
            candidates = sorted(debug_path.glob(f"{submission['student_id']}_*.json"), reverse=True)
            for candidate in candidates:
                try:
                    possible = json.loads(candidate.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    continue
                if (possible.get('student_id') == submission['student_id']
                        and possible.get('model') == selected_model
                        and possible.get('input_sha256') == input_sha256
                        and possible.get('grading_spec_sha256') == spec_fingerprint(spec)
                        and possible.get('submission_sha256') == spec_fingerprint(submission)):
                    cached_result = calculate_saved_response(possible, spec, submission,
                                                            response_schema_path, rounding_policy)
                    if retry_failed and cached_result['validation_status'] == 'invalid_model_response':
                        break
                    saved = possible
                    reused_responses += 1
                    print(f"Reused saved response: {candidate.name}", flush=True)
                    break
            if saved is None and candidates and not retry_failed:
                # For example, responses made before a new spec was approved.
                print(f"No saved response for {submission['student_id']} matches the "
                      "current grading spec, model and inputs; requesting a new one.",
                      flush=True)
        if saved is None:
            client = client or get_client()
            saved = {
                "student_id": submission["student_id"],
                "model": selected_model,
                "grading_spec_sha256": spec_fingerprint(spec),
                "submission_sha256": spec_fingerprint(submission),
                "input_sha256": input_sha256,
                "usage": None, "output_text": None,
            }
            api_calls_made += 1
            try:
                response = client.responses.create(
                    model=selected_model,
                    instructions=api_instructions,
                    input=api_input,
                    text={'format': {'type': 'json_schema', 'name': 'preliminary_grading_response',
                                     'schema': response_schema, 'strict': True}},
                    store=False,
                )
                saved['output_text'] = response.output_text
                saved['usage'] = extract_response_usage(response)
            except Exception as error:
                if getattr(error, 'status_code', None) in {401, 403}:
                    raise
                saved['error'] = f'{type(error).__name__}: {error}'
            response_path = debug_path / (f"{submission['student_id']}_"
                f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json")
            temporary_path = response_path.with_suffix('.json.tmp')
            temporary_path.write_text(json.dumps(saved, indent=2) + '\n', encoding='utf-8')
            temporary_path.replace(response_path)
            print(f"Saved model response: {response_path}", flush=True)
        calculated = calculate_saved_response(saved, spec, submission, response_schema_path,
                                               rounding_policy)
        if calculated['validation_status'] == 'invalid_model_response':
            print(f"Flagged {submission['student_id']} for review; continuing batch: "
                  + calculated['validation_error'], flush=True)
        usage = saved.get('usage')
        calculated["api_usage"] = usage
        per_student_usage.append({
            "student_id": submission["student_id"],
            "available": usage is not None,
            **(usage or {}),
        })
        results.append(calculated)

    return write_grading_run(project_path, spec_path, spec, manifest_path,
                             results, per_student_usage, selected_model, rounding_policy,
                             batch_execution={'api_calls_made': api_calls_made,
                                              'reused_responses': reused_responses})


def write_grading_run(project_path, spec_path, spec, manifest_path, results,
                      per_student_usage, selected_model, rounding_policy,
                      recovery=None, batch_execution=None):
    """Write a new preliminary run after all results have been prepared locally."""
    usage_fields = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
    )
    usage_totals = {
        field: sum(item.get(field, 0) for item in per_student_usage)
        for field in usage_fields
    }

    run = {
        "run_version": None,
        "status": "preliminary_instructor_review_required",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "model": selected_model,
        "rounding_policy": dict(ROUNDING_POLICIES[rounding_policy]),
        "api_usage": {
            "complete": all(item["available"] for item in per_student_usage),
            "per_student": per_student_usage,
            "totals": usage_totals,
        },
        "estimated_cost": None,
        "pricing_assumptions": None,
        "grading_spec": spec_path.name,
        "grading_spec_sha256": spec_fingerprint(spec),
        "submission_manifest": manifest_path.name,
        "submission_count": len(results),
        "maximum_score": spec["project"]["total_points"],
        "results": results,
    }
    invalid_count = sum(item.get("validation_status") == "invalid_model_response"
                        for item in results)
    run["unscored_submission_count"] = invalid_count
    if invalid_count:
        run["status"] = "preliminary_validation_review_required"
    if recovery is not None:
        run["recovery"] = recovery
    if batch_execution is not None:
        run['batch_execution'] = batch_execution

    run_path = next_grading_run_path(project_path / "grader")
    run["run_version"] = run_path.name.removeprefix("run_v")
    json_text = json.dumps(run, indent=2) + "\n"
    csv_text = render_preliminary_csv(results, spec)

    run_path.mkdir(parents=True, exist_ok=False)
    json_path = run_path / "grading_results.json"
    csv_path = run_path / "preliminary_grading_report.csv"
    json_path.write_text(json_text, encoding="utf-8")
    csv_path.write_text(csv_text, encoding="utf-8", newline="")
    (run_path / "grading_spec_snapshot.json").write_text(
        json.dumps(spec, indent=2) + "\n", encoding="utf-8")

    return run_path, json_path, csv_path, run
