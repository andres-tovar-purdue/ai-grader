import hashlib
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path


PROJECT_SUBFOLDERS = [
    "project",
    "datasets",
    "rubric",
    "reference",
    "submissions",
    "grader",
]

# Brightspace names each submission folder "<digits>-<digits> - <user> - <date>".
BRIGHTSPACE_FOLDER_PATTERN = re.compile(r"^\d+-\d+ - ")
BRIGHTSPACE_INDEX_NAME = "index.html"
EXTRACTION_STAGING_PREFIX = ".ai_grader_extract_"
HASH_CHUNK_SIZE = 1024 * 1024

# When the same submission is available from several places, prefer an
# already-extracted folder over extracting it again from the ZIP.
SOURCE_PREFERENCE = {"loose": 0, "wrapper": 1, "zip": 2}


class ProjectInitializationError(RuntimeError):
    """init-project stopped; the message says whether anything changed."""


def describe_move_counts(folder_count, index_count):
    """Return e.g. '5 submission folders, 1 index.html' without any names."""
    folders = f"{folder_count} submission folder{'' if folder_count == 1 else 's'}"
    return f"{folders}, {index_count} index.html"


def is_brightspace_folder_name(name):
    return bool(BRIGHTSPACE_FOLDER_PATTERN.match(name))


def is_index_name(name):
    return name.lower() == BRIGHTSPACE_INDEX_NAME


def count_items(plan, action):
    """Return (submission folder count, index.html count) for one action."""
    items = [item for item in plan["items"] if item["action"] == action]
    folders = sum(1 for item in items if item["kind"] == "folder")
    return folders, len(items) - folders


def sha256_of_stream(stream):
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(HASH_CHUNK_SIZE), b""):
        digest.update(chunk)
    return digest.hexdigest()


def path_files(path, kind):
    """Map relative file paths to file paths, or None if path is not the kind."""
    if kind == "index":
        return {"": path} if path.is_file() else None
    if not path.is_dir() or path.is_symlink():
        return None
    files = {}
    for directory, _, filenames in os.walk(path):
        for filename in filenames:
            file_path = Path(directory) / filename
            files[file_path.relative_to(path).as_posix()] = file_path
    return files


def source_sizes(source, kind):
    """Map relative file paths to sizes (directory and ZIP metadata only)."""
    if "sizes" not in source:
        if source["type"] == "zip":
            source["sizes"] = {rel: info.file_size for rel, info in source["files"].items()}
        else:
            files = path_files(source["path"], kind)
            source["sizes"] = None if files is None else {
                rel: file_path.stat().st_size for rel, file_path in files.items()
            }
    return source["sizes"]


def source_hashes(source, kind):
    """Map relative file paths to SHA-256 digests of their contents."""
    if "hashes" not in source:
        if source["type"] == "zip":
            with zipfile.ZipFile(source["zip"]["path"]) as archive:
                hashes = {}
                for rel, info in source["files"].items():
                    with archive.open(info) as stream:
                        hashes[rel] = sha256_of_stream(stream)
        else:
            hashes = {}
            for rel, file_path in path_files(source["path"], kind).items():
                with file_path.open("rb") as stream:
                    hashes[rel] = sha256_of_stream(stream)
        source["hashes"] = hashes
    return source["hashes"]


def same_contents(first, second, kind):
    """
    True if both sources hold the same file names with identical contents.
    Sizes are compared first, so files are hashed only when sizes all match.
    """
    first_sizes = source_sizes(first, kind)
    second_sizes = source_sizes(second, kind)
    if first_sizes is None or second_sizes is None or first_sizes != second_sizes:
        return False
    return source_hashes(first, kind) == source_hashes(second, kind)


def read_brightspace_zip(zip_path):
    """
    Read a ZIP's table of contents (not its files) and return the Brightspace
    items it holds, or None if it is not a Brightspace download.
    """

    try:
        with zipfile.ZipFile(zip_path) as archive:
            infos = archive.infolist()
    except (zipfile.BadZipFile, OSError):
        return None

    entries = {}
    other_count = 0

    for info in infos:
        name = info.filename.replace("\\", "/")
        parts = [part for part in name.split("/") if part]
        if not parts:
            continue

        top = parts[0]
        if top == "__MACOSX":
            continue

        if len(parts) == 1 and not info.is_dir() and is_index_name(top):
            kind, relative = "index", ""
        elif is_brightspace_folder_name(top) and (len(parts) > 1 or info.is_dir()):
            kind, relative = "folder", "/".join(parts[1:])
        else:
            if not info.is_dir():
                other_count += 1
            continue

        if (name.startswith("/") or ".." in parts or ":" in name
                or any(part != part.strip() for part in parts)):
            raise ProjectInitializationError(
                f"{zip_path.name} contains an unsafe or unusual file path. "
                "Nothing was changed. Extract it manually and rerun init-project."
            )
        if info.flag_bits & 0x1:
            raise ProjectInitializationError(
                f"{zip_path.name} is password-protected. Nothing was changed. "
                "Extract it manually and rerun init-project."
            )

        entry = entries.setdefault(top, {"kind": kind, "members": [], "files": {}})
        entry["members"].append(info)
        if not info.is_dir():
            entry["files"][relative] = info

    if not entries:
        return None

    return {"entries": entries, "other_count": other_count}


def add_source(groups, name, kind, source):
    group = groups.setdefault(name, {"kind": kind, "sources": []})
    if group["kind"] != kind:
        group["kind_clash"] = True
    group["sources"].append(source)


def plan_project_initialization(project_path):
    """
    Decide what init-project would do, without changing anything.

    Brightspace items can come from three places: folders at the top of the
    project, folders inside a top-level wrapper folder (Windows "Extract All"
    creates one named after the ZIP), and Brightspace ZIP downloads. Item
    names are kept in the plan for moving but must never be printed, because
    they contain student names.
    """

    project_path = Path(project_path)
    exists = project_path.exists()

    if exists and not project_path.is_dir():
        raise ProjectInitializationError(
            f"Project path exists but is not a folder: {project_path}"
        )

    plan = {
        "project_path": project_path,
        "exists": exists,
        "missing_subfolders": [],
        "items": [],
        "zips": [],
        "wrappers": [],
        "not_moved": [],
        "brightspace_named_not_moved": 0,
    }

    for folder_name in PROJECT_SUBFOLDERS:
        folder_path = project_path / folder_name
        if not folder_path.exists():
            plan["missing_subfolders"].append(folder_name)
        elif not folder_path.is_dir():
            raise ProjectInitializationError(
                f"{folder_name} exists in the project folder but is not a folder. "
                "Rename or remove it, then rerun init-project."
            )

    if not exists:
        return plan

    groups = {}

    for entry in sorted(project_path.iterdir(), key=lambda path: path.name.lower()):
        name = entry.name

        if name in PROJECT_SUBFOLDERS and entry.is_dir():
            continue

        if entry.is_symlink():
            plan["not_moved"].append(name)
        elif is_brightspace_folder_name(name):
            if entry.is_dir():
                add_source(groups, name, "folder", {"type": "loose", "path": entry})
            else:
                # Brightspace-named but not a folder: count it rather than
                # list it, so no student name is printed.
                plan["brightspace_named_not_moved"] += 1
        elif is_index_name(name) and entry.is_file():
            add_source(groups, name, "index", {"type": "loose", "path": entry})
        elif entry.is_file() and entry.suffix.lower() == ".zip":
            download = read_brightspace_zip(entry)
            if download is None:
                plan["not_moved"].append(name)
                continue
            zip_record = {"path": entry, "name": name, "extract_items": [],
                          "other_count": download["other_count"]}
            plan["zips"].append(zip_record)
            for top, contents in download["entries"].items():
                add_source(groups, top, contents["kind"], {
                    "type": "zip", "zip": zip_record,
                    "members": contents["members"],
                    "files": contents["files"],
                })
        elif entry.is_dir() and not name.startswith(EXTRACTION_STAGING_PREFIX):
            children = list(entry.iterdir())
            brightspace_children = [
                child for child in children
                if not child.is_symlink() and (
                    (child.is_dir() and is_brightspace_folder_name(child.name))
                    or (child.is_file() and is_index_name(child.name))
                )
            ]
            if not brightspace_children:
                plan["not_moved"].append(name)
                continue
            plan["wrappers"].append({
                "path": entry, "name": name, "removed": False,
                "remaining": len(children) - len(brightspace_children),
            })
            for child in brightspace_children:
                kind = "folder" if child.is_dir() else "index"
                add_source(groups, child.name, kind, {"type": "wrapper", "path": child})
        else:
            plan["not_moved"].append(name)

    submissions_path = project_path / "submissions"

    try:
        for name, group in sorted(groups.items(), key=lambda pair: pair[0].lower()):
            plan_item(plan, name, group, submissions_path)
    except (OSError, zipfile.BadZipFile) as error:
        # Comparing contents failed; OS messages include student-named paths.
        reason = getattr(error, "strerror", None) or "a ZIP could not be read"
        raise ProjectInitializationError(
            f"Could not compare submissions: {reason}. Nothing was changed. "
            "Close any program using the project folder and rerun init-project."
        ) from None

    # A wrapper folder is emptied only if everything Brightspace in it moves.
    moving_paths = {
        item["source"]["path"] for item in plan["items"]
        if item["action"] == "move" and item["source"]["type"] == "wrapper"
    }
    for wrapper in plan["wrappers"]:
        left_behind = [
            source for group in groups.values() for source in group["sources"]
            if source["type"] == "wrapper"
            and source["path"].parent == wrapper["path"]
            and source["path"] not in moving_paths
        ]
        wrapper["remaining"] += len(left_behind)

    return plan


def plan_item(plan, name, group, submissions_path):
    """Decide whether one submission is moved, extracted, skipped or a conflict."""
    kind = group["kind"]
    sources = sorted(group["sources"], key=lambda s: SOURCE_PREFERENCE[s["type"]])
    target = submissions_path / name

    if group.get("kind_clash"):
        action = "conflict"
    elif os.path.lexists(target):
        existing = {"type": "target", "path": target}
        same = all(same_contents(source, existing, kind) for source in sources)
        action = "already_present" if same else "conflict"
    elif not all(same_contents(source, sources[0], kind) for source in sources[1:]):
        action = "conflict"
    else:
        action = "extract" if sources[0]["type"] == "zip" else "move"

    item = {"name": name, "kind": kind, "source": sources[0], "action": action}
    plan["items"].append(item)
    if action == "extract":
        sources[0]["zip"]["extract_items"].append(item)


def check_free_space(plan):
    needed = sum(
        info.file_size
        for item in plan["items"] if item["action"] == "extract"
        for info in item["source"]["members"]
    )
    if needed and shutil.disk_usage(plan["project_path"]).free < needed * 1.1:
        raise ProjectInitializationError(
            f"Not enough free disk space to extract the ZIP ({needed:,} bytes "
            "needed). Nothing was changed."
        )


def extract_zip_items(plan, zip_record, submissions_path, done, total):
    """Extract this ZIP's selected items via a staging folder, then move them in."""
    if not zip_record["extract_items"]:
        return done

    staging = Path(tempfile.mkdtemp(
        prefix=EXTRACTION_STAGING_PREFIX, dir=plan["project_path"]
    ))
    try:
        with zipfile.ZipFile(zip_record["path"]) as archive:
            for item in zip_record["extract_items"]:
                for info in item["source"]["members"]:
                    archive.extract(info, staging)
                extracted = staging / item["name"]
                target = submissions_path / item["name"]
                if not extracted.exists() or os.path.lexists(target):
                    raise ProjectInitializationError(
                        f"Stopped after placing {done} of {total} item(s): an item "
                        f"from {zip_record['name']} could not be placed in "
                        "submissions\\. Nothing was overwritten. Rerun "
                        "init-project to continue."
                    )
                extracted.rename(target)
                done += 1
    except (OSError, zipfile.BadZipFile) as error:
        # OS messages include paths, which contain student names.
        reason = getattr(error, "strerror", None) or "the ZIP could not be read"
        raise ProjectInitializationError(
            f"Stopped after placing {done} of {total} item(s): {reason}. "
            "Nothing was overwritten. Rerun init-project to continue."
        ) from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return done


def is_projects_folder(path):
    return path.name.lower() == "projects"


def resolve_project_path(project_name, current_path):
    """
    Return (project folder, note) for init-project, so that running it from
    the wrong folder never creates projects\\projects\\NAME or NAME\\projects\\NAME.

    - From the workspace root: projects\\NAME (the normal case, no note).
    - From inside projects\\: .\\NAME.
    - From inside projects\\NAME itself: the current folder.
    - A path such as projects\\NAME: used as given if its parent is projects\\.
    Stops instead of guessing when the intent is unclear.
    """

    current_path = Path(current_path).resolve()
    name_path = Path(project_name)
    parts = name_path.parts

    if not parts or project_name.strip() in {".", ".."}:
        raise ProjectInitializationError(
            "Give a project name, for example: init-project mspe_49600_fa26_pr01"
        )

    if name_path.is_absolute() or len(parts) > 1:
        candidate = (current_path / name_path).resolve()
        if is_projects_folder(candidate.parent):
            return candidate, f"Using the project folder given as a path: {candidate}"
        raise ProjectInitializationError(
            f"{project_name} is a path, and its folder is not inside a projects "
            "folder. Run init-project from the workspace root with just the "
            f"project name, for example: init-project {name_path.name}"
        )

    if is_projects_folder(current_path):
        candidate = current_path / project_name
        nested = current_path / "projects" / project_name
        if nested.exists():
            raise ProjectInitializationError(
                "Stopped: you are inside a projects folder, and both "
                f"{candidate} and {nested} could be meant. Nothing was changed. "
                "Run init-project from the workspace root instead."
            )
        return candidate, (
            f"Running inside the projects folder, so using: {candidate}"
        )

    if (current_path.name == project_name
            and is_projects_folder(current_path.parent)):
        return current_path, (
            f"Running inside the project folder, so using it: {current_path}"
        )

    project_path = current_path / "projects" / project_name
    stray = current_path / project_name

    if stray.is_dir() and not project_path.exists():
        raise ProjectInitializationError(
            f"Stopped: found a folder named {project_name} here ({stray}), but "
            f"projects belong in {project_path}. Nothing was changed. Move the "
            f"folder into {current_path / 'projects'}, then rerun init-project. "
            "If you meant to start a separate new project, choose a different name."
        )

    return project_path, None


def init_project(project_name, workspace_path=None, dry_run=False):
    """
    Create a grading project beneath the workspace projects folder, or
    complete an existing project folder that holds a Brightspace download.

    For an existing folder, missing standard subfolders are created; Brightspace
    submission folders and index.html are moved into submissions\\, whether they
    sit at the top of the project, inside a folder created by extracting the
    download, or inside a Brightspace ZIP (which is extracted and kept).
    Everything else is left in place. Nothing is overwritten: if submissions\\
    already holds a different item with the same name, nothing is changed.
    Items already present with the same file names and identical contents
    (SHA-256) are skipped, so rerunning is safe. With dry_run=True, only the plan is returned.

    Example:
        init_project("mspe_49600_fa26_pr01")
    """

    if workspace_path is None:
        workspace_path = Path.cwd()

    project_path, note = resolve_project_path(project_name, workspace_path)

    plan = plan_project_initialization(project_path)
    plan["location_note"] = note

    if dry_run:
        return plan

    conflicts = count_items(plan, "conflict")
    if any(conflicts):
        raise ProjectInitializationError(
            "Stopped: these already exist in submissions\\ (or appear twice) "
            "with different contents: "
            + describe_move_counts(*conflicts)
            + ". Nothing was changed. Compare them with submissions\\, remove "
            "or rename the duplicates, then rerun init-project."
        )

    check_free_space(plan)

    project_path.mkdir(parents=True, exist_ok=True)

    for folder_name in plan["missing_subfolders"]:
        (project_path / folder_name).mkdir()

    submissions_path = project_path / "submissions"
    to_move = [item for item in plan["items"] if item["action"] == "move"]
    total = len(to_move) + sum(count_items(plan, "extract"))
    done = 0

    for item in to_move:
        target = submissions_path / item["name"]
        if os.path.lexists(target):
            raise ProjectInitializationError(
                f"Stopped after placing {done} of {total} item(s): an item "
                "appeared in submissions\\ during the move. Nothing was "
                "overwritten. Rerun init-project to continue."
            )
        try:
            # rename moves the folder without opening any file in it.
            item["source"]["path"].rename(target)
        except OSError as error:
            raise ProjectInitializationError(
                f"Stopped after placing {done} of {total} item(s): "
                f"{error.strerror or 'the move failed'}. Close any program "
                "using the project folder and rerun init-project to continue."
            ) from None
        done += 1

    for zip_record in plan["zips"]:
        done = extract_zip_items(plan, zip_record, submissions_path, done, total)

    for wrapper in plan["wrappers"]:
        if wrapper["remaining"] == 0:
            try:
                wrapper["path"].rmdir()
                wrapper["removed"] = True
            except OSError:
                pass

    return plan
