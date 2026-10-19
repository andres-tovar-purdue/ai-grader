"""Check original filenames locally; never expose identity-bearing names to the model."""
import fnmatch
import hashlib
import json
from pathlib import Path

from ai_grader.repo_evidence import (
    REPOSITORY_MAP_FILENAME, REPOSITORY_SOURCE, verify_repository_redaction,
)
from ai_grader.spec_validation import latest_spec_path, validate_grading_spec
from ai_grader.submission_processing import (
    anonymize_relative_path, process_submissions,
    validate_anonymized_manifest,
)


def spec_fingerprint(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode("utf-8")).hexdigest()


def deliverable_presence_checks(submission, spec):
    """Match supplied artifact roles, keeping uncertain names out of deductions."""
    deliverables = [item for item in spec['deliverables'] if item.get('presence_patterns')]
    candidates = {
        item['deliverable_id']: [file['path'] for file in submission['files']
                                if any(fnmatch.fnmatchcase(file['path'].lower(), pattern.lower())
                                       for pattern in item['presence_patterns'])]
        for item in deliverables
    }
    matched_paths = {path for paths in candidates.values() for path in paths}
    owners = {}
    for deliverable_id, paths in candidates.items():
        for path in paths:
            owners.setdefault(path, set()).add(deliverable_id)
    checks = []
    for item in deliverables:
        paths = candidates[item['deliverable_id']]
        suffixes = {Path(pattern).suffix.lower() for pattern in item['presence_patterns']}
        unknown = [file['path'] for file in submission['files']
                   if Path(file['path']).suffix.lower() in suffixes
                   and file['path'] not in matched_paths]
        # A spare file assigned to another role of this type could itself be
        # mislabeled. Do not call the missing role absent without review.
        for other in deliverables:
            other_suffixes = {Path(pattern).suffix.lower() for pattern in other['presence_patterns']}
            if suffixes & other_suffixes:
                unknown.extend(candidates[other['deliverable_id']][1:])
        unknown = sorted(set(unknown) - set(paths))
        if any(len(owners[path]) > 1 for path in paths):
            status = 'ambiguous'
        elif paths:
            status = 'present'
        elif unknown:
            status = 'ambiguous'
        else:
            status = 'missing'
        checks.append({'deliverable_id': item['deliverable_id'], 'status': status,
                       'artifact_paths': paths, 'unclassified_paths': unknown})
    return {'method': 'anonymized_file_presence_v1', 'checks': checks}


def original_filename_checks(folder, username, full_name, spec):
    files = sorted(path for path in Path(folder).rglob("*") if path.is_file())
    checks = []
    for deliverable in spec["deliverables"]:
        patterns = deliverable.get("file_patterns", [])
        exact_paths = []
        for index, path in enumerate(files, start=1):
            if any(fnmatch.fnmatchcase(path.name, pattern.replace("username", username))
                   for pattern in patterns):
                exact_paths.append(anonymize_relative_path(
                    path.relative_to(folder), index, username, full_name).as_posix())
        checks.append({"deliverable_id": deliverable["deliverable_id"],
                       "status": ("exact_match" if exact_paths else "no_exact_match")
                                 if patterns else "not_checked",
                       "artifact_paths": exact_paths})
    return {"method": "original_filenames_local_v1", "spec_sha256": spec_fingerprint(spec),
            "checks": checks}


def refresh_submission_checks(project_path):
    """Refresh only compliance facts; refuse changed identities, selections, or artifacts."""
    root = Path(project_path).resolve()
    grader = root / "grader"
    spec_path = latest_spec_path(grader)
    validate_grading_spec(spec_path, Path(__file__).resolve().parents[2] /
                          "schemas" / "grading_spec.schema.json")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    manifest_path = grader / "submission_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    private_map = json.loads((grader / "student_map.json").read_text(encoding="utf-8"))
    fresh, fresh_map, _ = process_submissions(root)
    if fresh_map != private_map or len(fresh) != len(manifest["submissions"]):
        raise RuntimeError("Original submission selection or identity mapping changed. "
                           "Prepare a new submission set instead of refreshing checks.")
    fresh_by_id = {item["student_id"]: item for item in fresh}
    for old in manifest["submissions"]:
        new = fresh_by_id[old["student_id"]]
        # Repository evidence was fetched separately; compare only the files
        # that came from the Brightspace submission folder.
        original_files = [file_info for file_info in old["files"]
                          if file_info.get("source") != REPOSITORY_SOURCE]
        if original_files != new["files"]:
            raise RuntimeError("Original submission artifacts changed; checks were not refreshed.")
        identity = private_map[old["student_id"]]
        folder = root / "submissions" / identity["selected_source_folder"]
        originals = sorted(path for path in folder.rglob("*") if path.is_file())
        for original, file_info in zip(originals, original_files):
            copied = grader / "anonymized_submissions" / old["student_id"] / file_info["path"]
            if file_info["file_type"] == "text":
                # Preparation writes text using platform-native newlines. Compare
                # text using universal-newline decoding, exactly as grading does.
                # This permits LF/CRLF differences, not changed characters/content.
                matches = copied.read_text(encoding="utf-8") == file_info["content"]
            else:
                matches = copied.read_bytes() == original.read_bytes()
            if not matches:
                raise RuntimeError(
                    "Anonymized artifact integrity check failed for "
                    f"{old['student_id']}/{file_info['path']}. "
                    "Artifact content differs from the prepared submission."
                )
        old["deliverable_checks"] = original_filename_checks(
            folder, identity["username"], identity["full_name"], spec)
    validate_anonymized_manifest(manifest, private_map)
    repository_map_path = grader / REPOSITORY_MAP_FILENAME
    if repository_map_path.is_file():
        verify_repository_redaction(
            manifest, json.loads(repository_map_path.read_text(encoding="utf-8")))
    backup = manifest_path.with_name("submission_manifest.before_checks.json")
    if not backup.exists():
        backup.write_bytes(manifest_path.read_bytes())
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest_path)
    return manifest_path, manifest
