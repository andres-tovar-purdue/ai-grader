"""
Extract ZIP archives that students submit, so their files can be graded like
any other submitted file.

Extraction is local and defensive: members are written only below the given
destination, and unsafe paths, links, encrypted members, oversized members
and operating-system clutter are skipped and counted. A ZIP inside a ZIP is
kept as a file, not extracted. Originals are never modified.
"""

import stat
import zipfile
import zlib
from pathlib import Path, PurePosixPath


ARCHIVE_EXTENSIONS = {".zip"}
# The extracted files of "files.zip" appear in "files.zip_contents/".
CONTENTS_FOLDER_SUFFIX = "_contents"

MAX_ARCHIVE_MEMBERS = 500
MAX_MEMBER_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 100 * 1024 * 1024

CLUTTER_FOLDERS = {"__macosx", "__pycache__", ".git"}
CLUTTER_FILES = {".ds_store", "thumbs.db", "desktop.ini"}

SKIP_REASONS = (
    "unsafe_path", "link", "encrypted", "too_large", "over_total_limit",
    "over_member_limit", "damaged", "clutter",
)

# Reading or writing one member can fail on its own: a damaged or
# unsupported entry, or a name this operating system cannot create.
MEMBER_ERRORS = (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError,
                 EOFError, ValueError, zlib.error)


def is_archive(path):
    return Path(path).suffix.lower() in ARCHIVE_EXTENSIONS


def contents_folder(relative_path):
    """Relative folder for the extracted files of an archive at relative_path."""
    relative_path = Path(relative_path)
    return relative_path.with_name(relative_path.name + CONTENTS_FOLDER_SUFFIX)


def safe_member_path(name):
    """
    The member's relative path, or None if it is absolute, has a drive
    letter, or climbs out of the archive with "..".
    """
    name = name.replace("\\", "/")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return None
    parts = [part for part in PurePosixPath(name).parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    return PurePosixPath(*parts)


def is_link(info):
    return stat.S_ISLNK(info.external_attr >> 16)


def is_clutter(member):
    lowered = [part.casefold() for part in member.parts]
    return (any(part in CLUTTER_FOLDERS for part in lowered[:-1])
            or lowered[-1] in CLUTTER_FILES
            or lowered[-1].startswith("._"))


def copy_limited(source, destination, limit):
    """Copy at most limit bytes; return False (and remove the file) if more."""
    written = 0
    with destination.open("wb") as target:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            written += len(chunk)
            if written > limit:
                break
            target.write(chunk)
    if written > limit:
        destination.unlink()
        return False, 0
    return True, written


def extract_archive(path, destination):
    """
    Extract a ZIP archive below destination.

    Returns {"status", "files", "skipped", "nested_archives"}: status is
    "extracted", "partly_extracted" (some members skipped for safety, size or
    damage; clutter alone does not count), "encrypted" (nothing readable) or
    "unreadable" (not a readable ZIP); files lists the extracted members' relative paths (sorted);
    skipped counts members by reason; nested_archives counts ZIPs inside
    (kept as files).
    """
    destination = Path(destination)
    skipped = dict.fromkeys(SKIP_REASONS, 0)
    files = []
    total = 0
    nested = 0

    try:
        with zipfile.ZipFile(path) as archive:
            members = [info for info in archive.infolist() if not info.is_dir()]
            for count, info in enumerate(members, start=1):
                member = safe_member_path(info.filename)
                if member is None:
                    skipped["unsafe_path"] += 1
                    continue
                if is_clutter(member):
                    skipped["clutter"] += 1
                    continue
                if count > MAX_ARCHIVE_MEMBERS:
                    skipped["over_member_limit"] += 1
                    continue
                if is_link(info):
                    skipped["link"] += 1
                    continue
                if info.flag_bits & 0x1:
                    skipped["encrypted"] += 1
                    continue
                if info.file_size > MAX_MEMBER_BYTES:
                    skipped["too_large"] += 1
                    continue
                if total + info.file_size > MAX_ARCHIVE_TOTAL_BYTES:
                    skipped["over_total_limit"] += 1
                    continue
                target = destination.joinpath(*member.parts)
                # Two members whose names differ only by case or separators.
                if target.exists():
                    skipped["unsafe_path"] += 1
                    continue
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # The declared size can lie; never write more than the limit.
                    with archive.open(info) as source:
                        ok, written = copy_limited(source, target, MAX_MEMBER_BYTES)
                except MEMBER_ERRORS:
                    try:
                        target.unlink(missing_ok=True)
                    except OSError:
                        pass
                    skipped["damaged"] += 1
                    continue
                if not ok:
                    skipped["too_large"] += 1
                    continue
                total += written
                files.append(Path(*member.parts))
                if is_archive(member.name):
                    nested += 1
    except MEMBER_ERRORS + (zipfile.LargeZipFile,):
        return {"status": "unreadable", "files": [],
                "skipped": skipped, "nested_archives": 0}

    problems = sum(count for reason, count in skipped.items() if reason != "clutter")
    if not files and skipped["encrypted"]:
        status = "encrypted"
    elif problems:
        status = "partly_extracted"
    else:
        status = "extracted"
    return {"status": status, "files": sorted(files), "skipped": skipped,
            "nested_archives": nested}


def problem_counts(skipped):
    """Skipped members other than clutter, by reason, for reports."""
    return {reason: count for reason, count in skipped.items()
            if count and reason != "clutter"}
