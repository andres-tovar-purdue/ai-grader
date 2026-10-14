from pathlib import Path


EXPECTED_FOLDERS = [
    "project",
    "datasets",
    "rubric",
    "reference",
    "submissions",
    "grader",
]


def inspect_project(project_path):
    """
    Inspect a project directory and summarize its contents.
    """

    project_path = Path(project_path).resolve()

    if not project_path.exists():
        raise FileNotFoundError(
            f"Project folder does not exist: {project_path}"
        )

    if not project_path.is_dir():
        raise NotADirectoryError(
            f"Project path is not a directory: {project_path}"
        )

    summary = {
        "project_path": str(project_path),
        "folders": {},
    }

    for folder_name in EXPECTED_FOLDERS:
        folder_path = project_path / folder_name

        if folder_path.exists() and folder_path.is_dir():
            files = [
                str(path.relative_to(project_path))
                for path in folder_path.rglob("*")
                if path.is_file()
            ]

            summary["folders"][folder_name] = {
                "exists": True,
                "file_count": len(files),
                "files": files,
            }

        else:
            summary["folders"][folder_name] = {
                "exists": False,
                "file_count": 0,
                "files": [],
            }

    return summary