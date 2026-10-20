"""
Set grading runs aside without deleting them.

archive-grading moves grading runs, and the saved model responses once no
run is left, into grader\\archive\\grading_<timestamp>\\ with a record of what
was moved and why. Nothing is deleted. A run with finalized grades is never
moved. Run numbers keep counting past archived runs, so a new run never
reuses an archived run's number (finalizations are filed by run number).
"""

import json
import re
from datetime import datetime
from pathlib import Path

from ai_grader.grading import (
    ARCHIVE_FOLDER, DEBUG_RESPONSES_FOLDER, GRADING_RUNS_FOLDER,
)
from ai_grader.project_initialization import rename_with_retry


ARCHIVE_RECORD_FILENAME = "archive_record.json"


class ArchiveError(RuntimeError):
    """archive-grading stopped before moving anything."""


def grading_runs(grader_path):
    runs_path = Path(grader_path) / GRADING_RUNS_FOLDER
    if not runs_path.is_dir():
        return []
    return sorted(path.name for path in runs_path.iterdir()
                  if path.is_dir() and re.fullmatch(r"run_v\d{3}", path.name))


def has_finalizations(grader_path, run_version):
    folder = Path(grader_path) / "finalizations" / run_version
    return folder.is_dir() and any(folder.glob("finalization_v*"))


def new_archive_path(grader_path):
    root = Path(grader_path) / ARCHIVE_FOLDER
    name = f"grading_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    path, number = root / name, 1
    while path.exists():
        number += 1
        path = root / f"{name}_{number}"
    return path


def archive_grading(project_path, run_versions=None, reason=None):
    """
    Move the given grading runs (all runs when none are given) into a new
    archive folder. When no run is left in grading_runs, the saved model
    responses are moved too, since they belong to the same preparation.

    Returns (archive folder, moved items). Raises ArchiveError, having moved
    nothing, for an unknown run, a finalized run, or nothing to archive.
    """

    grader_path = Path(project_path).resolve() / "grader"
    existing = grading_runs(grader_path)
    if run_versions:
        unknown = sorted(set(run_versions) - set(existing))
        if unknown:
            raise ArchiveError(
                f"No such grading run: {', '.join(unknown)}. Nothing was moved.")
        selected = sorted(set(run_versions))
    else:
        selected = existing

    finalized = [run for run in selected if has_finalizations(grader_path, run)]
    if finalized:
        raise ArchiveError(
            f"{', '.join(finalized)} has finalized grades (grader\\finalizations\\). "
            "Finalized grades are never set aside automatically. Nothing was moved.")

    remaining = [run for run in existing if run not in selected]
    responses_path = grader_path / DEBUG_RESPONSES_FOLDER
    move_responses = (not remaining and responses_path.is_dir()
                      and any(responses_path.iterdir()))
    if not selected and not move_responses:
        raise ArchiveError(
            "Nothing to archive: there are no grading runs or saved responses.")

    archive_path = new_archive_path(grader_path)
    (archive_path / GRADING_RUNS_FOLDER).mkdir(parents=True)
    planned = [f"{GRADING_RUNS_FOLDER}/{run}" for run in selected]
    if move_responses:
        planned.append(DEBUG_RESPONSES_FOLDER)
    moved = []
    try:
        for item in planned:
            rename_with_retry(grader_path / item, archive_path / item)
            moved.append(item)
    finally:
        # Written even after a failed move, so the record says what moved.
        record = {
            "archived_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "reason": reason,
            "moved": moved,
            "complete": moved == planned,
            "note": "Set aside by archive-grading; nothing was deleted.",
        }
        (archive_path / ARCHIVE_RECORD_FILENAME).write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return archive_path, moved
