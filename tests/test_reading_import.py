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


def _word(text, x0, x1, top):
    return {"text": text, "x0": float(x0), "x1": float(x1), "top": float(top)}


class _FakePdfPage:
    """Minimal pdfplumber stand-in for the word-level two-column path."""

    def __init__(self, words, text="", images=None):
        self._words = words
        self._text = text
        self.images = images or []

    def extract_words(self, x_tolerance=1.5, extra_attrs=None):
        return list(self._words)

    def extract_text(self, x_tolerance=1.5):
        return self._text


def test_pdf_page_full_width_title_comes_before_two_columns():
    from webapp.services.reading_import import _extract_pdf_page_text

    words = [
        # full-width title crossing the gutter (x0=60 < 288, x1=340 > 312)
        _word("Migration", 60, 130, 50),
        _word("and", 132, 155, 50),
        _word("Climate", 157, 220, 50),
        _word("Policy", 222, 280, 50),
        _word("Review", 282, 340, 50),
        # same-baseline body row split by the gutter gap
        _word("Left", 60, 95, 100),
        _word("column", 97, 145, 100),
        _word("text", 147, 175, 100),
        _word("Right", 340, 380, 100),
        _word("column", 382, 432, 100),
        _word("text", 434, 462, 100),
    ]

    result = _extract_pdf_page_text(_FakePdfPage(words), gutter=300.0)

    assert result == (
        "Migration and Climate Policy Review\n\n"
        "Left column text\n\n"
        "Right column text"
    )


def test_pdf_page_orders_left_column_before_right_column():
    from webapp.services.reading_import import _extract_pdf_page_text

    words = [
        _word("First", 60, 100, 100),
        _word("left", 102, 130, 100),
        _word("line.", 132, 165, 100),
        _word("First", 340, 380, 100),
        _word("right", 382, 415, 100),
        _word("line.", 417, 450, 100),
        _word("Second", 60, 110, 120),
        _word("left", 112, 140, 120),
        _word("line.", 142, 175, 120),
        _word("Second", 340, 390, 120),
        _word("right", 392, 425, 120),
        _word("line.", 427, 460, 120),
    ]

    result = _extract_pdf_page_text(_FakePdfPage(words), gutter=300.0)

    assert result == (
        "First left line.\nSecond left line.\n\n"
        "First right line.\nSecond right line."
    )


def test_pdf_page_full_width_caption_stays_between_column_regions():
    from webapp.services.reading_import import _extract_pdf_page_text

    words = [
        _word("Alpha", 60, 100, 100),
        _word("left", 102, 130, 100),
        _word("body", 132, 165, 100),
        _word("Alpha", 340, 380, 100),
        _word("right", 382, 415, 100),
        _word("body", 417, 450, 100),
        # full-width caption crossing the gutter
        _word("Figure", 60, 100, 180),
        _word("1:", 102, 112, 180),
        _word("Caption", 114, 170, 180),
        _word("spanning", 172, 240, 180),
        _word("the", 242, 258, 180),
        _word("page", 260, 320, 180),
        _word("Beta", 60, 95, 240),
        _word("left", 97, 125, 240),
        _word("body", 127, 160, 240),
        _word("Beta", 340, 375, 240),
        _word("right", 377, 410, 240),
        _word("body", 412, 445, 240),
    ]

    result = _extract_pdf_page_text(_FakePdfPage(words), gutter=300.0)

    assert result == (
        "Alpha left body\n\n"
        "Alpha right body\n\n"
        "Figure 1: Caption spanning the page\n\n"
        "Beta left body\n\n"
        "Beta right body"
    )


def test_pdf_page_without_gutter_keeps_single_column_text_order():
    from webapp.services.reading_import import _extract_pdf_page_text

    page = _FakePdfPage([], text="Intro line one\nIntro line two\n7\nIntro line three")

    result = _extract_pdf_page_text(page, gutter=None)

    assert result == "Intro line one\nIntro line two\nIntro line three"


def test_dehyphenate_joins_typesetting_line_break():
    from webapp.services.reading_import import _dehyphenate

    result = _dehyphenate("The publi-\ncations office released data.")

    assert result == "The publications office released data."


def test_dehyphenate_keeps_genuine_hyphenated_compound():
    from webapp.services.reading_import import _dehyphenate

    raw = (
        "A citation-\ndriven approach. The citation index ranks papers. "
        "Driven teams publish more."
    )

    result = _dehyphenate(raw)

    assert "citation-driven approach" in result


def test_dehyphenate_joins_latin_diacritic_name_at_line_break():
    from webapp.services.reading_import import _dehyphenate

    result = _dehyphenate("Tim Rock-\ntäschel et al.")

    assert result == "Tim Rocktäschel et al."


def test_pdf_page_excludes_rotated_margin_words():
    from webapp.services.reading_import import _extract_pdf_page_text

    words = [
        # rotated arXiv margin string at the left edge (upright=False)
        {**_word("arXiv:2511.14362v1", 16, 36, 100), "upright": False},
        {**_word("[cs.CL]", 16, 36, 300), "upright": False},
        _word("Body", 60, 100, 100),
        _word("left", 102, 130, 100),
        _word("Body", 340, 380, 100),
        _word("right", 382, 415, 100),
    ]

    result = _extract_pdf_page_text(_FakePdfPage(words), gutter=300.0)

    assert result == "Body left\n\nBody right"


def test_pdf_page_excludes_image_region_words_but_keeps_caption():
    from webapp.services.reading_import import _extract_pdf_page_text

    images = [{"x0": 306.142, "x1": 524.408, "top": 219.907, "bottom": 318.736}]
    words = [
        _word("Body", 60, 100, 100),
        _word("text", 102, 140, 100),
        # diagram words whose centers fall inside the image bbox
        _word("Encoder", 320, 370, 240),
        _word("Decoder", 400, 450, 280),
        # caption below the image (top=329.3 > bottom=318.736) must survive
        _word("Figure", 306, 350, 329.3),
        _word("1:", 352, 364, 329.3),
        _word("Architecture", 366, 440, 329.3),
    ]

    result = _extract_pdf_page_text(_FakePdfPage(words, images=images), gutter=300.0)

    assert result == "Body text\n\nFigure 1: Architecture"


def test_dehyphenate_joins_when_merged_word_attested_elsewhere():
    from webapp.services.reading_import import _dehyphenate

    # Head fragment is attested on its own; only the merged word elsewhere
    # proves the break is typesetting, not a compound.
    raw = (
        "The flummox-\nified parser failed. A flummox alone is harmless. "
        "Once flummoxified, nothing recovers."
    )

    result = _dehyphenate(raw)

    assert "flummoxified parser" in result
    assert "flummox-ified" not in result
