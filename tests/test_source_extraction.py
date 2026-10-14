import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ai_grader.source_extraction import (
    discover_project_sources,
    extract_pdf,
)


class SourceExtractionTests(unittest.TestCase):
    @patch("ai_grader.source_extraction.PdfReader")
    def test_pdf_extraction_preserves_page_numbers(self, reader_class):
        page_one = MagicMock()
        page_one.extract_text.return_value = "First page"
        page_two = MagicMock()
        page_two.extract_text.return_value = "Second page"
        reader_class.return_value.pages = [page_one, page_two]

        pages = extract_pdf(Path("assignment.pdf"))

        self.assertEqual(pages, [
            {"page": 1, "text": "First page"},
            {"page": 2, "text": "Second page"},
        ])

    @patch("ai_grader.source_extraction.PdfReader")
    def test_image_only_pdf_fails_clearly(self, reader_class):
        page = MagicMock()
        page.extract_text.return_value = ""
        reader_class.return_value.pages = [page]

        with self.assertRaisesRegex(RuntimeError, "require OCR"):
            extract_pdf(Path("scanned.pdf"))

    def test_generated_instructions_can_be_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            original = project / "original.md"
            generated = project / "project_instructions.md"
            original.write_text("Original", encoding="utf-8")
            generated.write_text("Generated", encoding="utf-8")

            sources = discover_project_sources(root, [generated])

            self.assertEqual([source["path"] for source in sources], [
                "project/original.md"
            ])


if __name__ == "__main__":
    unittest.main()
