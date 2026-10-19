"""
GitHub repositories linked from submissions, as grading evidence.

Brightspace writes submissions\\index.html with each submission's text, which
is where students paste repository links. When that file contains GitHub
links, prepare-submissions:

1. maps each index.html entry to Student_### through the in-memory student
   map, and records for every student without a recognized repository link
   why not (counts and reason codes only);
2. clones each linked repository without credentials (no credential helpers,
   no user or system git config, no prompts);
3. evaluates the last commit at or before the student's Brightspace
   submission time: its tracked files go to the anonymized folder with the
   same identity redaction as other submission files (plus the repository
   owner), skipping tracked environment folders such as .venv. The current
   state is kept separately for the instructor and is not graded;
4. writes an objective facts file per student; and
5. when a link fails or is only a profile link, lists the owner's public
   repositories whose names match the project, for instructor review only.

Nothing here is course-specific, and nothing happens without GitHub links.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

from ai_grader.submission_processing import (
    anonymize_relative_path,
    classify_file,
    decode_text,
    identity_variants,
    read_text_file,
    redact_identity,
)


INDEX_FILENAME = "index.html"
REPOSITORY_MAP_FILENAME = "repository_map.json"
REPOSITORY_CURRENT_FOLDER = "repository_current"
REPOSITORY_SOURCE = "github_repository"
GRADED_FOLDER = "github_repo"

OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
REPO = r"[A-Za-z0-9._-]+"
# github.com/owner[/repo], with or without https:// or www.
GITHUB_LINK_PATTERN = re.compile(
    rf"(?:https?://)?(?:www\.)?github\.com/({OWNER})(?:/({REPO}))?(?![A-Za-z0-9-])",
    re.IGNORECASE,
)
GITHUB_SSH_PATTERN = re.compile(rf"git@github\.com:({OWNER})/({REPO})", re.IGNORECASE)
GITHUB_MENTION_PATTERN = re.compile(r"github\.com", re.IGNORECASE)
OTHER_URL_PATTERN = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
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
API_TIMEOUT_SECONDS = 30
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

# Why a student has no recognized repository link (reported by Student_###).
NO_LINK_REASONS = {
    "github_profile_link_only": "GitHub profile link without a repository",
    "github_link_not_recognized": "mentions github.com but no owner/repository link",
    "non_github_link_only": "links only to sites other than GitHub",
    "no_link_in_submission_text": "no link in the submission text",
    "no_index_entry": "no entry in index.html",
}


class RepositoryEvidenceError(RuntimeError):
    """Repository evidence could not be prepared safely."""


# ---------------------------------------------------------------- index.html

class BrightspaceIndexParser(HTMLParser):
    """
    Split index.html into submission entries.

    Brightspace starts each submission with a table row whose cell spans the
    table (colspan); the rows that follow hold the submission text and links.
    Without such rows, the whole file is treated as one entry.
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


def find_github_links(text):
    """
    Return (repositories, profile owners) found in text.

    Repositories are unique (owner, repo) pairs from https, www, bare
    github.com/owner/repo and git@github.com:owner/repo links. Profiles are
    owners linked without a repository.
    """
    repositories, profiles = [], []
    seen_repos, seen_owners = set(), set()

    matches = [(m.group(1), m.group(2)) for m in GITHUB_LINK_PATTERN.finditer(text)]
    matches += [(m.group(1), m.group(2)) for m in GITHUB_SSH_PATTERN.finditer(text)]
    for owner, repo in matches:
        if owner.lower() in GITHUB_RESERVED_OWNERS:
            continue
        repo = (repo or "").rstrip(".")
        if repo.lower().endswith(".git"):
            repo = repo[:-4]
        if repo and repo not in {".", ".."}:
            key = (owner.lower(), repo.lower())
            if key not in seen_repos:
                seen_repos.add(key)
                repositories.append((owner, repo))
        elif owner.lower() not in seen_owners:
            seen_owners.add(owner.lower())
            profiles.append(owner)

    linked_owners = {owner.lower() for owner, _ in repositories}
    profiles = [owner for owner in profiles if owner.lower() not in linked_owners]
    return repositories, profiles


def find_github_repositories(text):
    """Return unique (owner, repo) pairs from GitHub repository links in text."""
    return find_github_links(text)[0]


def no_link_reason(text, profiles):
    if profiles:
        return "github_profile_link_only"
    if GITHUB_MENTION_PATTERN.search(text):
        return "github_link_not_recognized"
    if OTHER_URL_PATTERN.search(text):
        return "non_github_link_only"
    return "no_link_in_submission_text"


def parse_index(html_text):
    """Return submission entries with their header, text, links and reason."""
    parser = BrightspaceIndexParser()
    parser.feed(html_text)
    parser.close()
    has_headers = any(block["header"] for block in parser.blocks)

    entries = []
    for block in parser.blocks:
        if has_headers and not block["header"]:
            continue  # Text before the first submission, such as a title.
        text = " ".join(block["text"])
        combined = " ".join(block["links"] + [text])
        repositories, profiles = find_github_links(combined)
        entries.append({
            "header": " ".join(block["header"]),
            "text": text,
            "repositories": repositories,
            "profiles": profiles,
            "reason": None if repositories else no_link_reason(combined, profiles),
            "has_github_link": bool(repositories or profiles),
        })
    return entries


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
    Return the one Student_### an entry belongs to, or None.

    The header is checked first; if it names nobody, the whole entry is.
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


def read_index(index_path):
    return decode_text(Path(index_path).read_bytes())


def map_repositories(index_path, student_map):
    """
    Map index.html entries to students.

    Returns (links by student ID, counts), or (None, None) when the file is
    missing or has no GitHub links. Each student's record holds repositories,
    profile owners and, without a repository, the reason code.
    """
    index_path = Path(index_path)
    if not index_path.is_file():
        return None, None

    entries = parse_index(read_index(index_path))
    if not any(entry["has_github_link"] for entry in entries):
        return None, None

    by_student = {}
    unmatched_with_links = 0
    unmatched_without_links = 0
    for entry in entries:
        student_id = match_block(entry, student_map)
        if student_id is None:
            if entry["has_github_link"]:
                unmatched_with_links += 1
            else:
                unmatched_without_links += 1
            continue
        record = by_student.setdefault(
            student_id, {"repositories": [], "profiles": [], "reasons": []})
        known = {(o.lower(), r.lower()) for o, r in record["repositories"]}
        for owner, repo in entry["repositories"]:
            if (owner.lower(), repo.lower()) not in known:
                known.add((owner.lower(), repo.lower()))
                record["repositories"].append((owner, repo))
        for owner in entry["profiles"]:
            if owner.lower() not in {o.lower() for o in record["profiles"]}:
                record["profiles"].append(owner)
        if entry["reason"]:
            record["reasons"].append(entry["reason"])

    without_link = {}
    for student_id in sorted(student_map):
        record = by_student.get(student_id)
        if record is None:
            without_link[student_id] = "no_index_entry"
        elif not record["repositories"]:
            reasons = record["reasons"]
            # The most informative reason across the student's entries.
            without_link[student_id] = min(reasons, key=list(NO_LINK_REASONS).index)
            record["reason"] = without_link[student_id]

    counts = {
        "index_entries": len(entries),
        "index_entries_with_github_links": sum(e["has_github_link"] for e in entries),
        "index_entries_not_matched_to_a_student": unmatched_with_links + unmatched_without_links,
        "index_entries_with_github_links_not_matched": unmatched_with_links,
        "students_with_repository_urls": sum(
            1 for record in by_student.values() if record["repositories"]),
        "students_without_repository_urls": len(without_link),
        "students_without_recognized_repository_link": without_link,
        "repository_urls": sum(len(r["repositories"]) for r in by_student.values()),
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
            "clone", "--quiet", "--no-tags",
            # Saved in the clone, so later checkouts behave the same way.
            "--config", "core.symlinks=false", "--config", "core.autocrlf=false",
            "--config", "core.longpaths=true",
            "--", clone_url_for(owner, repo), str(destination),
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


def list_public_repositories(owner):
    """
    Names of the owner's public repositories, from an anonymous GitHub API
    request (no token, no credentials), or None if the lookup failed.
    """
    request = urllib.request.Request(
        f"https://api.github.com/users/{owner}/repos?per_page=100&type=owner",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ai-grader"},
    )
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    return [item["name"] for item in data
            if isinstance(item, dict) and isinstance(item.get("name"), str)]


# ---------------------------------------------------------------- candidates

def name_tokens(name):
    return [token for token in re.split(r"[^a-z0-9]+", name.lower()) if token]


def normalized(name):
    return "".join(name_tokens(name))


def matches_pattern(candidate, pattern):
    """
    True if a repository name matches a project/course name pattern.

    Separators are ignored (underscore, hyphen, dot, none), and the
    candidate may use any two or more of the pattern's parts in order:
    "MSPE49600-PR02" matches "mspe_49600_fa26_pr02".
    """
    target = normalized(candidate)
    if not target:
        return False
    if target == normalized(pattern):
        return True
    states = {(0, 0)}
    for token in name_tokens(pattern):
        states |= {(position + len(token), used + 1) for position, used in states
                   if target.startswith(token, position)}
    return any(position == len(target) and used >= 2 for position, used in states)


def find_candidates(owner, patterns, exclude=()):
    """Return (lookup status, [(repository, matched pattern)])."""
    names = list_public_repositories(owner)
    if names is None:
        return "lookup_failed", []
    # Exclude only the linked repository itself (GitHub names ignore case).
    # Its separator variants are exactly the candidates being looked for.
    excluded = {name.lower() for name in exclude}
    candidates = []
    for name in names:
        if name.lower() in excluded:
            continue
        pattern = next((p for p in patterns if matches_pattern(name, p)), None)
        if pattern is not None:
            candidates.append((name, pattern))
    return "looked_up", candidates


def project_patterns(project_path, linked_names=()):
    """Names a student's repository is likely to resemble."""
    patterns = list(linked_names) + [Path(project_path).name]
    try:
        from ai_grader.spec_validation import latest_spec_path
        spec = json.loads(latest_spec_path(Path(project_path) / "grader")
                          .read_text(encoding="utf-8"))
        patterns.append(spec["project"]["project_id"])
    except (FileNotFoundError, OSError, KeyError, ValueError, TypeError):
        pass
    unique = []
    for pattern in patterns:
        if pattern and normalized(pattern) not in {normalized(p) for p in unique}:
            unique.append(pattern)
    return unique


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


# ---------------------------------------------------------------- history

def submission_cutoff(identity):
    """
    The student's submission time from the selected Brightspace attempt, as
    an aware datetime at the end of that minute, or None if unknown.

    Brightspace folder names give local time to the minute; the machine's
    time zone is assumed.
    """
    selected = identity.get("selected_source_folder")
    for attempt in identity.get("attempts", []):
        if attempt.get("source_folder") == selected and attempt.get("submitted_at"):
            submitted = datetime.fromisoformat(attempt["submitted_at"])
            if submitted.tzinfo is None:
                submitted = submitted.astimezone()
            return submitted + timedelta(seconds=59)
    return None


def read_commits(clone_path):
    """All commits reachable from HEAD, newest first, with committer times."""
    log = git_output(["log", "--format=%H%x1f%ct%x1f%cI%x1f%B%x1e"], clone_path)
    if log is None:
        return []
    commits = []
    for record in log.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, timestamp, committed_at, message = record.split("\x1f", 3)
        commits.append({"sha": sha, "timestamp": int(timestamp),
                        "committed_at": committed_at, "message": message.strip()})
    return commits


def commit_fact(commit, identity, owner):
    return {"committed_at": commit["committed_at"],
            "message": redact(commit["message"], identity, owner)}


def commit_list(commits, identity, owner):
    return [commit_fact(commit, identity, owner) for commit in commits[:MAX_LISTED_COMMITS]]


# ---------------------------------------------------------------- files

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


def tree_facts(clone_path, identity, owner):
    """Tracked files and environment folders of the checked-out commit."""
    tracked_output = git_output(["ls-files", "-z"], clone_path) or ""
    tracked = [path for path in tracked_output.split("\0") if path]
    env_folders = environment_folders(tracked)
    listed = [path for path in tracked if not inside(path, env_folders)]
    return tracked, env_folders, {
        "tracked_file_count": len(tracked),
        "tracked_files": [
            redact_path(path, identity, owner).as_posix()
            for path in listed[:MAX_LISTED_FILES]
        ],
        "tracked_files_not_listed": max(0, len(listed) - MAX_LISTED_FILES),
        "environment_folders_tracked": bool(env_folders),
        "environment_folders": [
            redact_path(folder, identity, owner).as_posix() for folder in env_folders
        ],
        "environment_folder_file_count": sum(1 for p in tracked if inside(p, env_folders)),
    }


def copy_tracked_files(clone_path, tracked, env_folders, identity, owner,
                       destination_root, folder_name, next_index):
    """Copy tracked files with redaction; return (file records, skipped, next index)."""
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
        destination = destination_root / relative
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


# ---------------------------------------------------------------- per repository

def evaluate_clone(checkout, identity, owner, cutoff, student_root, current_root,
                   folder_name, next_index):
    """
    Choose the commit to grade, copy its files, and keep the current state.

    Returns (facts, graded file records, next index).
    """
    commits = read_commits(checkout)
    if not commits:
        return {"fetch_status": "public_but_empty", "commit_count": 0,
                "evaluated_state": "none: the repository has no commits",
                "evaluated_commit": None, "files_folder": None}, [], next_index

    head = commits[0]
    if cutoff is None:
        evaluated = head
        state = "latest commit: submission time unknown"
        before, after = commits, []
    else:
        before = [c for c in commits if c["timestamp"] <= cutoff.timestamp()]
        after = [c for c in commits if c["timestamp"] > cutoff.timestamp()]
        evaluated = max(before, key=lambda c: c["timestamp"]) if before else None
        state = ("last commit at or before submission" if evaluated
                 else "none: no commit at or before submission")

    facts = {
        "commit_count": len(commits),
        "evaluated_state": state,
        "evaluated_commit": commit_fact(evaluated, identity, owner) if evaluated else None,
        "commits_at_or_before_submission_count": len(before),
        "commits_at_or_before_submission": commit_list(before, identity, owner),
        "commits_after_submission_count": len(after),
        "commits_after_submission": commit_list(after, identity, owner),
        "files_folder": None,
    }

    # Current state first (HEAD is checked out after cloning). Not graded.
    tracked, env_folders, current = tree_facts(checkout, identity, owner)
    current["commit"] = commit_fact(head, identity, owner)
    current["same_as_evaluated_commit"] = evaluated is not None and evaluated["sha"] == head["sha"]
    current["graded"] = False
    if not current["same_as_evaluated_commit"]:
        copied, _, _ = copy_tracked_files(checkout, tracked, env_folders, identity, owner,
                                          current_root, folder_name, 1)
        current["kept_for_instructor_in"] = (
            f"grader/{REPOSITORY_CURRENT_FOLDER}/{current_root.name}/{folder_name}"
            if copied else None)
    facts["current_state"] = current

    if evaluated is None:
        return facts, [], next_index

    if evaluated["sha"] != head["sha"]:
        result = run_git(["checkout", "--quiet", "--detach", "--force", evaluated["sha"]],
                         cwd=checkout)
        if result.returncode != 0:
            raise RepositoryEvidenceError("Could not check out the evaluated commit.")
        tracked, env_folders, _ = tree_facts(checkout, identity, owner)

    _, _, evaluated_tree = tree_facts(checkout, identity, owner)
    files, skipped, next_index = copy_tracked_files(
        checkout, tracked, env_folders, identity, owner, student_root, folder_name, next_index)
    facts.update(evaluated_tree)
    facts["files_folder"] = folder_name
    facts["copied_file_count"] = len(files)
    facts["files_not_copied"] = skipped
    return facts, files, next_index


def add_facts_file(student_root, facts, folder_name, next_index):
    facts["identity_redaction_note"] = (
        "Known student names, usernames and repository owners were redacted. "
        "Other handles in names or text cannot be detected."
    )
    facts_path = Path(f"artifact_{next_index:03d}_{folder_name}_facts.json")
    content = json.dumps(facts, indent=2, ensure_ascii=False) + "\n"
    (student_root / facts_path).write_text(content, encoding="utf-8")
    return {
        "path": facts_path.as_posix(),
        "file_type": "text",
        "extension": ".json",
        "size_bytes": len(content.encode("utf-8")),
        "content": content,
        "source": REPOSITORY_SOURCE,
    }


def candidate_facts(owner, patterns, identity, exclude, private):
    """Look up candidates; private gets names and URLs, the facts only a count."""
    status, candidates = find_candidates(owner, patterns, exclude)
    private.extend({
        "owner": owner, "repository": name,
        "url": f"https://github.com/{owner}/{name}",
        "matched_pattern": pattern,
    } for name, pattern in candidates)
    return {
        "candidate_lookup": status,
        "candidate_repositories_for_instructor_review": len(candidates),
        "candidate_note": (
            "Public repositories of the same owner whose names match the "
            "project were listed privately for the instructor. They were not "
            "fetched and must not be graded."
        ),
    }


def add_repository_evidence(submission, identity, record, patterns, student_root,
                            current_root, work_path, private):
    """Fetch one student's repositories and add their evidence to the submission."""
    student_root.mkdir(parents=True, exist_ok=True)
    next_index = len(submission["files"]) + 1
    cutoff = submission_cutoff(identity)
    added, statuses = [], []
    repositories = record["repositories"]

    for number, (owner, repo) in enumerate(repositories, start=1):
        folder_name = GRADED_FOLDER if len(repositories) == 1 else f"{GRADED_FOLDER}_{number}"
        facts = {
            "evidence_type": "github_repository_facts",
            "collected_by": "AI Grader: git clone without credentials",
            "repository_name": redact(repo, identity, owner),
            "repository_owner": "<repo_owner>",
            "submission_time": cutoff.isoformat() if cutoff else None,
            "submission_time_basis": (
                "Brightspace submission time of the selected attempt (local "
                "time zone, end of that minute); commits compared by committer time"),
        }
        clone_path = Path(tempfile.mkdtemp(prefix="repo_", dir=work_path))
        try:
            status, reachable = clone_repository(owner, repo, clone_path / "checkout")
            facts["publicly_reachable"] = reachable
            if status == "cloned":
                history, files, next_index = evaluate_clone(
                    clone_path / "checkout", identity, owner, cutoff, student_root,
                    current_root, folder_name, next_index)
                status = history.pop("fetch_status", status)
                facts.update(history)
                added.extend(files)
            else:
                facts["files_folder"] = None
                facts.update(candidate_facts(
                    owner, patterns + [repo], identity, [repo], private))
            facts["fetch_status"] = status
        finally:
            remove_tree(clone_path)

        added.append(add_facts_file(student_root, facts, folder_name, next_index))
        next_index += 1
        statuses.append(status)

    if not repositories:
        facts = {
            "evidence_type": "github_repository_facts",
            "collected_by": "AI Grader",
            "repository_link": record["reason"],
            "repository_link_description": NO_LINK_REASONS[record["reason"]],
            "publicly_reachable": None,
            "fetch_status": "no_repository_link",
            "files_folder": None,
        }
        for owner in record["profiles"]:
            facts.update(candidate_facts(owner, patterns, identity, [], private))
        added.append(add_facts_file(student_root, facts, GRADED_FOLDER, next_index))
        statuses.append("no_repository_link")

    submission["files"].extend(added)
    submission["file_count"] = len(submission["files"])
    return statuses


def verify_repository_redaction(manifest, repository_map):
    """Fail if a repository owner appears in that student's repository evidence."""
    for submission in manifest["submissions"]:
        entry = repository_map.get(submission["student_id"]) or {}
        if isinstance(entry, list):  # Maps written before candidates existed.
            entry = {"repositories": entry}
        owners = {item["owner"] for key in ("repositories", "profiles", "candidates")
                  for item in entry.get(key, [])}
        evidence = json.dumps(
            [file for file in submission["files"] if file.get("source") == REPOSITORY_SOURCE],
            ensure_ascii=False,
        )
        for owner in owners:
            if owner_pattern(owner).search(evidence):
                raise RepositoryEvidenceError(
                    "Repository evidence contains an unredacted repository owner."
                )


def attach_repository_evidence(project_path, submissions, student_map,
                               anonymized_root, current_root):
    """
    Add GitHub repository evidence when index.html contains GitHub links.

    Returns (summary for the manifest, private repository map), or
    (None, None) when there are no GitHub links.
    """
    project_path = Path(project_path)
    by_student, counts = map_repositories(
        project_path / "submissions" / INDEX_FILENAME, student_map)
    if by_student is None:
        return None, None

    work_path = Path(tempfile.mkdtemp(prefix=".repo_fetch_",
                                      dir=project_path / "grader"))
    statuses = []
    private_map = {}
    candidate_counts = {}
    try:
        for submission in submissions:
            student_id = submission["student_id"]
            record = by_student.get(student_id) or {
                "repositories": [], "profiles": [], "reason": "no_index_entry"}
            private = []
            linked = [repo for _, repo in record["repositories"]]
            statuses.extend(add_repository_evidence(
                submission, student_map[student_id], record,
                project_patterns(project_path, linked),
                Path(anonymized_root) / student_id, Path(current_root) / student_id,
                work_path, private,
            ))
            private_map[student_id] = {
                "repositories": [
                    {"owner": owner, "repository": repo,
                     "url": f"https://github.com/{owner}/{repo}"}
                    for owner, repo in record["repositories"]
                ],
                "profiles": [{"owner": owner, "url": f"https://github.com/{owner}"}
                             for owner in record["profiles"]],
                "candidates": private,
            }
            if private:
                candidate_counts[student_id] = len(private)
    finally:
        remove_tree(work_path)

    counts["fetch_status_counts"] = {
        status: statuses.count(status) for status in sorted(set(statuses))
    }
    counts["candidate_repositories_for_instructor_review"] = candidate_counts
    return counts, private_map
