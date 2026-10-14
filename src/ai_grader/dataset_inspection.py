import csv
from pathlib import Path


DEFAULT_FULL_CSV_MAX_BYTES = 200_000
DEFAULT_SAMPLE_ROWS = 20


def summarize_csv(path, project_path, full_csv_max_bytes=DEFAULT_FULL_CSV_MAX_BYTES):
    """Return full small-CSV content or a clearly limited large-CSV summary."""
    relative_path = path.relative_to(project_path).as_posix()
    size_bytes = path.stat().st_size

    if size_bytes <= full_csv_max_bytes:
        return {
            "path": relative_path,
            "mode": "full",
            "size_bytes": size_bytes,
            "content": path.read_text(encoding="utf-8", errors="replace"),
            "limitation": None,
        }

    with path.open("r", encoding="utf-8", errors="replace", newline="") as file:
        reader = csv.reader(file)
        rows = []
        row_count = 0
        for row in reader:
            row_count += 1
            if len(rows) < DEFAULT_SAMPLE_ROWS + 1:
                rows.append(row)

    sample = "\n".join(",".join(row) for row in rows)
    limitation = (
        f"Dataset exceeds the {full_csv_max_bytes}-byte full-content limit. "
        f"Only the header and first {DEFAULT_SAMPLE_ROWS} data rows are included."
    )
    return {
        "path": relative_path,
        "mode": "sampled",
        "size_bytes": size_bytes,
        "row_count_including_header": row_count,
        "content": sample,
        "limitation": limitation,
    }


def collect_csv_datasets(project_path, full_csv_max_bytes=DEFAULT_FULL_CSV_MAX_BYTES):
    """Collect CSV datasets from datasets/ without touching submissions/."""
    project_path = Path(project_path).resolve()
    datasets_path = project_path / "datasets"
    if not datasets_path.is_dir():
        return []
    return [
        summarize_csv(path, project_path, full_csv_max_bytes)
        for path in sorted(datasets_path.rglob("*"))
        if path.is_file() and path.suffix.lower() == ".csv"
    ]
