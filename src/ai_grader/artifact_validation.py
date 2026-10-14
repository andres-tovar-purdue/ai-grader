import hashlib
import zipfile
from pathlib import Path


SLX_REQUIRED_ENTRIES = {
    "[Content_Types].xml",
    "simulink/blockdiagram.xml",
}


def inspect_slx_package(path):
    """Validate an SLX container without parsing or executing its model."""
    path = Path(path)
    result = {
        "status": "missing",
        "size_bytes": 0,
        "sha256": None,
        "reason": "Artifact is missing.",
    }
    if not path.is_file():
        return result

    size = path.stat().st_size
    result["size_bytes"] = size
    if size == 0:
        result.update(status="corrupted_or_unreadable", reason="Artifact is empty.")
        return result

    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    result["sha256"] = digest.hexdigest()

    if not zipfile.is_zipfile(path):
        result.update(
            status="wrong_or_mislabeled_deliverable",
            reason="Artifact is not a native ZIP-based SLX package.",
        )
        return result

    try:
        with zipfile.ZipFile(path) as package:
            corrupt_entry = package.testzip()
            names = set(package.namelist())
    except (OSError, zipfile.BadZipFile):
        result.update(
            status="corrupted_or_unreadable",
            reason="SLX package cannot be read.",
        )
        return result

    if corrupt_entry is not None:
        result.update(
            status="corrupted_or_unreadable",
            reason="SLX package failed CRC validation.",
        )
        return result
    if not SLX_REQUIRED_ENTRIES.issubset(names):
        result.update(
            status="wrong_or_mislabeled_deliverable",
            reason="ZIP lacks required native Simulink package entries.",
        )
        return result

    result.update(
        status="apparently_valid_not_technically_inspected",
        reason=(
            "Nonempty native SLX package passed structural and CRC checks. "
            "Model internals were not parsed or executed."
        ),
    )
    return result


def preflight_slx_artifacts(submission, student_root):
    """Preflight all SLX artifacts and flag byte-identical deliverables."""
    reports = {}
    digest_paths = {}
    for file_info in submission["files"]:
        relative_path = file_info["path"]
        if Path(relative_path).suffix.lower() != ".slx":
            continue
        report = inspect_slx_package(Path(student_root) / relative_path)
        reports[relative_path] = report
        if report["sha256"]:
            digest_paths.setdefault(report["sha256"], []).append(relative_path)

    for paths in digest_paths.values():
        if len(paths) < 2:
            continue
        for relative_path in paths:
            reports[relative_path] = {
                **reports[relative_path],
                "status": "wrong_or_mislabeled_deliverable",
                "reason": (
                    "Artifact is byte-identical to another submitted SLX "
                    "deliverable with a different required role."
                ),
                "duplicate_paths": sorted(
                    path for path in paths if path != relative_path
                ),
            }
    return reports
