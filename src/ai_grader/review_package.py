"""
The rubric review package: everything Claude needs to check a rubric and its
grading specification, and nothing about students.

Contents: the assignment materials (project\\), the rubric drafts and course
policy (rubric\\), the reference solution (reference\\), the datasets
(datasets\\) and the latest grading specification, plus a README. Never
submissions\\, the student map, the manifest, anonymized copies, runs or
review files.
"""

import re
import zipfile
from pathlib import Path


PACKAGE_PATTERN = re.compile(r"review_package_v([0-9]{3})\.zip")
FOLDERS = ("project", "rubric", "reference", "datasets")
README = """AI Grader rubric review package
================================

Project: {project}
Grading specification: {spec}

This package holds the assignment materials, the rubric drafts, the course
grading policy, the reference solution, the datasets and the draft grading
specification. It contains no student work, names or grades.

Please check, against the assignment:
- point totals per task and in all;
- that every rubric item and the course policy are reflected in the
  specification's criteria and deduction rules;
- the reference values (recompute them from the datasets where possible);
- rules that would deduct for something AI Grader cannot inspect.

Return corrected files with the same names: Markdown drafts go back into
project\\, rubric\\ or reference\\; a corrected specification is saved as
grader\\grading_spec_reviewed.json. Then run:  python -m ai_grader continue {project} --approve
"""


def write_review_package(project_path, spec_path):
    """Write grader\\review_package_v###.zip; returns its path."""
    project_path = Path(project_path)
    grader = project_path / "grader"
    versions = [int(match.group(1)) for path in grader.glob("review_package_v*.zip")
                if (match := PACKAGE_PATTERN.fullmatch(path.name))]
    output = grader / f"review_package_v{max(versions, default=0) + 1:03d}.zip"
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("README.txt", README.format(project=project_path.name,
                                                     spec=Path(spec_path).name))
        for folder in FOLDERS:
            root = project_path / folder
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    package.write(path, path.relative_to(project_path).as_posix())
        package.write(spec_path, f"grader/{Path(spec_path).name}")
    return output
