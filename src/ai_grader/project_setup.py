"""
Stage 1 of the workflow: put the instructor's files where AI Grader reads them.

The instructor drops everything at the top of projects\\NAME\\: the
Brightspace ZIP, the assignment PDF, datasets and an own solution.
init-project unpacks the Brightspace download into submissions\\; this module
sorts the instructor's other files:

- PDF, Markdown and text files -> project\\ (assignment and solution;
  prepare-project reads them);
- Word and notebook files -> project\\ too, but they are not read: the
  instructor is asked to save them as PDF;
- CSV and Excel files -> datasets\\.

Files are moved, never overwritten: a name that already exists in the target
folder is left where it is and reported. Other files (including ZIPs that are
not Brightspace downloads) stay at the top and are reported.
"""

import os
from pathlib import Path


TO_PROJECT = {".pdf", ".md", ".txt"}
TO_PROJECT_UNREAD = {".docx", ".doc", ".ipynb", ".pptx"}
TO_DATASETS = {".csv", ".xlsx", ".xls", ".tsv", ".json"}


def sort_loose_files(project_path):
    """
    Move the instructor's loose files into project\\ and datasets\\. Returns
    {"project": [names], "datasets": [names], "unread": [names],
     "already_there": [names], "left": [names]}. Names are the instructor's
    file names (assignment materials, never student work).
    """
    project_path = Path(project_path)
    summary = {"project": [], "datasets": [], "unread": [], "already_there": [], "left": []}
    for entry in sorted(project_path.iterdir(), key=lambda path: path.name.lower()):
        if not entry.is_file() or entry.name.startswith("."):
            continue
        suffix = entry.suffix.lower()
        if suffix in TO_PROJECT or suffix in TO_PROJECT_UNREAD:
            folder, key = "project", "unread" if suffix in TO_PROJECT_UNREAD else "project"
        elif suffix in TO_DATASETS:
            folder, key = "datasets", "datasets"
        else:
            if suffix != ".zip":
                summary["left"].append(entry.name)
            continue
        target = project_path / folder / entry.name
        if target.exists():
            summary["already_there"].append(entry.name)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(entry, target)
        summary[key].append(entry.name)
    return summary


def loose_instructor_files(project_path):
    """Top-level files sort_loose_files would move (to tell whether setup is pending)."""
    project_path = Path(project_path)
    if not project_path.is_dir():
        return []
    return [entry.name for entry in project_path.iterdir()
            if entry.is_file() and not entry.name.startswith(".")
            and entry.suffix.lower() in TO_PROJECT | TO_PROJECT_UNREAD | TO_DATASETS
            and not (project_path / ("datasets" if entry.suffix.lower() in TO_DATASETS
                                     else "project") / entry.name).exists()]
