"""
First-run setup for AI Grader. Run from the repository root with any
Python 3.10 or newer, before a virtual environment exists:

    python install.py

It creates (each only if missing, so it is safe to rerun):

    <parent>\\
    ├── ai-grader\\      this repository
    │   ├── .venv\\      virtual environment with AI Grader installed (-e)
    │   └── .env         API key and model, from .env.example
    └── projects\\       local grading projects, outside the repository

then makes one test API request. An existing .env is never overwritten.
Standard library only.
"""

import getpass
import subprocess
import sys
import venv
from pathlib import Path


MIN_PYTHON = (3, 10)
REPOSITORY_ROOT = Path(__file__).resolve().parent
VENV_PATH = REPOSITORY_ROOT / ".venv"
ENV_PATH = REPOSITORY_ROOT / ".env"
ENV_EXAMPLE_PATH = REPOSITORY_ROOT / ".env.example"
PROJECTS_PATH = REPOSITORY_ROOT.parent / "projects"

CONNECTION_TEST = (
    "from ai_grader.ai_client import describe_model, test_connection\n"
    "print(describe_model())\n"
    "try:\n"
    "    print(test_connection())\n"
    "except Exception as error:\n"
    "    print(f'{type(error).__name__}: {error}')\n"
    "    raise SystemExit(1)\n"
)


class SetupError(Exception):
    pass


def check_python(version_info=sys.version_info):
    if tuple(version_info[:2]) < MIN_PYTHON:
        found = ".".join(str(part) for part in version_info[:3])
        raise SetupError(
            f"AI Grader needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer; "
            f"this is Python {found}. Install a newer Python and rerun "
            "install.py with it."
        )


def venv_python(venv_path, windows=(sys.platform == "win32")):
    if windows:
        return venv_path / "Scripts" / "python.exe"
    return venv_path / "bin" / "python"


def ensure_venv(venv_path, create=None):
    """Create the virtual environment unless it exists. Returns True if created."""

    python = venv_python(venv_path)
    if python.is_file():
        return False
    if venv_path.exists():
        raise SetupError(
            f"{venv_path} exists but has no {python.name}. It may be broken or "
            "from another computer. Delete it, then rerun install.py."
        )
    create = create or (lambda path: venv.create(path, with_pip=True))
    create(venv_path)
    return True


def install_package(python, repository_root, run=None):
    run = run or subprocess.run
    result = run([str(python), "-m", "pip", "install", "-e", str(repository_root)])
    if result.returncode != 0:
        raise SetupError(
            "pip could not install AI Grader. Read the pip output above, fix "
            "the problem, then rerun install.py."
        )


def confirm(question, ask=None):
    ask = ask or input
    try:
        answer = ask(f"{question} [Y/n] ").strip().lower()
    except EOFError:
        return False
    return answer in {"", "y", "yes"}


def ensure_projects_folder(projects_path, ask=None):
    """Return "exists", "created" or "skipped"."""

    if projects_path.is_dir():
        return "exists"
    if projects_path.exists():
        raise SetupError(f"{projects_path} exists but is not a folder.")
    if not confirm(f"Create the projects folder {projects_path}?", ask):
        return "skipped"
    projects_path.mkdir()
    return "created"


def prompt_required(label, ask):
    while True:
        try:
            value = ask(f"{label}: ").strip()
        except EOFError:
            raise SetupError(f"No {label} given; .env was not created.") from None
        if value:
            return value
        print(f"{label} is required.")


def fill_env_template(template, values):
    """Set KEY=value on each template line KEY= whose key is in values."""

    lines = []
    for line in template.splitlines():
        key, separator, _ = line.partition("=")
        if separator and key.strip() in values:
            line = f"{key.strip()}={values[key.strip()]}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def ensure_env_file(env_path, example_path, ask=None, ask_secret=None):
    """Create .env from .env.example unless it exists. Returns True if created."""

    ask = ask or input
    ask_secret = ask_secret or getpass.getpass
    if env_path.exists():
        return False
    template = example_path.read_text(encoding="utf-8")

    print()
    print(f"Creating {env_path}.")
    print("The API key is not shown while you type or paste it.")
    values = {
        "OPENAI_API_KEY": prompt_required("OPENAI_API_KEY", ask_secret),
        "OPENAI_MODEL": prompt_required("OPENAI_MODEL (for example gpt-5.4-mini)", ask),
    }
    try:
        name = ask("GRADER_INSTRUCTOR_NAME (who approves grading specs; Enter to skip): ").strip()
    except EOFError:
        name = ""
    if name:
        values["GRADER_INSTRUCTOR_NAME"] = name

    # "x" refuses to replace a file, even one created since the check above.
    with env_path.open("x", encoding="utf-8") as file:
        file.write(fill_env_template(template, values))
    return True


def test_api_connection(python, repository_root, run=None):
    """
    Return (ok, lines) from one test request made with the venv's Python:
    the model used and the reply, or the error.
    """

    run = run or subprocess.run
    result = run(
        [str(python), "-c", CONNECTION_TEST],
        cwd=str(repository_root), capture_output=True, text=True,
    )
    lines = (result.stdout or "").strip().splitlines()
    if result.returncode != 0 and not lines:
        # Failed before printing, e.g. AI Grader is not installed.
        lines = (result.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
    return result.returncode == 0, lines


def print_activation(repository_root):
    print("  cd ..")
    print(f"  .\\{repository_root.name}\\.venv\\Scripts\\Activate.ps1")


def print_next_steps(repository_root):
    parent = repository_root.parent
    print()
    print(f"Work from {parent} (the folder that holds {repository_root.name}\\ "
          "and projects\\). Activate the environment there with:")
    print()
    print_activation(repository_root)
    print()
    print("Next step:")
    print()
    print("  python -m ai_grader check-setup")
    print("  python -m ai_grader init-project PROJECT_ID")


def print_fix_steps(repository_root, env_path):
    """After a failed API test: everything else is set up, so no rerun is needed."""

    print()
    print(f"Fix OPENAI_API_KEY (or OPENAI_MODEL) in {env_path} with a text editor.")
    print("Then check again; there is no need to rerun install.py:")
    print()
    print_activation(repository_root)
    print("  python -m ai_grader check-setup")


def main():
    print("AI Grader setup")
    try:
        check_python()
        print(f"Python {sys.version.split()[0]}: OK")

        if ensure_venv(VENV_PATH):
            print(f"Created {VENV_PATH}")
        else:
            print(f"Using existing {VENV_PATH}")
        python = venv_python(VENV_PATH)

        print("Installing AI Grader into .venv (pip install -e .) ...")
        install_package(python, REPOSITORY_ROOT)

        status = ensure_projects_folder(PROJECTS_PATH)
        if status == "skipped":
            print(f"Skipped {PROJECTS_PATH}. Create it before init-project.")
        else:
            print(f"Projects folder {status}: {PROJECTS_PATH}")

        if ensure_env_file(ENV_PATH, ENV_EXAMPLE_PATH):
            print(f"Created {ENV_PATH}")
        else:
            print(f"Keeping existing {ENV_PATH} (not changed).")
    except SetupError as error:
        print()
        print(f"Setup stopped: {error}")
        return 1

    print("Testing the API connection ...")
    ok, lines = test_api_connection(python, REPOSITORY_ROOT)
    for line in lines:
        print(f"  {line}")
    if not ok:
        print("API connection failed.")
        print_fix_steps(REPOSITORY_ROOT, ENV_PATH)
        return 1

    print("API connection: OK")
    print_next_steps(REPOSITORY_ROOT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
