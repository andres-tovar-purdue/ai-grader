import json
from pathlib import Path

from ai_grader.project_inspection import inspect_project


MANIFEST_FILENAME = "project_manifest.json"


def build_project_manifest(project_path):
    """
    Build a machine-readable inventory of a grading project.
    """

    project_path = Path(project_path).resolve()
    summary = inspect_project(project_path)

    folders = {}
    total_files = 0

    for folder_name, info in summary["folders"].items():

        # Do not include the manifest itself in its own inventory.
        files = [
            Path(file_path).as_posix()
            for file_path in info["files"]
            if Path(file_path).name != MANIFEST_FILENAME
        ]

        folders[folder_name] = {
            "exists": info["exists"],
            "file_count": len(files),
            "files": files,
        }

        total_files += len(files)

    submissions_path = project_path / "submissions"

    submission_folders = []

    if submissions_path.exists():
        submission_folders = sorted(
            path.name
            for path in submissions_path.iterdir()
            if path.is_dir()
        )

    manifest = {
        "manifest_version": "1.0",
        "project_name": project_path.name,
        "total_files": total_files,
        "submission_count": len(submission_folders),
        "submission_folders": submission_folders,
        "folders": folders,
    }

    return manifest


def write_project_manifest(project_path):
    """
    Build and save the project manifest in the grader folder.
    """

    project_path = Path(project_path).resolve()

    manifest = build_project_manifest(project_path)

    grader_path = project_path / "grader"
    grader_path.mkdir(parents=True, exist_ok=True)

    output_path = grader_path / MANIFEST_FILENAME

    with output_path.open("w", encoding="utf-8") as file:
        json.dump(manifest, file, indent=2)
        file.write("\n")

    return output_path, manifest