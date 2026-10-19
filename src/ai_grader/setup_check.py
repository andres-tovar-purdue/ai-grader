"""
check-setup: report whether this computer is ready to run AI Grader.

Expected layout (created by install.py at the repository root):

    <parent>\\
    ├── ai-grader\\      this repository
    │   ├── .venv\\
    │   └── .env
    └── projects\\

Never prints the API key; only whether one is set.
"""

import os
import sys
from pathlib import Path

from ai_grader.ai_client import DEFAULT_MODEL, test_connection


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def check(name, ok, detail):
    return {"name": name, "ok": ok, "detail": detail}


def check_venv(prefix=None, base_prefix=None, repository_root=REPOSITORY_ROOT):
    prefix = Path(prefix or sys.prefix)
    base_prefix = Path(base_prefix or sys.base_prefix)
    if prefix == base_prefix:
        return check("Virtual environment", False,
                     "Not active. Run install.py, then activate .venv.")
    expected = repository_root / ".venv"
    if prefix.resolve() != expected.resolve():
        return check("Virtual environment", True,
                     f"Active, but not the repository's .venv ({prefix}).")
    return check("Virtual environment", True, f"Active: {prefix}")


def check_env_file(repository_root=REPOSITORY_ROOT):
    env_path = repository_root / ".env"
    if env_path.is_file():
        return check(".env file", True, str(env_path))
    old_path = repository_root.parent / ".env"
    if old_path.is_file():
        return check(".env file", False,
                     f"Found at the old location {old_path}. "
                     f"Move it to {env_path}.")
    return check(".env file", False,
                 f"Missing: {env_path}. Run install.py to create it.")


def check_api_key():
    if os.getenv("OPENAI_API_KEY"):
        return check("OPENAI_API_KEY", True, "Set (not shown).")
    return check("OPENAI_API_KEY", False, "Not set. Add it to .env.")


def check_model():
    model = os.getenv("OPENAI_MODEL")
    if model:
        return check("OPENAI_MODEL", True, model)
    return check("OPENAI_MODEL", False,
                 f"Not set; {DEFAULT_MODEL} would be used. Add it to .env.")


def check_workspace(repository_root=REPOSITORY_ROOT, current_path=None):
    projects = repository_root.parent / "projects"
    if not projects.is_dir():
        return check("Projects folder", False,
                     f"Missing: {projects}. Run install.py to create it.")
    # init-project creates projects\NAME under the current folder.
    current_path = Path(current_path or Path.cwd()).resolve()
    if current_path != repository_root.parent.resolve():
        return check("Projects folder", True,
                     f"{projects}. Run commands from {repository_root.parent} "
                     "so init-project uses it.")
    return check("Projects folder", True, str(projects))


def check_api(connect=test_connection):
    if not os.getenv("OPENAI_API_KEY"):
        return check("API connection", False, "Skipped: no API key.")
    try:
        connect()
    except Exception as error:
        return check("API connection", False,
                     f"Failed: {type(error).__name__}: {error}")
    return check("API connection", True, "Reachable.")


def run_setup_checks(skip_api=False, connect=test_connection):
    checks = [
        check_venv(),
        check_env_file(),
        check_api_key(),
        check_model(),
        check_workspace(),
    ]
    if skip_api:
        checks.append(check("API connection", True, "Skipped (--skip-api)."))
    else:
        checks.append(check_api(connect))
    return checks
