from pathlib import Path

from pypdf import PdfReader


SUPPORTED_PROJECT_EXTENSIONS = {".pdf", ".md", ".txt"}


def extract_pdf(path):
    """Extract text from a PDF while preserving page boundaries."""
    reader = PdfReader(path)
    pages = []
    for page_number, page in enumerate(reader.pages, start=1):
        pages.append({
            "page": page_number,
            "text": (page.extract_text() or "").strip(),
        })

    if not any(page["text"] for page in pages):
        raise RuntimeError(
            f"No extractable text was found in PDF: {path}. "
            "The file may be scanned and require OCR."
        )
    return pages


def discover_project_sources(project_path, excluded_paths=()):
    """Collect supported original instructor materials from project/."""
    project_path = Path(project_path).resolve()
    source_folder = project_path / "project"
    excluded = {Path(path).resolve() for path in excluded_paths}

    if not source_folder.is_dir():
        raise FileNotFoundError(
            f"Project materials folder does not exist: {source_folder}"
        )

    sources = []
    for path in sorted(source_folder.rglob("*")):
        if not path.is_file() or path.resolve() in excluded:
            continue
        if path.suffix.lower() not in SUPPORTED_PROJECT_EXTENSIONS:
            continue

        relative_path = path.relative_to(project_path).as_posix()
        if path.suffix.lower() == ".pdf":
            sources.append({
                "path": relative_path,
                "source_type": "pdf",
                "pages": extract_pdf(path),
            })
        else:
            content = path.read_text(
                encoding="utf-8", errors="replace"
            ).strip()
            if content:
                sources.append({
                    "path": relative_path,
                    "source_type": "text",
                    "content": content,
                })

    if not sources:
        raise RuntimeError(
            "No readable PDF, Markdown, or plain-text project materials "
            f"were found in {source_folder}."
        )
    return sources
