import argparse
import json
import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv

from ai_grader.ai_client import DEFAULT_MODEL, describe_model
from ai_grader.finalization import finalize_grading, load_approved_scores
from ai_grader.grading import grade_submissions
from ai_grader.recovery import recover_grading
from ai_grader.reporting import generate_review_report, open_report
from ai_grader.project_initialization import (
    ProjectInitializationError, count_items, describe_move_counts, init_project,
)
from ai_grader.project_inspection import inspect_project
from ai_grader.project_manifest import write_project_manifest
from ai_grader.project_preparation import prepare_project
from ai_grader.repo_evidence import NO_LINK_REASONS
from ai_grader.rounding import DEFAULT_ROUNDING_POLICY, ROUNDING_POLICIES
from ai_grader.setup_check import run_setup_checks
from ai_grader.spec_approval import approve_grading_spec
from ai_grader.spec_generation import generate_grading_spec
from ai_grader.spec_validation import validate_grading_spec
from ai_grader.submission_processing import write_submission_manifest
from ai_grader.submission_checks import refresh_submission_checks


load_dotenv()


def print_next_command(command):
    """Print a copy-paste-ready next command."""
    print()
    print("Next command:")
    print()
    print(command)


def print_init_changes(plan, dry_run):
    """Print what init-project moved or extracted, as counts only, never names."""
    moved = count_items(plan, "move")
    extracted = count_items(plan, "extract")
    already = count_items(plan, "already_present")

    print()
    if any(moved) or not plan["zips"]:
        print(f"{'Would move' if dry_run else 'Moved'} into submissions\\: "
              f"{describe_move_counts(*moved)}")

    if plan["zips"]:
        print(f"{'Would extract' if dry_run else 'Extracted'} from Brightspace "
              f"ZIP into submissions\\: {describe_move_counts(*extracted)}")
        for zip_record in plan["zips"]:
            note = "kept"
            if zip_record["other_count"]:
                note += (f"; {zip_record['other_count']} other file(s) in it "
                         "are not extracted")
            print(f"  - {zip_record['name']} ({note})")

    if any(already):
        print(f"Already in submissions\\ (skipped): {describe_move_counts(*already)}")

    for wrapper in plan["wrappers"]:
        if wrapper["remaining"] == 0:
            verb = "Would remove" if dry_run else (
                "Removed" if wrapper["removed"] else "Could not remove")
            print(f"{verb} the emptied download folder: {wrapper['name']}")

    other_count = plan["brightspace_named_not_moved"]
    kept_wrappers = [w["name"] for w in plan["wrappers"] if w["remaining"]]

    if plan["not_moved"] or other_count or kept_wrappers:
        print()
        print("Not moved (left in place):")
        for name in plan["not_moved"]:
            print(f"  - {name}")
        for name in kept_wrappers:
            print(f"  - {name} (download folder that still holds other items)")
        if other_count:
            print(
                f"  - {other_count} Brightspace-named file(s) that are not "
                "submission folders"
            )


def print_init_dry_run(plan):
    """Print the init-project plan with counts only, never student names."""
    project_path = plan["project_path"]

    print()
    print("Dry run: nothing was changed.")
    print()
    if plan["exists"]:
        print(f"Existing project folder: {project_path}")
    else:
        print(f"Would create project folder: {project_path}")

    print()
    if plan["missing_subfolders"]:
        print("Would create folders:")
        for folder_name in plan["missing_subfolders"]:
            print(f"  - {folder_name}\\")
    else:
        print("All standard folders already exist.")

    if not plan["exists"]:
        return

    print_init_changes(plan, dry_run=True)

    conflicts = count_items(plan, "conflict")
    if any(conflicts):
        print()
        print(
            "init-project would stop without changing anything: these already "
            "exist in submissions\\ (or appear twice) with different contents: "
            + describe_move_counts(*conflicts)
        )
    elif not plan["items"]:
        print()
        print("No Brightspace submission folders, index.html or ZIP found.")


def project_path_from_spec(spec_path):
    """Return PROJECT_PATH for a grading spec stored in PROJECT_PATH/grader/."""
    return Path(spec_path).resolve().parent.parent


def find_latest_grading_spec(project_path):
    """Return the latest grading_spec_v*.json path, or None if none exists."""
    grader_path = Path(project_path).resolve() / "grader"
    spec_paths = sorted(grader_path.glob("grading_spec_v*.json"))
    return spec_paths[-1] if spec_paths else None


def load_spec_status(spec_path):
    """Return grading-spec status, or None if the file cannot be read."""
    if spec_path is None:
        return None

    try:
        with Path(spec_path).open("r", encoding="utf-8") as file:
            spec = json.load(file)
    except (OSError, json.JSONDecodeError):
        return None

    return spec.get("status")


def load_unresolved_ambiguities(spec_path):
    """Return unresolved ambiguity records from a grading specification."""
    if spec_path is None:
        return []

    try:
        with Path(spec_path).open("r", encoding="utf-8") as file:
            spec = json.load(file)
    except (OSError, json.JSONDecodeError):
        return []

    return [
        ambiguity
        for ambiguity in spec.get("known_ambiguities", [])
        if ambiguity.get("status") == "unresolved"
    ]


def preparation_artifacts(project_path):
    """Return expected instructor-review preparation artifacts."""
    project_path = Path(project_path).resolve()
    return {
        "project instructions": project_path / "project" / "project_instructions.md",
        "instructor rubric": project_path / "rubric" / "instructor_rubric.md",
        "reference solution": project_path / "reference" / "reference_solution.md",
    }


def latest_grading_run(project_path):
    """Return the latest run_v* grading directory, or None."""
    runs_path = Path(project_path).resolve() / "grader" / "grading_runs"
    run_paths = sorted(
        path for path in runs_path.glob("run_v*")
        if path.is_dir()
    )
    return run_paths[-1] if run_paths else None


def latest_finalization_for_run(project_path, grading_run):
    """Return the latest finalization for the supplied grading run, or None."""
    if grading_run is None:
        return None

    finalizations_path = (
        Path(project_path).resolve()
        / "grader"
        / "finalizations"
        / Path(grading_run).name
    )

    if not finalizations_path.exists():
        return None

    finalization_paths = sorted(
        path for path in finalizations_path.glob("finalization_v*")
        if path.is_dir()
    )
    return finalization_paths[-1] if finalization_paths else None


def main():
    parser = argparse.ArgumentParser(
        description="AI Grader"
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True
    )

    # init-project command
    init_parser = subparsers.add_parser(
        "init-project",
        help="Create a new grading project workspace."
    )

    init_parser.add_argument(
        "project_name",
        help=(
            "Name of the grading project, e.g. mspe_49600_fa26_pr01. If "
            "projects\\NAME already exists (for example, holding a Brightspace "
            "ZIP or its extracted folders), missing folders are added and the "
            "Brightspace submissions are extracted or moved into submissions\\."
        ),
    )

    init_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be created and moved without changing anything.",
    )

    # prepare-project command
    prepare_project_parser = subparsers.add_parser(
        "prepare-project",
        help="Create instructor-reviewable project preparation drafts."
    )

    prepare_project_parser.add_argument(
        "project_path",
        help="Path to the project folder."
    )

    # validate command
    validate_parser = subparsers.add_parser(
        "validate",
        help="Validate a grading specification."
    )

    validate_parser.add_argument(
        "spec_path",
        help="Path to the project-specific grading specification."
    )

    # inspect command
    inspect_parser = subparsers.add_parser(
        "inspect",
        help="Inspect a grading project folder."
    )

    inspect_parser.add_argument(
        "project_path",
        help="Path to the project folder."
    )

    # manifest command
    manifest_parser = subparsers.add_parser(
        "manifest",
        help="Create a machine-readable project manifest."
    )

    manifest_parser.add_argument(
        "project_path",
        help="Path to the project folder."
    )

    # generate-spec command
    generate_spec_parser = subparsers.add_parser(
        "generate-spec",
        help="Generate a draft project-specific grading specification."
    )

    generate_spec_parser.add_argument(
        "project_path",
        help="Path to the project folder."
    )

    # approve-spec command
    approve_spec_parser = subparsers.add_parser(
        "approve-spec",
        help="Approve an instructor-reviewed grading specification."
    )

    approve_spec_parser.add_argument(
        "spec_path",
        help="Path to the draft grading specification."
    )

    approve_spec_parser.add_argument(
        "--allow-unresolved",
        action="store_true",
        help="Allow approval even when unresolved ambiguities remain."
    )

    # prepare-submissions command
    prepare_parser = subparsers.add_parser(
        "prepare-submissions",
        help="Prepare and anonymize student submissions."
    )

    prepare_parser.add_argument(
        "project_path",
        help="Path to the project folder."
    )

    checks_parser = subparsers.add_parser(
        "refresh-submission-checks",
        help="Check original filenames locally without replacing anonymized submissions.",
    )
    checks_parser.add_argument("project_path", help="Path to the project folder.")

    # grade-submissions command
    grade_parser = subparsers.add_parser(
        "grade-submissions",
        help="Create preliminary grading reports for anonymized submissions."
    )

    grade_parser.add_argument(
        "project_path",
        help="Path to the project folder."
    )

    grade_parser.add_argument(
        "--model",
        help=(
            "Responses API model override. Defaults to OPENAI_MODEL when set, "
            f"otherwise {DEFAULT_MODEL}."
        ),
    )

    grade_parser.add_argument(
        "--rounding-policy",
        choices=sorted(ROUNDING_POLICIES),
        default=DEFAULT_ROUNDING_POLICY,
        help="Task/project rounding policy for new preliminary grades.",
    )
    grade_parser.add_argument('--resume', action='store_true',
                              help='Reuse saved responses only when model and complete inputs match.')
    grade_parser.add_argument('--retry-failed', action='store_true',
                              help='With --resume, retry only failed responses; reuse successful ones.')

    recovery_parser = subparsers.add_parser(
        "recover-grading", help="Recover saved model responses offline; invalid results remain unscored.",
    )
    recovery_parser.add_argument("project_path", help="Path to the project folder.")
    recovery_parser.add_argument("--response-pattern", required=True,
                                 help="Filename pattern in grader/debug_responses; one response per student.")
    recovery_parser.add_argument("--rounding-policy", choices=sorted(ROUNDING_POLICIES),
                                 default=DEFAULT_ROUNDING_POLICY)

    report_parser = subparsers.add_parser(
        "generate-report", help="Create a named instructor review CSV with blank approval fields.",
    )
    report_parser.add_argument("project_path", help="Path to the project folder.")
    report_parser.add_argument("--run", help="Run version; defaults to the latest grading run.")
    report_parser.add_argument("--open", action="store_true",
                               help="Open the report in the default spreadsheet application.")

    # finalize-grading command
    finalize_parser = subparsers.add_parser(
        "finalize-grading",
        help="Create offline instructor and student-facing final reports.",
    )

    finalize_parser.add_argument(
        "project_path",
        help="Path to the project folder."
    )

    finalize_parser.add_argument(
        "--run",
        required=True,
        help="Baseline run version."
    )

    score_source = finalize_parser.add_mutually_exclusive_group(required=True)
    score_source.add_argument(
        "--score",
        action="append",
        help="Approved score as Student_###=POINTS.",
    )
    score_source.add_argument('--scores-file',
                              help='Reviewed CSV with student_id and filled total_instructor_score columns.')

    finalize_parser.add_argument(
        "--criterion-score",
        action="append",
        default=[],
        help="Criterion override as Student_###:CRITERION_ID=POINTS.",
    )

    # check-setup command
    check_setup_parser = subparsers.add_parser(
        "check-setup",
        help="Check the virtual environment, .env, projects folder and API access."
    )

    check_setup_parser.add_argument(
        "--skip-api",
        action="store_true",
        help="Do not make the test API request.",
    )

    args = parser.parse_args()

    # command handling section
    if args.command == "init-project":
        try:
            plan = init_project(args.project_name, dry_run=args.dry_run)
        except ProjectInitializationError as error:
            print()
            print(error)
            raise SystemExit(1)

        if plan.get("location_note"):
            print()
            print(plan["location_note"])

        project_path = plan["project_path"]
        placed_count = sum(
            sum(count_items(plan, action))
            for action in ("move", "extract", "already_present")
        )

        if args.dry_run:
            print_init_dry_run(plan)
            return

        print()
        print("Project initialized successfully:")
        print(f"  {project_path}")
        print()

        if plan["missing_subfolders"]:
            print("Created folders:")
            for folder_name in plan["missing_subfolders"]:
                print(f"  - {folder_name}\\")
        else:
            print("All standard folders already existed.")

        if plan["exists"]:
            print_init_changes(plan, dry_run=False)

        print()
        print("Before continuing:")
        print("  1. Place the official assignment instructions in project\\")
        print("  2. Place any instructor-provided datasets in datasets\\")
        if placed_count:
            print("  3. Confirm that submissions\\ holds every Brightspace submission folder")
        else:
            print("  3. Place downloaded Brightspace submissions in submissions\\")

        print_next_command(
            f'python -m ai_grader inspect "{project_path}"'
        )

    elif args.command == "prepare-project":
        print(describe_model())
        result = prepare_project(args.project_path)

        print()
        print("Draft project preparation artifacts written:")
        for path in result["output_paths"].values():
            print(f"  - {path}")

        if result["limitations"]:
            print()
            print("Dataset limitations:")
            for limitation in result["limitations"]:
                print(f"  - {limitation}")

        print()
        print("Instructor review required before continuing.")
        print()
        print("Please review:")
        print("  - project\\project_instructions.md")
        print("  - rubric\\instructor_rubric.md")
        print("  - reference\\reference_solution.md")
        print()
        print("Confirm that:")
        print("  - the project requirements match the original assignment;")
        print("  - the rubric point allocations are correct;")
        print("  - the reference solution is technically correct; and")
        print("  - no new student requirements were introduced.")

        print_next_command(
            f'python -m ai_grader generate-spec "{args.project_path}"'
        )

    elif args.command == "validate":
        schema_path = (
            Path(__file__).resolve().parents[2]
            / "schemas"
            / "grading_spec.schema.json"
        )

        validate_grading_spec(
            args.spec_path,
            schema_path
        )

        print()
        print("Grading specification is valid.")

    elif args.command == "inspect":
        summary = inspect_project(args.project_path)
        project_path = Path(args.project_path).resolve()

        print()
        print(f"Project: {summary['project_path']}")
        print()

        missing_folders = []

        for folder_name, info in summary["folders"].items():
            if info["exists"]:
                print(
                    f"{folder_name}: "
                    f"{info['file_count']} file(s)"
                )

                for file_path in info["files"]:
                    print(f"  - {file_path}")

            else:
                missing_folders.append(folder_name)
                print(f"{folder_name}: MISSING")

        print()
        print("Project inspection complete.")

        if missing_folders:
            print()
            print("Workflow status: project structure is incomplete.")
            print()
            print("Fix the missing project folders before continuing:")
            for folder_name in missing_folders:
                print(f"  - {folder_name}\\")
            print()
            print(
                "Run init-project with this project's name from the workspace "
                "root to add the missing folders; existing files are kept. "
                "Add --dry-run to preview."
            )

        else:
            prep_paths = preparation_artifacts(project_path)
            existing_prep = {
                name: path
                for name, path in prep_paths.items()
                if path.exists()
            }

            spec_path = find_latest_grading_spec(project_path)
            spec_status = load_spec_status(spec_path)

            manifest_path = project_path / "grader" / "submission_manifest.json"
            student_map_path = project_path / "grader" / "student_map.json"
            anonymized_path = project_path / "grader" / "anonymized_submissions"

            submissions_prepared = (
                manifest_path.is_file()
                and student_map_path.is_file()
                and anonymized_path.is_dir()
            )

            grading_run = latest_grading_run(project_path)
            finalization = latest_finalization_for_run(project_path, grading_run)

            print()

            if not existing_prep and spec_path is None:
                print("Workflow status: ready for project preparation.")
                print()
                print("Before continuing, confirm that:")
                print("  - project\\ contains the official assignment materials;")
                print("  - datasets\\ contains any instructor-provided datasets; and")
                print("  - submissions\\ contains the downloaded student submissions.")

                print_next_command(
                    f'python -m ai_grader prepare-project "{args.project_path}"'
                )

            elif len(existing_prep) != len(prep_paths):
                print("Workflow status: project preparation is incomplete.")
                print()
                print("Expected preparation files:")
                for name, path in prep_paths.items():
                    state = "FOUND" if path.exists() else "MISSING"
                    print(f"  - {state}: {path}")
                print()
                print(
                    "Do not continue until the three preparation artifacts "
                    "are present and have been reviewed."
                )

            elif spec_path is None:
                print("Workflow status: preparation artifacts are ready for review.")
                print()
                print("Please review:")
                for path in prep_paths.values():
                    print(f"  - {path}")
                print()
                print("Confirm that:")
                print("  - the project requirements match the original assignment;")
                print("  - the rubric point allocations are correct;")
                print("  - the reference solution is technically correct; and")
                print("  - no new student requirements were introduced.")

                print_next_command(
                    f'python -m ai_grader generate-spec "{args.project_path}"'
                )

            elif spec_status == "draft":
                unresolved = load_unresolved_ambiguities(spec_path)

                print("Workflow status: draft grading specification awaiting approval.")
                print()
                print("Please review:")
                print(f"  - {spec_path}")
                print()
                print("Confirm that:")
                print("  - task names and point totals are correct;")
                print("  - grading criteria match the reviewed rubric;")
                print("  - evidence requirements are appropriate; and")
                print("  - unresolved ambiguities have been reviewed.")

                if unresolved:
                    print()
                    print(
                        f"Unresolved grading decisions: {len(unresolved)}"
                    )
                    for ambiguity in unresolved:
                        ambiguity_id = ambiguity.get("ambiguity_id", "UNKNOWN")
                        description = ambiguity.get(
                            "description",
                            "No description provided.",
                        )
                        print(f"  - {ambiguity_id}: {description}")

                    print()
                    print(
                        "Approval will be blocked until these ambiguities are "
                        "resolved, unless you explicitly choose --allow-unresolved."
                    )

                print_next_command(
                    f'python -m ai_grader approve-spec "{spec_path}"'
                )

            elif spec_status not in {"approved", "draft"}:
                print("Workflow status: grading specification needs attention.")
                print()
                print(f"Specification found: {spec_path}")
                print(
                    "Its status could not be recognized as 'draft' or 'approved'."
                )
                print("Review or validate the specification before continuing.")

                print_next_command(
                    f'python -m ai_grader validate "{spec_path}"'
                )

            elif spec_status == "approved" and not submissions_prepared:
                print("Workflow status: grading specification approved.")
                print()
                print("Approved specification:")
                print(f"  - {spec_path}")
                print()
                print(
                    "Next, prepare and anonymize the student submissions."
                )

                print_next_command(
                    f'python -m ai_grader prepare-submissions "{args.project_path}"'
                )

            elif submissions_prepared and grading_run is None:
                print("Workflow status: anonymized submissions are ready for grading.")
                print()
                print("Review before grading:")
                print(f"  - {manifest_path}")
                print(f"  - {anonymized_path}")
                print()
                print(
                    "Keep student_map.json private; it contains the identity mapping."
                )

                print_next_command(
                    f'python -m ai_grader grade-submissions "{args.project_path}"'
                )

            elif grading_run is not None and finalization is None:
                json_path = grading_run / "grading_results.json"
                csv_path = grading_run / "preliminary_grading_report.csv"

                run_data = json.loads(json_path.read_text(encoding="utf-8")) if json_path.is_file() else {}
                invalid_ids = [result['student_id'] for result in run_data.get('results', [])
                               if result.get('validation_status') == 'invalid_model_response']
                if invalid_ids:
                    print("Workflow status: saved-response validation review required.")
                    print("Unscored submissions: " + ', '.join(invalid_ids))
                    print(f"Review: {csv_path}")
                    print(f"Saved response and validation details: {json_path}")
                    print("Correct and revalidate the invalid responses before finalization.")
                    return

                print("Workflow status: preliminary grading completed.")
                print()
                print("Instructor review required before finalization.")
                print()
                print("Please review:")
                if csv_path.exists():
                    print(f"  - {csv_path}")
                if json_path.exists():
                    print(f"  - {json_path}")
                print()
                print(
                    "Approve or adjust the criterion scores and final score "
                    "for each student."
                )
                print()
                print_next_command(
                    f'python -m ai_grader generate-report '
                    f'"{args.project_path}" --run {grading_run.name} --open'
                )

            else:
                print("Workflow status: grading has been finalized.")
                print()
                print("Latest finalization:")
                print(f"  - {finalization}")
                print()
                print(
                    "Review the final instructor summary and student feedback "
                    "before entering grades in Brightspace."
                )

    elif args.command == "manifest":
        output_path, manifest = write_project_manifest(
            args.project_path
        )

        print()
        print(f"Project manifest written to: {output_path}")
        print(f"Files inventoried: {manifest['total_files']}")
        print(
            f"Submission folders: "
            f"{manifest['submission_count']}"
        )

    elif args.command == "generate-spec":
        schema_path = (
            Path(__file__).resolve().parents[2]
            / "schemas"
            / "grading_spec.schema.json"
        )

        print(describe_model())
        output_path, spec = generate_grading_spec(
            args.project_path,
            schema_path,
        )

        print()
        print(
            f"Draft grading specification written to: "
            f"{output_path}"
        )
        print(f"Tasks generated: {len(spec['tasks'])}")
        print("Grading specification passed schema validation.")

        print()
        print("Instructor review required before approval.")
        print()
        print("Please review:")
        print(f"  - {output_path}")
        print()
        print("Confirm that:")
        print("  - task names and point totals are correct;")
        print("  - grading criteria match the reviewed rubric; and")
        print("  - no unresolved or unintended requirements remain.")

        unresolved = [
            ambiguity
            for ambiguity in spec.get("known_ambiguities", [])
            if ambiguity.get("status") == "unresolved"
        ]

        if unresolved:
            print()
            print(f"Unresolved grading decisions: {len(unresolved)}")
            for ambiguity in unresolved:
                ambiguity_id = ambiguity.get("ambiguity_id", "UNKNOWN")
                description = ambiguity.get(
                    "description",
                    "No description provided.",
                )
                print(f"  - {ambiguity_id}: {description}")

            print()
            print(
                "Resolve these decisions in the draft specification before "
                "approval, or explicitly approve them with --allow-unresolved."
            )

        print_next_command(
            f'python -m ai_grader approve-spec "{output_path}"'
        )

    elif args.command == "approve-spec":
        schema_path = (
            Path(__file__).resolve().parents[2]
            / "schemas"
            / "grading_spec.schema.json"
        )

        approved_by = os.getenv(
            "GRADER_INSTRUCTOR_NAME"
        )

        if not approved_by:
            raise RuntimeError(
                "GRADER_INSTRUCTOR_NAME was not found. "
                "Add it to the local .env file."
            )

        try:
            output_path, spec, unresolved = approve_grading_spec(
                args.spec_path,
                schema_path,
                approved_by=approved_by,
                allow_unresolved=args.allow_unresolved,
            )
        except RuntimeError as exc:
            message = str(exc)

            if "contains unresolved ambiguities" not in message:
                raise

            spec_path = Path(args.spec_path).resolve()

            print()
            print("Grading specification was NOT approved.")
            print()
            print(message)
            print()
            print("Review and resolve the grading decisions in:")
            print(f"  - {spec_path}")
            print()
            print("After resolving them, run:")
            print()
            print(
                f'python -m ai_grader approve-spec "{spec_path}"'
            )
            print()
            print(
                "If you intentionally accept the remaining ambiguities, "
                "you may instead run:"
            )
            print()
            print(
                f'python -m ai_grader approve-spec "{spec_path}" '
                "--allow-unresolved"
            )
            return

        print()
        print(
            f"Grading specification approved: "
            f"{output_path}"
        )
        print(
            f"Approved by: "
            f"{spec['approval']['approved_by']}"
        )

        if unresolved:
            print(
                f"Warning: approved with "
                f"{len(unresolved)} unresolved ambiguity/ambiguities."
            )

        project_path = project_path_from_spec(args.spec_path)
        if (project_path / "grader" / "anonymized_submissions").is_dir():
            next_step = "refresh-submission-checks"
        else:
            next_step = "prepare-submissions"
        print_next_command(
            f'python -m ai_grader {next_step} "{project_path}"'
        )

    elif args.command == "prepare-submissions":
        manifest_path, map_path, manifest = (
            write_submission_manifest(
                args.project_path
            )
        )

        anonymized_path = (
            Path(args.project_path).resolve()
            / "grader"
            / "anonymized_submissions"
        )

        print()
        print(
            f"Submission manifest written to: "
            f"{manifest_path}"
        )
        print(
            f"Student map written to: "
            f"{map_path}"
        )
        print(
            f"Submissions found: "
            f"{manifest['submission_count']}"
        )
        print(
            "Anonymized artifacts written beneath: "
            f"{anonymized_path}"
        )

        unparsed_count = manifest.get("unparsed_folder_count", 0)
        if unparsed_count:
            print()
            print(
                f"Warning: {unparsed_count} submission folder(s) could not be parsed."
            )
            print("Review the submission manifest before grading.")

        repositories = manifest.get("repository_evidence")
        if repositories:
            print()
            print("GitHub repositories linked in submissions\\index.html:")
            print(f"  Students with repository links: "
                  f"{repositories['students_with_repository_urls']}")
            print(f"  Students without repository links: "
                  f"{repositories['students_without_repository_urls']}")
            for status, count in repositories["fetch_status_counts"].items():
                print(f"  {status.replace('_', ' ')}: {count}")
            missing = repositories["students_without_recognized_repository_link"]
            if missing:
                print("  No recognized repository link:")
                for student_id, reason in missing.items():
                    print(f"    - {student_id}: {NO_LINK_REASONS[reason]}")
            candidates = repositories["candidate_repositories_for_instructor_review"]
            if candidates:
                print("  Possible repositories for instructor review (not fetched or graded):")
                for student_id, count in candidates.items():
                    print(f"    - {student_id}: {count} candidate(s)")
            unmatched = repositories["index_entries_with_github_links_not_matched"]
            if unmatched:
                print(f"  Warning: {unmatched} index.html entr"
                      f"{'y' if unmatched == 1 else 'ies'} with GitHub links "
                      "could not be matched to exactly one student; their "
                      "repositories were not fetched.")
            print("  Graded repository files are the last commit at or before "
                  "each submission time. Facts and files are in each student's "
                  "anonymized folder; current repository states are kept in "
                  "grader\\repository_current (not graded); owners, URLs and "
                  "candidates are kept privately in grader\\repository_map.json.")

        print()
        print("Review before grading:")
        print(f"  - {manifest_path}")
        print(f"  - {anonymized_path}")
        print()
        print("Keep student_map.json private; it contains the identity mapping.")

        print_next_command(
            f'python -m ai_grader grade-submissions "{args.project_path}"'
        )

    elif args.command == "refresh-submission-checks":
        manifest_path, manifest = refresh_submission_checks(args.project_path)
        print(f"Original filename checks refreshed for {manifest['submission_count']} submissions.")
        print(f"Manifest: {manifest_path}")
        print_next_command(f'python -m ai_grader grade-submissions "{args.project_path}"')

    elif args.command == "grade-submissions":
        print(describe_model(args.model))
        run_path, json_path, csv_path, run = grade_submissions(
            args.project_path,
            model=args.model,
            rounding_policy=args.rounding_policy,
            resume=args.resume,
            retry_failed=args.retry_failed,
        )

        print()
        print(f"Preliminary grading run written to: {run_path}")
        print(f"Structured results: {json_path}")
        print(f"Instructor review report: {csv_path}")
        print(f"Submissions graded: {run['submission_count']}")
        print("Final instructor scores were not assigned.")
        execution = run.get('batch_execution', {})
        if execution:
            print(f"New API calls: {execution['api_calls_made']}; reused responses: {execution['reused_responses']}")
        if run.get('unscored_submission_count'):
            print(f"Unscored submissions needing review: {run['unscored_submission_count']}")
            print('Valid submissions were saved. Resolve flagged responses before finalization.')
            return

        print()
        print("Instructor review required before finalization.")
        print()
        print("Please review:")
        print(f"  - {csv_path}")
        print(f"  - {json_path}")
        print()
        print("Approve or adjust the criterion scores and final scores for each student.")
        print()
        print_next_command(
            f'python -m ai_grader generate-report "{args.project_path}" '
            f'--run {Path(run_path).name} --open'
        )

    elif args.command == "recover-grading":
        run_path, json_path, csv_path, run = recover_grading(
            args.project_path, args.response_pattern, rounding_policy=args.rounding_policy,
        )
        print(f"Recovered preliminary run: {run_path}")
        print(f"Structured results: {json_path}")
        print(f"Instructor review report: {csv_path}")
        print("New API calls: 0")
        for result in run['results']:
            if result.get('validation_status') == 'invalid_model_response':
                print(f"{result['student_id']}: UNSCORED — {result['validation_error']}")
            else:
                print(f"{result['student_id']}: preliminary score {result['total_agent_score']:g}")
        if run.get('unscored_submission_count'):
            print("Invalid saved responses require correction and revalidation before finalization.")
        print("Review the saved-response provenance and feedback before approving any scores.")
        print_next_command(
            f'python -m ai_grader generate-report "{args.project_path}" '
            f'--run {Path(run_path).name} --open'
        )

    elif args.command == "generate-report":
        try:
            report_path = generate_review_report(args.project_path, args.run)
        except (ValueError, OSError, KeyError) as error:
            parser.error(str(error))
        print(f"Named instructor review report: {report_path}")
        print("Review each submission and fill total_instructor_score, then save the CSV.")
        print("Final scores remain unapproved until you complete the review.")
        print_next_command(
            f'python -m ai_grader finalize-grading "{args.project_path}" '
            f'--run {report_path.parent.name} --scores-file "{report_path}"'
        )
        if args.open:
            try:
                open_report(report_path)
            except (OSError, subprocess.SubprocessError) as error:
                print(f"Report saved, but could not open it automatically: {error}")

    elif args.command == "finalize-grading":
        approved_scores = load_approved_scores(args.scores_file) if args.scores_file else {}
        for item in args.score or []:
            student_id, score = item.split("=", 1)
            approved_scores[student_id] = float(score)

        overrides = {}
        for item in args.criterion_score:
            key, score = item.split("=", 1)
            student_id, criterion_id = key.split(":", 1)
            overrides[(student_id, criterion_id)] = float(score)

        output_path, csv_path, txt_paths, validation = finalize_grading(
            args.project_path,
            args.run,
            approved_scores,
            overrides,
        )

        print()
        print(f"Finalization written to: {output_path}")
        print(f"Instructor summary: {csv_path}")
        for path in txt_paths:
            print(f"Student feedback: {path}")
        print(f"Offline validation: {json.dumps(validation, sort_keys=True)}")
        print()
        print("Grading workflow complete.")
        print("Review the final instructor summary and student feedback before")
        print("entering grades in Brightspace.")

    elif args.command == "check-setup":
        if not args.skip_api:
            print(describe_model())
        checks = run_setup_checks(skip_api=args.skip_api)

        print()
        for item in checks:
            status = "OK     " if item["ok"] else "PROBLEM"
            print(f"  [{status}] {item['name']}: {item['detail']}")
        print()

        if not all(item["ok"] for item in checks):
            print("Setup is incomplete. Fix the problems above, or rerun")
            print("python install.py from the ai-grader folder.")
            raise SystemExit(1)

        print("Setup is complete.")


if __name__ == "__main__":
    main()
