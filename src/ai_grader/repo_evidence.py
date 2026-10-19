"""
GitHub repositories linked from submissions, as grading evidence.

Brightspace writes submissions\\index.html with each submission's text, which
is where students paste repository links. When that file contains GitHub
repository URLs, prepare-submissions:

1. maps each URL to Student_### through the in-memory student map;
2. clones the repository without credentials (no credential helpers, no user
   or system git config, no prompts);
3. copies the tracked files into the student's anonymized folder with the
   same identity redaction as other submission files (plus the repository
   owner), skipping tracked environment folders such as .venv; and
4. writes an objective facts file for the student.

Nothing here is course-specific, and nothing happens without repository URLs.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path

from ai_grader.submission_processing import (
    anonymize_relative_path,
    classify_file,
    identity_variants,
    read_text_file,
    redact_identity,
)


INDEX_FILENAME = "index.html"
REPOSITORY_MAP_FILENAME = "repository_map.json"
REPOSITORY_SOURCE = "github_repository"

GITHUB_REPO_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
# github.com/<first segment> values that are site pages, not owners.
GITHUB_RESERVED_OWNERS = {
    "about", "apps", "collections", "contact", "explore", "features", "login",
    "marketplace", "notifications", "orgs", "pricing", "pulls", "search",
    "settings", "site", "sponsors", "topics", "users",
}

ENVIRONMENT_FOLDER_NAMES = {".venv", "venv", "env", ".env", "virtualenv",
                            ".virtualenv", ".conda"}
ENVIRONMENT_MARKER_FILES = {"pyvenv.cfg"}
ENVIRONMENT_MARKER_FOLDERS = {"conda-meta"}

GIT_TIMEOUT_SECONDS = 180
MAX_REPO_FILE_BYTES = 5 * 1024 * 1024
MAX_REPO_COPY_BYTES = 50 * 1024 * 1024
MAX_LISTED_FILES = 2000
MAX_LISTED_COMMITS = 1000

NOT_PUBLIC_MARKERS = (
    "repository not found", "could not read username", "authentication failed",
    "terminal prompts disabled", "returned error: 401", "returned error: 403",
    "returned error: 404",
)
NETWORK_MARKERS = (
    "could not resolve host", "failed to connect", "connection timed out",
    "connection reset", "connection refused", "unable to access",
)


class RepositoryEvidenceError(RuntimeError):
    """Repository evidence could not be prepared safely."""


# ---------------------------------------------------------------- index.html

class BrightspaceIndexParser(HTMLParser):
    """
    Split index.html into submission blocks.

    Brightspace starts each submission with a table row whose cell spans the
    table (colspan); the rows that follow hold the submission text and links.
    Without such rows, the whole file is treated as one block.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.current = None
        self.in_header = False

    def block(self):
        if self.current is None:
            self.current = {"header": [], "text": [], "links": []}
            self.blocks.append(self.current)
        return self.current

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "td" and "colspan" in attributes:
            self.current = None
            self.block()
            self.in_header = True
        if tag == "a" and attributes.get("href"):
            self.block()["links"].append(attributes["href"])

    def handle_endtag(self, tag):
        if tag == "td":
            self.in_header = False

    def handle_data(self, data):
        if not data.strip():
            return
        block = self.block()
        block["text"].append(data)
        if self.in_header:
            block["header"].append(data)


def find_github_repositories(text):
    """Return unique (owner, repo) pairs from GitHub repository URLs in text."""
    found = []
    seen = set()
    for match in GITHUB_REPO_PATTERN.finditer(text):
        owner, repo = match.group(1), match.group(2).rstrip(".")
        if repo.lower().endswith(".git"):
            repo = repo[:-4]
        if (not repo or repo in {".", ".."}
                or owner.lower() in GITHUB_RESERVED_OWNERS):
            continue
        key = (owner.lower(), repo.lower())
        if key not in seen:
            seen.add(key)
            found.append((owner, repo))
    return found


def parse_index(html_text):
    """Return submission blocks with their header, all text and repositories."""
    parser = BrightspaceIndexParser()
    parser.feed(html_text)
    parser.close()
    blocks = []
    for block in parser.blocks:
        text = " ".join(block["text"])
        repositories = find_github_repositories(" ".join(block["links"] + [text]))
        blocks.append({
            "header": " ".join(block["header"]),
            "text": text,
            "repositories": repositories,
        })
    return blocks


def name_forms(identity):
    """Ways a student's name or username may appear in index.html."""
    full_name = identity.get("full_name") or ""
    words = full_name.split()
    forms = set(identity_variants(full_name)) | set(identity_variants(identity.get("username")))
    if len(words) >= 2:
        given, family = " ".join(words[:-1]), words[-1]
        forms |= {f"{family}, {given}", f"{family} {given}"}
    return {form for form in forms if len(form) >= 2}


def mentions(text, forms):
    return any(
        re.search(r"(?<![A-Za-z0-9])" + re.escape(form) + r"(?![A-Za-z0-9])",
                  text, re.IGNORECASE)
        for form in forms
    )


def match_block(block, student_map):
    """
    Return the one Student_### a block belongs to, or None.

    The header is checked first; if it names nobody, the whole block is.
    Two or more matches are ambiguous and return None.
    """
    forms = {student_id: name_forms(identity) for student_id, identity in student_map.items()}
    for text in (block["header"], block["text"]):
        matches = [sid for sid, student_forms in forms.items() if mentions(text, student_forms)]
        if len(matches) == 1:
            return matches[0]
        if matches:
            return None
    return None


def map_repositories(index_path, student_map):
    """
    Map GitHub repositories in index.html to students.

    Returns (repositories by student ID, counts), or (None, None) when the
    file is missing or has no GitHub repository URLs.
    """
    index_path = Path(index_path)
    if not index_path.is_file():
        return None, None

    blocks = parse_index(index_path.read_text(encoding="utf-8", errors="replace"))
    repo_blocks = [block for block in blocks if block["repositories"]]
    if not repo_blocks:
        return None, None

    by_student = {}
    unmatched = 0
    for block in repo_blocks:
        student_id = match_block(block, student_map)
        if student_id is None:
            unmatched += 1
            continue
        known = by_student.setdefault(student_id, [])
        for owner, repo in block["repositories"]:
            if (owner.lower(), repo.lower()) not in {(o.lower(), r.lower()) for o, r in known}:
                known.append((owner, repo))

    counts = {
        "index_blocks_with_repository_urls": len(repo_blocks),
        "index_blocks_not_matched_to_a_student": unmatched,
        "students_with_repository_urls": len(by_student),
        "students_without_repository_urls": len(student_map) - len(by_student),
        "repository_urls": sum(len(repos) for repos in by_student.values()),
    }
    return by_student, counts


# ---------------------------------------------------------------- fetching

def clone_url_for(owner, repo):
    """The only URL that is ever cloned, rebuilt from validated parts."""
    return f"https://github.com/{owner}/{repo}.git"


def git_environment():
    """An environment in which git cannot use or ask for any credentials."""
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        # Ignore user and system config: credential helpers, url.insteadOf
        # rewrites to SSH, and similar settings could supply credentials.
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    })
    return env


def run_git(args, cwd=None, timeout=GIT_TIMEOUT_SECONDS):
    return subprocess.run(
        ["git", "-c", "credential.helper=", *args],
        cwd=cwd, env=git_environment(), capture_output=True, timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def clone_repository(owner, repo, destination):
    """
    Clone without credentials. Returns (status, publicly_reachable).

    Git's error text is classified but never stored, since it includes the URL.
    """
    try:
        result = run_git([
            "-c", "core.symlinks=false", "-c", "core.autocrlf=false",
            "-c", "core.longpaths=true",
            "clone", "--quiet", "--no-tags", "--", clone_url_for(owner, repo),
            str(destination),
        ])
    except FileNotFoundError:
        return "git_not_available", None
    except subprocess.TimeoutExpired:
        return "timeout", None

    if result.returncode == 0:
        return "cloned", True
    message = result.stderr.decode("utf-8", errors="replace").lower()
    if any(marker in message for marker in NOT_PUBLIC_MARKERS):
        return "not_public_or_not_found", False
    if any(marker in message for marker in NETWORK_MARKERS):
        return "network_error", None
    return "clone_failed", None


def git_output(args, cwd):
    result = run_git(args, cwd=cwd)
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- redaction

def owner_forms(owner):
    forms = {owner, owner.replace("-", " "), owner.replace("-", "_"), owner.replace("-", "")}
    return {form for form in forms if len(form) >= 3}


def owner_pattern(owner):
    """Owner forms as whole words, so an owner like "data" leaves "dataset" alone."""
    forms = sorted(owner_forms(owner), key=len, reverse=True)
    return re.compile(
        r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(form) for form in forms)
        + r")(?![A-Za-z0-9])",
        re.IGNORECASE,
    )


def redact(text, identity, owner):
    text = redact_identity(text, identity["username"], identity["full_name"])
    return owner_pattern(owner).sub("<repo_owner>", text)


def redact_path(relative, identity, owner):
    """Redact a path the way submission filenames are: names become "student"."""
    parts = []
    for part in Path(relative).parts:
        part = redact(part, identity, owner).replace("<repo_owner>", "owner")
        parts.append(part.replace("<username>", "student").replace("<student_name>", "student"))
    return Path(*parts)


# ---------------------------------------------------------------- facts

def environment_folders(tracked):
    """Tracked folders that are Python/conda environments."""
    folders = set()
    for path in tracked:
        parts = path.split("/")
        for depth, part in enumerate(parts[:-1]):
            if part in ENVIRONMENT_FOLDER_NAMES:
                folders.add("/".join(parts[:depth + 1]))
                break
        if parts[-1] in ENVIRONMENT_MARKER_FILES and len(parts) > 1:
            folders.add("/".join(parts[:-1]))
        for depth, part in enumerate(parts[:-1]):
            if part in ENVIRONMENT_MARKER_FOLDERS and depth > 0:
                folders.add("/".join(parts[:depth]))
    return sorted(folders)


def inside(path, folders):
    return any(path == folder or path.startswith(folder + "/") for folder in folders)


def read_commits(clone_path):
    log = git_output(["log", "--format=%cI%x1f%B%x1e"], clone_path)
    if log is None:
        return []
    commits = []
    for record in log.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        committed_at, _, message = record.partition("\x1f")
        commits.append({"committed_at": committed_at, "message": message.strip()})
    return commits


def collect_facts(clone_path, identity, owner, repo):
    tracked_output = git_output(["ls-files", "-z"], clone_path) or ""
    tracked = [path for path in tracked_output.split("\0") if path]
    commits = read_commits(clone_path)
    env_folders = environment_folders(tracked)
    env_file_count = sum(1 for path in tracked if inside(path, env_folders))

    listed = [path for path in tracked if not inside(path, env_folders)]
    return tracked, env_folders, {
        "tracked_file_count": len(tracked),
        "tracked_files": [
            redact_path(path, identity, owner).as_posix()
            for path in listed[:MAX_LISTED_FILES]
        ],
        "tracked_files_not_listed": max(0, len(listed) - MAX_LISTED_FILES),
        "commit_count": len(commits),
        "commits": [
            {"committed_at": commit["committed_at"],
             "message": redact(commit["message"], identity, owner)}
            for commit in commits[:MAX_LISTED_COMMITS]
        ],
        "commits_not_listed": max(0, len(commits) - MAX_LISTED_COMMITS),
        "environment_folders_tracked": bool(env_folders),
        "environment_folders": [
            redact_path(folder, identity, owner).as_posix() for folder in env_folders
        ],
        "environment_folder_file_count": env_file_count,
    }


# ---------------------------------------------------------------- copying

def copy_tracked_files(clone_path, tracked, env_folders, identity, owner,
                       student_root, folder_name, next_index):
    """Copy tracked files into the anonymized folder; return file records."""
    files = []
    skipped = {"environment_folder_files": 0, "too_large": 0,
               "over_total_limit": 0, "not_regular_files": 0}
    total = 0

    for path in tracked:
        if inside(path, env_folders):
            skipped["environment_folder_files"] += 1
            continue
        source = clone_path / path
        if source.is_symlink() or not source.is_file():
            skipped["not_regular_files"] += 1
            continue
        size = source.stat().st_size
        if size > MAX_REPO_FILE_BYTES:
            skipped["too_large"] += 1
            continue
        if total + size > MAX_REPO_COPY_BYTES:
            skipped["over_total_limit"] += 1
            continue
        total += size

        relative = anonymize_relative_path(
            Path(folder_name) / redact_path(path, identity, owner), next_index,
            identity["username"], identity["full_name"],
        )
        next_index += 1
        destination = student_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        file_type = classify_file(source)
        record = {
            "path": relative.as_posix(),
            "file_type": file_type,
            "extension": source.suffix.lower(),
            "size_bytes": size,
            "source": REPOSITORY_SOURCE,
        }
        if file_type == "text":
            record["content"] = redact(read_text_file(source), identity, owner)
            record["size_bytes"] = len(record["content"].encode("utf-8"))
            destination.write_text(record["content"], encoding="utf-8")
        else:
            shutil.copyfile(source, destination)
        files.append(record)

    return files, skipped, next_index


def remove_tree(path):
    """Remove a clone, including git's read-only object files on Windows."""
    def make_writable_and_retry(function, target, _):
        os.chmod(target, stat.S_IWRITE)
        function(target)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable_and_retry)
    else:
        shutil.rmtree(path, onerror=make_writable_and_retry)


# ---------------------------------------------------------------- per student

def add_repository_evidence(submission, identity, repositories, student_root, work_path):
    """Fetch one student's repositories and add their evidence to the submission."""
    student_root.mkdir(parents=True, exist_ok=True)
    next_index = len(submission["files"]) + 1
    added = []
    statuses = []

    for number, (owner, repo) in enumerate(repositories, start=1):
        suffix = "" if len(repositories) == 1 else f"_{number}"
        folder_name = f"github_repo{suffix}"
        clone_path = Path(tempfile.mkdtemp(prefix="repo_", dir=work_path))
        try:
            status, reachable = clone_repository(owner, repo, clone_path / "checkout")
            facts = {
                "evidence_type": "github_repository_facts",
                "collected_by": "AI Grader: git clone without credentials",
                "repository_name": redact(repo, identity, owner),
                "repository_owner": "<repo_owner>",
                "publicly_reachable": reachable,
                "fetch_status": status,
                "files_folder": folder_name if status == "cloned" else None,
            }
            if status == "cloned":
                checkout = clone_path / "checkout"
                tracked, env_folders, repo_facts = collect_facts(
                    checkout, identity, owner, repo)
                files, skipped, next_index = copy_tracked_files(
                    checkout, tracked, env_folders, identity, owner,
                    student_root, folder_name, next_index)
                facts.update(repo_facts)
                facts["copied_file_count"] = len(files)
                facts["files_not_copied"] = skipped
                added.extend(files)
        finally:
            remove_tree(clone_path)

        facts["identity_redaction_note"] = (
            "Known student names, usernames and the repository owner were "
            "redacted. Other handles in names or text cannot be detected."
        )
        facts_path = Path(f"artifact_{next_index:03d}_{folder_name}_facts.json")
        next_index += 1
        content = json.dumps(facts, indent=2, ensure_ascii=False) + "\n"
        (student_root / facts_path).write_text(content, encoding="utf-8")
        added.append({
            "path": facts_path.as_posix(),
            "file_type": "text",
            "extension": ".json",
            "size_bytes": len(content.encode("utf-8")),
            "content": content,
            "source": REPOSITORY_SOURCE,
        })
        statuses.append(status)

    submission["files"].extend(added)
    submission["file_count"] = len(submission["files"])
    return statuses


def verify_repository_redaction(manifest, repository_map):
    """Fail if a repository owner appears in that student's repository evidence."""
    for submission in manifest["submissions"]:
        owners = repository_map.get(submission["student_id"], [])
        evidence = json.dumps(
            [file for file in submission["files"] if file.get("source") == REPOSITORY_SOURCE],
            ensure_ascii=False,
        )
        for entry in owners:
            if owner_pattern(entry["owner"]).search(evidence):
                raise RepositoryEvidenceError(
                    "Repository evidence contains an unredacted repository owner."
                )


def attach_repository_evidence(project_path, submissions, student_map, anonymized_root):
    """
    Add GitHub repository evidence when index.html links to repositories.

    Returns (summary counts for the manifest, private repository map), or
    (None, None) when there are no repository URLs.
    """
    project_path = Path(project_path)
    by_student, counts = map_repositories(
        project_path / "submissions" / INDEX_FILENAME, student_map)
    if by_student is None:
        return None, None

    work_path = Path(tempfile.mkdtemp(prefix=".repo_fetch_",
                                      dir=project_path / "grader"))
    statuses = []
    try:
        for submission in submissions:
            repositories = by_student.get(submission["student_id"])
            if not repositories:
                continue
            statuses.extend(add_repository_evidence(
                submission, student_map[submission["student_id"]], repositories,
                Path(anonymized_root) / submission["student_id"], work_path,
            ))
    finally:
        remove_tree(work_path)

    counts["fetch_status_counts"] = {
        status: statuses.count(status) for status in sorted(set(statuses))
    }
    repository_map = {
        student_id: [
            {"owner": owner, "repository": repo,
             "url": f"https://github.com/{owner}/{repo}"}
            for owner, repo in repositories
        ]
        for student_id, repositories in sorted(by_student.items())
    }
    return counts, repository_map
