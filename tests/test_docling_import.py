import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from webapp.services import docling_import, reading_import


def test_items_to_paragraphs_keeps_body_text_only():
    items = [
        {"label": "section_header", "text": "1 Introduction"},
        {"label": "text", "text": "  Body   paragraph\n with spacing. "},
        {"label": "list_item", "text": "First point."},
        {"label": "table", "text": "cell content"},
        {"label": "formula", "text": "E = mc^2"},
        {"label": "caption", "text": "Figure 1: overview."},
        {"label": "picture", "text": ""},
        {"label": "page_header", "text": "Journal Name"},
        {"label": "text", "text": "   "},
    ]

    assert docling_import.items_to_paragraphs(items) == [
        "1 Introduction",
        "Body paragraph with spacing.",
        "First point.",
    ]


def test_docling_disabled_via_env(monkeypatch):
    monkeypatch.setenv("ELT_DOCLING", "off")
    monkeypatch.setattr(docling_import, "_probe_cache", None)
    assert docling_import.docling_available() is False
    assert docling_import.extract_text_with_docling_bytes(b"%PDF fake") is None


def test_extract_text_from_pdf_falls_back_when_docling_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(docling_import, "extract_text_with_docling", lambda path: None)
    monkeypatch.setattr(
        reading_import, "_extract_pdf_text_layer_from_path", lambda path, pages=None: "geometric text"
    )

    assert reading_import.extract_text_from_pdf(tmp_path / "fake.pdf") == "geometric text"


def test_extract_text_from_pdf_prefers_docling_when_available(tmp_path, monkeypatch):
    monkeypatch.setattr(
        docling_import, "extract_text_with_docling", lambda path: "docling\n\nreflowed"
    )

    assert reading_import.extract_text_from_pdf(tmp_path / "fake.pdf") == "docling\n\nreflowed"


@pytest.mark.skipif(
    os.environ.get("ELT_DOCLING_E2E") != "1",
    reason="真实 Docling 转换约 40s+，仅 ELT_DOCLING_E2E=1 时运行",
)
def test_docling_end_to_end_on_arxiv_pdf(monkeypatch):
    monkeypatch.setenv("ELT_DOCLING", "on")
    monkeypatch.setattr(docling_import, "_probe_cache", None)
    pytest.importorskip("docling")
    pdf = os.environ.get("ELT_DOCLING_PDF", r"C:\Users\may\Downloads\2511.14362v1.pdf")
    if not os.path.exists(pdf):
        pytest.skip("sample PDF not present")

    text = reading_import.extract_text_from_pdf(pdf)

    assert "The accelerating growth of scientific publications" in text
    assert "publi-" not in text
    assert "Figure 1: An overview of SciRAG framework." not in text  # caption 整块跳过
