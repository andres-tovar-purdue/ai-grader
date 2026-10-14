from pathlib import Path


PROJECT_SUBFOLDERS = [
    "project",
    "datasets",
    "rubric",
    "reference",
    "submissions",
    "grader",
]


def init_project(project_name, workspace_path=None):
    """
    Create a new grading project beneath the workspace projects folder.

    Example:
        init_project("mspe_49600_fa26_pr01")
    """

    if workspace_path is None:
        workspace_path = Path.cwd()

    workspace_path = Path(workspace_path).resolve()

    projects_path = workspace_path / "projects"
    project_path = projects_path / project_name

    if project_path.exists():
        raise FileExistsError(
            f"Project already exists: {project_path}"
        )

    projects_path.mkdir(parents=True, exist_ok=True)
    project_path.mkdir()

    for folder_name in PROJECT_SUBFOLDERS:
        (project_path / folder_name).mkdir()

    return project_path