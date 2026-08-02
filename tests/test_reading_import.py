import sys
import os
import zipfile
from io import BytesIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


def test_build_reading_blocks_from_text_splits_paragraphs():
    from webapp.services.reading_import import build_reading_blocks_from_text

    raw = "A first paragraph about migration.\n\nA second paragraph about climate change."

    result = build_reading_blocks_from_text(raw, title="Test Passage")

    assert result["title"] == "Test Passage"
    assert result["blocks"] == [
        {"index": 1, "text": "A first paragraph about migration."},
        {"index": 2, "text": "A second paragraph about climate change."},
    ]


def test_build_reading_blocks_from_text_removes_empty_lines_and_page_noise():
    from webapp.services.reading_import import build_reading_blocks_from_text

    raw = "Reading Passage 1\n\n\nA useful paragraph.\n\nQuestions 1-13\n\nAnother useful paragraph."

    result = build_reading_blocks_from_text(raw, title="Reading Passage 1")

    assert [b["text"] for b in result["blocks"]] == [
        "A useful paragraph.",
        "Another useful paragraph.",
    ]


def test_extract_text_from_txt_upload_bytes():
    from webapp.services.reading_import import extract_text_from_upload

    result = extract_text_from_upload("passage.txt", "A txt paragraph.".encode("utf-8"))

    assert result == "A txt paragraph."


def test_extract_text_from_docx_upload_bytes():
    from webapp.services.reading_import import extract_text_from_upload

    buffer = BytesIO()
    document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>A docx paragraph.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Another paragraph.</w:t></w:r></w:p>
  </w:body>
</w:document>"""
    with zipfile.ZipFile(buffer, "w") as docx:
        docx.writestr("word/document.xml", document_xml)

    result = extract_text_from_upload("passage.docx", buffer.getvalue())

    assert result == "A docx paragraph.\n\nAnother paragraph."


def test_extract_text_from_pdf_bytes_uses_ocr_when_text_layer_is_empty(monkeypatch):
    import webapp.services.reading_import as reading_import

    monkeypatch.setattr(reading_import, "_extract_pdf_text_layer_from_bytes", lambda content: "")
    monkeypatch.setattr(reading_import, "_ocr_pdf_bytes", lambda content, pages=None: "OCR paragraph.")

    result = reading_import.extract_text_from_pdf_bytes(b"%PDF scanned")

    assert result == "OCR paragraph."


def test_extract_text_from_pdf_bytes_reports_missing_ocr_dependency(monkeypatch):
    import pytest
    import webapp.services.reading_import as reading_import

    def missing_ocr(content, pages=None):
        raise ImportError("pytesseract")

    monkeypatch.setattr(reading_import, "_extract_pdf_text_layer_from_bytes", lambda content: "")
    monkeypatch.setattr(reading_import, "_ocr_pdf_bytes", missing_ocr)

    with pytest.raises(ValueError, match="当前环境缺少 pytesseract 或 Tesseract"):
        reading_import.extract_text_from_pdf_bytes(b"%PDF scanned")


def test_extract_text_from_pdf_bytes_reports_missing_tesseract_binary(monkeypatch):
    import pytest
    import webapp.services.reading_import as reading_import

    def missing_tesseract(content, pages=None):
        raise RuntimeError("Tesseract binary not found")

    monkeypatch.setattr(reading_import, "_extract_pdf_text_layer_from_bytes", lambda content: "")
    monkeypatch.setattr(reading_import, "_ocr_pdf_bytes", missing_tesseract)

    with pytest.raises(ValueError, match="当前环境缺少 pytesseract 或 Tesseract"):
        reading_import.extract_text_from_pdf_bytes(b"%PDF scanned")
