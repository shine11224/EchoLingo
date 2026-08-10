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


def test_items_to_paragraphs_drops_letter_spaced_figure_text():
    items = [
        {"label": "text", "text": "R e fi n e m e n t"},
        {"label": "text", "text": "I n i t i a l A n s w e r G e n e r a t i o n"},
        {"label": "text", "text": "I am a normal sentence with single letters."},
    ]
    paragraphs = docling_import.items_to_paragraphs(items)
    # letter-spaced 图内标签整块剔除；正常句子单字母占比低不受影响
    assert paragraphs == ["I am a normal sentence with single letters."]


def test_items_to_paragraphs_drops_figure_noise_and_keeps_structural_words():
    items = [
        {"label": "text", "text": "8"},
        {"label": "text", "text": "O~O"},
        {"label": "text", "text": "& Rerank"},
        {"label": "text", "text": "Abstract"},
        {"label": "section_header", "text": "Abstract"},
        {"label": "text", "text": "Real body paragraph stays."},
    ]
    assert docling_import.items_to_paragraphs(items) == [
        "Abstract",
        "Abstract",
        "Real body paragraph stays.",
    ]


def test_items_to_paragraphs_strips_trailing_citation_noise():
    items = [
        {"label": "text", "text": "Formatting requirements . . . [0]"},
        {"label": "text", "text": "As shown in prior work [12]."},
        {"label": "text", "text": "Trailing bracket only [3]"},
    ]
    paragraphs = docling_import.items_to_paragraphs(items)
    assert paragraphs[0] == "Formatting requirements"
    assert paragraphs[1] == "As shown in prior work [12]."
    assert paragraphs[2] == "Trailing bracket only"


def test_items_to_paragraphs_merges_lowercase_continuation():
    items = [
        {"label": "text", "text": "The controller compares chains by"},
        {"label": "text", "text": "Figure garbage"},  # 12 字符以上不会被噪声剔除，但小写合并仍针对下一段
        {"label": "text", "text": "similarity or centrality scores, then decides."},
        {"label": "section_header", "text": "3.3 Adaptive Retrieval"},
    ]
    paragraphs = docling_import.items_to_paragraphs(items)
    # "similarity..." 并回前一段（此处前一段是图文字残留，真实场景图文字已被 label 过滤）
    assert paragraphs[0] == "The controller compares chains by"
    assert paragraphs[1] == "Figure garbage similarity or centrality scores, then decides."
    assert paragraphs[2] == "3.3 Adaptive Retrieval"


def test_split_joined_words_uses_dictionary(monkeypatch):
    known = {"retrieval", "augmented", "interpretability", "graph", "and"}
    monkeypatch.setattr(docling_import, "_word_in_dict", lambda w: w in known)
    assert docling_import._split_joined_words("retrievalaugmented generation") == "retrieval-augmented generation"
    # 词典里已有的词不拆
    assert docling_import._split_joined_words("interpretability") == "interpretability"
    # 拆不出的保留原样
    assert docling_import._split_joined_words("xyzzyplugh") == "xyzzyplugh"


def test_split_joined_words_skips_short_tokens(monkeypatch):
    monkeypatch.setattr(docling_import, "_word_in_dict", lambda w: False)
    assert docling_import._split_joined_words("graphand") == "graphand"  # <10 字符不处理


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
