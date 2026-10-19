import codecs
import hashlib
import json
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path


TEXT_EXTENSIONS = {
    ".py",
    ".m",
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".ipynb",
    ".bas",
}

IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
}

MODEL_EXTENSIONS = {
    ".slx",
    ".mdl",
}

SPREADSHEET_EXTENSIONS = {
    ".xlsx",
    ".xlsm",
    ".xls",
}

ANONYMIZED_SUBMISSIONS_FOLDER = "anonymized_submissions"


def classify_file(path):
    """
    Classify a submitted file by extension.
    """

    extension = path.suffix.lower()

    if extension in TEXT_EXTENSIONS:
        return "text"

    if extension in IMAGE_EXTENSIONS:
        return "image"

    if extension in MODEL_EXTENSIONS:
        return "model"

    if extension in SPREADSHEET_EXTENSIONS:
        return "spreadsheet"

    return "other"


# Longest first: the UTF-32 LE mark starts with the UTF-16 LE mark. UTF-8
# (with or without a BOM) keeps the previous decoding, so manifests prepared
# before this change still match on refresh-submission-checks.
TEXT_BYTE_ORDER_MARKS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def decode_text(data):
    """
    Decode text bytes, honouring a byte-order mark.

    Windows tools such as PowerShell's ">" write UTF-16 with a BOM; reading
    those as UTF-8 would turn every character into a replacement character.
    Without a BOM, text is read as UTF-8. Line endings are translated to
    "\\n" exactly as Python's text mode (universal newlines) does.
    """
    encoding = "utf-8"
    for mark, candidate in TEXT_BYTE_ORDER_MARKS:
        if data.startswith(mark):
            encoding = candidate
            break
    text = data.decode(encoding, errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_text_file(path):
    """
    Read a text-based submission file.
    """

    return decode_text(path.read_bytes())


def parse_brightspace_folder_name(folder_name):
    """
    Parse a Brightspace submission folder name.

    Example:
    100001-2000001 - astudent Avery Student - Jul 30, 2026 1053 PM

    Returns:
        {
            "username": "astudent",
            "full_name": "Avery Student",
            "submitted_at": datetime(...),
            "brightspace_prefix": "100001-2000001"
        }

    Returns None if the folder name does not match the expected format.
    """

    name = folder_name.strip()

    # Allow parsing of a ZIP filename as well as an extracted folder name.
    if name.lower().endswith(".zip"):
        name = name[:-4]

    parts = name.split(" - ")

    if len(parts) < 3:
        return None

    brightspace_prefix = parts[0].strip()
    identity = parts[1].strip()
    date_text = " - ".join(parts[2:]).strip()

    identity_parts = identity.split()

    if len(identity_parts) < 2:
        return None

    username = identity_parts[0]
    full_name = " ".join(identity_parts[1:])

    submitted_at = None

    date_formats = [
        "%b %d, %Y %I%M %p",
        "%b %d, %Y %I:%M %p",
    ]

    for date_format in date_formats:
        try:
            submitted_at = datetime.strptime(
                date_text,
                date_format,
            )
            break
        except ValueError:
            pass

    return {
        "username": username,
        "full_name": full_name,
        "submitted_at": submitted_at,
        "brightspace_prefix": brightspace_prefix,
    }


def identity_variants(value):
    """Return common filename/text forms of a known identity string."""
    if not value:
        return []
    words = value.split()
    return sorted({
        value,
        "_".join(words),
        "-".join(words),
        "".join(words),
    }, key=len, reverse=True)


PLACEHOLDERS = ("<student_name>", "<username>", "<repo_owner>")
# A letter in any script; digits, underscores, spaces and punctuation are not.
LETTER = r"[^\W\d_]"


def name_parts(full_name):
    """
    The separate words of a full name (split at spaces and hyphens), for
    redacting a first name or surname that appears alone. Parts with fewer
    than two letters, such as a middle initial, are left out.
    """

    if not full_name:
        return []
    parts = {part for part in re.split(r"[\s\-]+", full_name)
             if sum(character.isalpha() for character in part) >= 2}
    # Longest first, so a longer part wins over one it contains.
    return sorted(parts, key=lambda part: (-len(part), part))


def redact_name_parts(text, full_name):
    """
    Replace each name part as a whole token, ignoring case: a letter on
    either side means no match, so "Chen" leaves "kitchen" alone, while
    "chen_prj1.ipynb" and "Chen," are redacted. Placeholders already in the
    text are kept as they are.
    """

    parts = name_parts(full_name)
    if not parts:
        return text
    pattern = re.compile(
        "(" + "|".join(re.escape(item) for item in PLACEHOLDERS) + ")"
        + f"|(?<!{LETTER})(?:" + "|".join(re.escape(part) for part in parts)
        + f")(?!{LETTER})",
        re.IGNORECASE,
    )
    return pattern.sub(
        lambda match: match.group(1) or "<student_name>",
        text,
    )


def redact_identity(text, username, full_name):
    """Replace known identity strings in agent-facing text."""
    replacements = (
        (identity_variants(full_name), "<student_name>"),
        (identity_variants(username), "<username>"),
    )
    for variants, placeholder in replacements:
        for value in variants:
            text = re.sub(
                re.escape(value),
                placeholder,
                text,
                flags=re.IGNORECASE,
            )
    # A first name or surname on its own, e.g. "<surname>_prj1_2.ipynb".
    return redact_name_parts(text, full_name)


def anonymize_relative_path(relative_path, index, username, full_name):
    """Create a deterministic, identity-free relative artifact path."""
    parts = []
    for part in relative_path.parts:
        safe_part = redact_identity(part, username, full_name)
        safe_part = safe_part.replace("<username>", "student")
        safe_part = safe_part.replace("<student_name>", "student")
        parts.append(safe_part)

    parts[-1] = f"artifact_{index:03d}_{parts[-1]}"
    return Path(*parts)


def merge_attempt_files(attempt_folders):
    """
    Return one student's submission files, merged across Brightspace
    attempts given oldest first.

    Every file of every attempt is included. When the same relative path
    (ignoring case) is in more than one attempt, the newest attempt's copy is
    kept. Entries are sorted by relative path, which fixes the artifact
    numbering; each holds the file's path, relative_path, attempt (1 is the
    oldest), source_folder and replaced_attempts (older attempts whose copy
    it replaced).
    """

    merged = {}
    for attempt_number, folder in enumerate(attempt_folders, start=1):
        folder = Path(folder)
        for path in sorted(item for item in folder.rglob("*") if item.is_file()):
            relative_path = path.relative_to(folder)
            key = relative_path.as_posix().casefold()
            earlier = merged.get(key)
            merged[key] = {
                "path": path,
                "relative_path": relative_path,
                "attempt": attempt_number,
                "source_folder": folder.name,
                "replaced_attempts": (
                    earlier["replaced_attempts"] + [earlier["attempt"]]
                    if earlier else []
                ),
            }
    return sorted(merged.values(), key=lambda entry: entry["relative_path"])


def identity_attempt_folders(project_path, identity):
    """A student's Brightspace attempt folders, oldest first, from student_map.json."""

    submissions_path = Path(project_path) / "submissions"
    return [submissions_path / attempt["source_folder"]
            for attempt in identity["attempts"]]


def inventory_submission_files(
    entries,
    username,
    full_name,
    destination_folder=None,
):
    """
    Inventory a student's merged submission files (see merge_attempt_files).
    """

    files = []

    for index, entry in enumerate(entries, start=1):
        path = entry["path"]
        relative_path = anonymize_relative_path(
            entry["relative_path"],
            index,
            username,
            full_name,
        )
        file_type = classify_file(path)

        file_info = {
            "path": relative_path.as_posix(),
            "file_type": file_type,
            "extension": path.suffix.lower(),
            "size_bytes": path.stat().st_size,
        }

        if file_type == "text":
            file_info["content"] = redact_identity(
                read_text_file(path),
                username,
                full_name,
            )
            file_info["size_bytes"] = len(
                file_info["content"].encode("utf-8")
            )

        if destination_folder is not None:
            destination_path = destination_folder / relative_path
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            if file_type == "text":
                destination_path.write_text(
                    file_info["content"],
                    encoding="utf-8",
                )
            else:
                shutil.copy2(path, destination_path)

        files.append(file_info)

    return files


def process_submissions(project_path, anonymized_root=None):
    """
    Parse Brightspace submission folders, group multiple attempts
    by Purdue username, merge each student's attempts (newest copy of
    a file wins), and assign anonymized student IDs.

    Returns:
        submissions:
            Agent-safe submission records using Student_### IDs.

        student_map:
            Local-only mapping containing username, full name,
            Brightspace folder names, attempt metadata, and the attempt
            each file came from.
    """

    project_path = Path(project_path).resolve()
    submissions_path = project_path / "submissions"

    if not submissions_path.exists():
        raise FileNotFoundError(
            f"Submissions folder does not exist: "
            f"{submissions_path}"
        )

    submission_folders = sorted(
        path
        for path in submissions_path.iterdir()
        if path.is_dir()
    )

    grouped = {}
    unparsed_folders = []

    for folder in submission_folders:
        parsed = parse_brightspace_folder_name(
            folder.name
        )

        if parsed is None:
            unparsed_folders.append(folder.name)
            continue

        username = parsed["username"]

        attempt = {
            "folder": folder,
            "folder_name": folder.name,
            "full_name": parsed["full_name"],
            "submitted_at": parsed["submitted_at"],
            "brightspace_prefix": parsed["brightspace_prefix"],
        }

        grouped.setdefault(
            username,
            [],
        ).append(attempt)

    submissions = []
    student_map = {}

    # Sorting by username makes Student_001 assignments deterministic.
    for index, username in enumerate(
        sorted(grouped),
        start=1,
    ):
        student_id = f"Student_{index:03d}"

        attempts = grouped[username]

        # Sort attempts chronologically.
        attempts.sort(
            key=lambda attempt: (
                attempt["submitted_at"]
                or datetime.min
            )
        )

        # The newest attempt gives the submission time and the name.
        newest_attempt = attempts[-1]

        destination_folder = None
        if anonymized_root is not None:
            destination_folder = Path(anonymized_root) / student_id

        entries = merge_attempt_files(
            attempt["folder"] for attempt in attempts
        )
        files_replaced = sum(1 for entry in entries if entry["replaced_attempts"])

        files = inventory_submission_files(
            entries,
            username,
            newest_attempt["full_name"],
            destination_folder,
        )

        review_flags = []

        if len(attempts) > 1:
            review_flags.append(
                f"Multiple submissions detected: "
                f"{len(attempts)} attempts. "
                f"Files from all attempts were merged; "
                f"{files_replaced} file(s) present in more than one attempt "
                f"use the newest copy."
            )

        unparsed_times = sum(
            1 for attempt in attempts if attempt["submitted_at"] is None
        )
        if newest_attempt["submitted_at"] is None:
            review_flags.append(
                "Submission timestamp could not be parsed."
            )
        elif unparsed_times:
            review_flags.append(
                f"{unparsed_times} attempt timestamp(s) could not be parsed; "
                "those attempts were treated as the oldest."
            )

        # Agent-safe record.
        submissions.append(
            {
                "student_id": student_id,
                "attempt_count": len(attempts),
                "files_replaced_by_newer_attempts": files_replaced,
                "file_count": len(files),
                "files": files,
                "review_flags": review_flags,
            }
        )

        # Private local mapping.
        student_map[student_id] = {
            "username": username,
            "full_name": newest_attempt["full_name"],
            "newest_source_folder": (
                newest_attempt["folder_name"]
            ),
            "attempt_count": len(attempts),
            "attempts": [
                {
                    "source_folder": attempt["folder_name"],
                    "submitted_at": (
                        attempt["submitted_at"].isoformat()
                        if attempt["submitted_at"]
                        else None
                    ),
                }
                for attempt in attempts
            ],
            # Which attempt each anonymized file came from.
            "files": [
                {
                    "artifact_path": file_info["path"],
                    "original_path": entry["relative_path"].as_posix(),
                    "attempt": entry["attempt"],
                    "source_folder": entry["source_folder"],
                    "replaced_attempts": entry["replaced_attempts"],
                }
                for entry, file_info in zip(entries, files)
            ],
        }

    return (
        submissions,
        student_map,
        unparsed_folders,
    )


def validate_anonymized_manifest(manifest, student_map):
    """Ensure known identities and source paths are absent from AI-facing JSON."""
    serialized = json.dumps(manifest, ensure_ascii=False).casefold()
    forbidden = []

    for student in student_map.values():
        forbidden.extend(identity_variants(student["username"]))
        forbidden.extend(identity_variants(student["full_name"]))
        forbidden.append(student["newest_source_folder"])
        forbidden.extend(
            attempt["source_folder"]
            for attempt in student["attempts"]
        )

    leaked = [
        value
        for value in forbidden
        if value and value.casefold() in serialized
    ]
    if leaked:
        raise RuntimeError(
            "Anonymized submission manifest contains identity-bearing data."
        )

    return True


def tree_signature(root):
    """Return relative paths, sizes, and hashes for a generated artifact tree."""
    signature = {}
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        signature[path.relative_to(root).as_posix()] = {
            "size_bytes": path.stat().st_size,
            "sha256": digest.hexdigest(),
        }
    return signature


def publish_anonymized_tree(temporary_path, anonymized_path):
    """Publish a verified copy without relying on Windows directory rename."""
    temporary_path = Path(temporary_path).resolve()
    anonymized_path = Path(anonymized_path).resolve()
    if anonymized_path.exists():
        raise FileExistsError(
            "Refusing to overwrite anonymized submissions: "
            f"{anonymized_path}"
        )

    expected_signature = tree_signature(temporary_path)
    try:
        shutil.copytree(temporary_path, anonymized_path)
        actual_signature = tree_signature(anonymized_path)
        if actual_signature != expected_signature:
            raise RuntimeError(
                "Published anonymized artifact tree failed integrity verification."
            )
    except Exception:
        if anonymized_path.exists():
            shutil.rmtree(anonymized_path)
        raise

    try:
        shutil.rmtree(temporary_path)
    except OSError:
        # The published tree is complete and verified. A locked staging copy may
        # be cleaned up manually without affecting the grading boundary.
        pass


# Grading output that refers to the current Student_### numbering. While any
# of it exists, the prepared submissions must not be replaced.
GRADING_EVIDENCE_FOLDERS = ("grading_runs", "debug_responses")


class SubmissionsAlreadyGradedError(FileExistsError):
    """prepare-submissions stopped because grading has used the prepared submissions."""


def grading_started(grader_path):
    """Return the first grading folder that holds anything, or None."""

    for folder_name in GRADING_EVIDENCE_FOLDERS:
        path = Path(grader_path) / folder_name
        if path.is_dir() and any(path.iterdir()):
            return path
    return None


def previous_preparation_paths(grader_path):
    """Files and folders an earlier prepare-submissions run wrote, if any remain."""

    from ai_grader.repo_evidence import (
        REPOSITORY_CURRENT_FOLDER, REPOSITORY_MAP_FILENAME,
    )
    grader_path = Path(grader_path)
    # Manifest first: once it is gone, a partly removed tree is never graded.
    candidates = [
        grader_path / "submission_manifest.json",
        grader_path / "student_map.json",
        grader_path / REPOSITORY_MAP_FILENAME,
        grader_path / ANONYMIZED_SUBMISSIONS_FOLDER,
        grader_path / REPOSITORY_CURRENT_FOLDER,
    ]
    return [path for path in candidates if path.exists()]


def remove_previous_preparation(paths):
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def write_submission_manifest(project_path):
    """
    Process submissions and save:

    1. submission_manifest.json
       Safe for downstream AI grading.

    2. student_map.json
       Local-only identity mapping. Never send to the AI.

    Rerunning on a project that was prepared but not yet graded replaces the
    earlier anonymized output, manifest and maps, but only after the new
    output has been built and validated. Once grading has started
    (grading_runs\\ or debug_responses\\ holds anything), it refuses and
    changes nothing.
    """

    project_path = Path(project_path).resolve()

    grader_path = project_path / "grader"
    grader_path.mkdir(
        parents=True,
        exist_ok=True,
    )

    anonymized_path = grader_path / ANONYMIZED_SUBMISSIONS_FOLDER

    from ai_grader.repo_evidence import REPOSITORY_CURRENT_FOLDER
    current_path = grader_path / REPOSITORY_CURRENT_FOLDER

    previous_paths = previous_preparation_paths(grader_path)
    grading_path = grading_started(grader_path) if previous_paths else None
    if grading_path is not None:
        raise SubmissionsAlreadyGradedError(
            "Refusing to replace the prepared submissions: grading has already "
            f"used them ({grading_path}). Nothing was changed. Student numbers "
            "in grading runs and saved responses refer to the current "
            "preparation."
        )

    temporary_path = Path(tempfile.mkdtemp(
        prefix=".anonymized_submissions_",
        dir=grader_path,
    ))
    # Current repository states (not graded) are staged the same way.
    current_temporary = Path(tempfile.mkdtemp(
        prefix=".repository_current_",
        dir=grader_path,
    ))

    try:
        (
            submissions,
            student_map,
            unparsed_folders,
        ) = process_submissions(
            project_path,
            anonymized_root=temporary_path,
        )

        from ai_grader.spec_validation import latest_spec_path
        from ai_grader.submission_checks import original_filename_checks
        try:
            spec_path = latest_spec_path(grader_path)
        except FileNotFoundError:
            spec_path = None
        if spec_path is not None:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            for submission in submissions:
                identity = student_map[submission["student_id"]]
                submission["deliverable_checks"] = original_filename_checks(
                    merge_attempt_files(identity_attempt_folders(project_path, identity)),
                    identity["username"], identity["full_name"], spec,
                )

        from ai_grader.repo_evidence import (
            attach_repository_evidence, verify_repository_redaction,
        )
        repository_summary, repository_map = attach_repository_evidence(
            project_path, submissions, student_map, temporary_path,
            current_temporary,
        )

        manifest = {
            "manifest_version": "2.0",
            "anonymization": {
                "status": "validated",
                "artifact_root": "grader/anonymized_submissions",
                "known_text_identities_redacted": True,
                "image_pixels_redacted": False,
                "image_identity_limitation": (
                    "Image pixels were not redacted. Screenshots may require "
                    "instructor review if visible identity is suspected."
                ),
            },
            "submission_count": len(submissions),
            "unparsed_folder_count": len(unparsed_folders),
            "unparsed_folders": [
                f"Unparsed_{index:03d}"
                for index, _ in enumerate(unparsed_folders, start=1)
            ],
            "submissions": submissions,
        }
        if repository_summary is not None:
            manifest["repository_evidence"] = repository_summary

        validate_anonymized_manifest(manifest, student_map)
        if repository_map is not None:
            verify_repository_redaction(manifest, repository_map)
        # The new output is complete and validated; only now remove the old.
        remove_previous_preparation(previous_paths)
        # Publish the ungraded current state first, and undo it if the graded
        # tree fails, so a failed run leaves nothing that blocks a rerun.
        if any(current_temporary.iterdir()):
            publish_anonymized_tree(current_temporary, current_path)
        else:
            current_temporary.rmdir()
        try:
            publish_anonymized_tree(temporary_path, anonymized_path)
        except Exception:
            if current_path.exists():
                shutil.rmtree(current_path)
            raise
    except Exception:
        for staging in (temporary_path, current_temporary):
            if staging.exists():
                shutil.rmtree(staging)
        raise

    manifest_path = (
        grader_path
        / "submission_manifest.json"
    )

    map_path = (
        grader_path
        / "student_map.json"
    )

    with manifest_path.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            manifest,
            file,
            indent=2,
        )
        file.write("\n")

    with map_path.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            student_map,
            file,
            indent=2,
        )
        file.write("\n")

    if repository_map is not None:
        # Private, like student_map.json: repository owners and URLs.
        from ai_grader.repo_evidence import REPOSITORY_MAP_FILENAME
        (grader_path / REPOSITORY_MAP_FILENAME).write_text(
            json.dumps(repository_map, indent=2) + "\n", encoding="utf-8",
        )

    return (
        manifest_path,
        map_path,
        manifest,
    )
