"""Reading passage import helpers for v2 reading mode."""
from __future__ import annotations

import re
from pathlib import Path
from typing import TypedDict
from io import BytesIO
import os
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile


class ReadingBlock(TypedDict):
    index: int
    text: str


class ReadingImportResult(TypedDict):
    title: str
    blocks: list[ReadingBlock]


NOISE_PREFIXES = (
    "reading passage",
    "questions ",
    "do the following statements",
    "choose the correct letter",
    "write the correct letter",
    "complete the notes",
    "complete the summary",
)

OCR_REQUIREMENT_MESSAGE = (
    "PDF 没有可读取的文本层，需要 OCR；当前环境缺少 pytesseract 或 Tesseract 程序。"
    "请安装 Tesseract 后重试，或上传带文本层的 PDF / txt / docx。"
)


def _is_noise_line(line: str) -> bool:
    normalized = line.strip().lower()
    return any(normalized.startswith(prefix) for prefix in NOISE_PREFIXES)


def build_reading_blocks_from_text(raw_text: str, title: str = "Reading Passage") -> ReadingImportResult:
    blocks: list[ReadingBlock] = []
    current: list[str] = []

    for raw_line in raw_text.splitlines():
        line = " ".join(raw_line.strip().split())
        if not line:
            if current:
                blocks.append({"index": len(blocks) + 1, "text": " ".join(current)})
                current = []
            continue
        if _is_noise_line(line):
            continue
        current.append(line)

    if current:
        blocks.append({"index": len(blocks) + 1, "text": " ".join(current)})

    return {"title": title or "Reading Passage", "blocks": blocks}


def extract_text_from_pdf(path: str | Path, pages: list[int] | None = None) -> str:
    pdf_path = Path(path)
    try:
        text = _extract_pdf_text_layer_from_path(pdf_path, pages=pages)
    except Exception as exc:
        text = ""
        text_error: Exception | None = exc
    else:
        text_error = None

    if text.strip():
        return text

    return _extract_pdf_with_ocr_fallback(
        lambda: _ocr_pdf_bytes(pdf_path.read_bytes(), pages=pages),
        text_error=text_error,
    )


def _extract_pdf_text_layer_from_path(path: str | Path, pages: list[int] | None = None) -> str:
    import pdfplumber

    with pdfplumber.open(str(path)) as pdf:
        selected = pages if pages is not None else list(range(len(pdf.pages)))
        return _extract_pdf_pages([pdf.pages[int(i)] for i in selected])


# ── Two-column (academic paper) aware extraction ─────────────────────

_X_TOLERANCE = 1.5  # >2 starts gluing tightly-kerned academic fonts into fake words
_PAGE_NUMBER_RE = re.compile(r"^\d{1,4}$")
_HYPHEN_BREAK_RE = re.compile(r"([A-Za-z]{2,})-\s*\n\s*([a-z]{2,})")
_GUTTER_CLEAN_RATIO = 0.15   # page votes for the document gutter only below this
_GUTTER_APPLY_RATIO = 0.35   # above this a page is treated as full-width (figure/table)
_GUTTER_AGREE_PT = 12        # candidates within ±12pt count as the same gutter


def _join_hyphen_break(match: re.Match) -> str:
    """Dehyphenate LaTeX line breaks: join only when the merged word is a real
    dictionary word (ECDICT); otherwise keep the hyphen (citation-\ndriven)."""
    merged = match.group(1) + match.group(2)
    try:
        from webapp.services import dicts as dict_service

        if dict_service.lookup_ecdict(merged.lower()):
            return merged
    except Exception:
        pass
    return f"{match.group(1)}-{match.group(2)}"


def _page_gutter_candidate(page) -> tuple[float, float] | None:
    """Argmin of smoothed char coverage in the central band; returns (x, ratio).

    ratio = coverage at gutter / page peak. Two-column body pages score ~0.02;
    pages whose center is crossed by figures/titles score higher but their argmin
    still lands on the gutter — the document-level vote filters outliers.
    """
    width = float(page.width)
    height = float(page.height)
    hist = [0.0] * (int(width) + 2)
    total = 0
    for ch in page.chars:
        if not str(ch.get("text") or "").strip():
            continue
        if float(ch["top"]) < height * 0.25:
            continue
        x0 = max(0, int(float(ch["x0"])))
        x1 = min(int(width) + 1, int(float(ch["x1"])) + 1)
        for x in range(x0, x1):
            hist[x] += 1
        total += 1
    if total < 100:
        return None
    peak = max(hist) or 1
    best_x = None
    best_v = None
    for x in range(int(width * 0.35), int(width * 0.65)):
        v = sum(hist[max(0, x - 3):x + 4]) / 7
        if best_v is None or v < best_v:
            best_v, best_x = v, x
    if best_x is None:
        return None
    return (float(best_x), best_v / peak)


def _document_gutter(pages) -> float | None:
    """Median of per-page clean gutter candidates; needs ≥3 pages or ≥30% agreeing."""
    votes = []
    for page in pages:
        candidate = _page_gutter_candidate(page)
        if candidate and candidate[1] < _GUTTER_CLEAN_RATIO:
            votes.append(candidate[0])
    if not votes:
        return None
    votes.sort()
    median = votes[len(votes) // 2]
    agreeing = [v for v in votes if abs(v - median) <= _GUTTER_AGREE_PT]
    if len(agreeing) >= max(3, int(len(pages) * 0.3)):
        return sum(agreeing) / len(agreeing)
    return None


def _extract_pdf_pages(pages) -> str:
    gutter = _document_gutter(pages)
    chunks = []
    for page in pages:
        text = _extract_pdf_page_text(page, gutter)
        if text.strip():
            chunks.append(text)
    return "\n\n".join(chunks)


def _extract_pdf_page_text(page, gutter: float | None) -> str:
    """Extract one page, splitting into left/right columns when a gutter applies."""
    use_gutter = gutter
    if use_gutter is not None:
        candidate = _page_gutter_candidate(page)
        # page crossed by a full-width figure/table → fall back to single column
        if candidate is None or candidate[1] > _GUTTER_APPLY_RATIO:
            use_gutter = None
    if use_gutter is None:
        return _clean_pdf_text(page.extract_text(x_tolerance=_X_TOLERANCE) or "")
    left = page.filter(lambda obj: (float(obj["x0"]) + float(obj["x1"])) / 2 < use_gutter)
    right = page.filter(lambda obj: (float(obj["x0"]) + float(obj["x1"])) / 2 >= use_gutter)
    left_text = left.extract_text(x_tolerance=_X_TOLERANCE) or ""
    right_text = right.extract_text(x_tolerance=_X_TOLERANCE) or ""
    return _clean_pdf_text(left_text + "\n\n" + right_text)


def _clean_pdf_text(text: str) -> str:
    text = _HYPHEN_BREAK_RE.sub(_join_hyphen_break, text)
    lines = [line for line in text.splitlines() if not _PAGE_NUMBER_RE.match(line.strip())]
    return "\n".join(lines)


def extract_text_from_upload(filename: str, content: bytes) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".txt", ".md"}:
        return _decode_text(content)
    if suffix == ".docx":
        return extract_text_from_docx_bytes(content)
    if suffix == ".pdf":
        return extract_text_from_pdf_bytes(content)
    raise ValueError("Unsupported reading file type. Use txt, docx, or pdf.")


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def extract_text_from_docx_bytes(content: bytes) -> str:
    with zipfile.ZipFile(BytesIO(content)) as docx:
        xml = docx.read("word/document.xml")
    root = ET.fromstring(xml)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", ns):
        texts = [node.text or "" for node in paragraph.findall(".//w:t", ns)]
        line = "".join(texts).strip()
        if line:
            paragraphs.append(line)
    return "\n\n".join(paragraphs)


def extract_text_from_pdf_bytes(content: bytes) -> str:
    try:
        text = _extract_pdf_text_layer_from_bytes(content)
    except Exception as exc:
        text = ""
        text_error: Exception | None = exc
    else:
        text_error = None

    if text.strip():
        return text

    return _extract_pdf_with_ocr_fallback(
        lambda: _ocr_pdf_bytes(content),
        text_error=text_error,
    )


def _extract_pdf_text_layer_from_bytes(content: bytes) -> str:
    import pdfplumber

    with pdfplumber.open(BytesIO(content)) as pdf:
        return _extract_pdf_pages(list(pdf.pages))


def _extract_pdf_with_ocr_fallback(ocr_func, *, text_error: Exception | None = None) -> str:
    try:
        text = ocr_func()
    except ImportError as exc:
        raise ValueError(OCR_REQUIREMENT_MESSAGE) from exc
    except Exception as exc:
        if _is_missing_ocr_runtime(exc):
            raise ValueError(OCR_REQUIREMENT_MESSAGE) from exc
        reason = "PDF 文本层提取失败" if text_error else "PDF 没有可读取的文本层"
        raise ValueError(f"{reason}；OCR 识别失败：{exc}") from exc

    if text.strip():
        return text
    raise ValueError("PDF 没有可读取的文本层，OCR 也没有识别出文字。请换成更清晰的 PDF，或上传 txt / docx。")


def _is_missing_ocr_runtime(exc: Exception) -> bool:
    name = exc.__class__.__name__.lower()
    message = str(exc).lower()
    return (
        "tesseractnotfound" in name
        or "tesseract binary not found" in message
        or "tesseract is not installed" in message
        or "no such file or directory" in message and "tesseract" in message
    )


def _find_tesseract_binary() -> str | None:
    from_path = shutil.which("tesseract")
    if from_path:
        return from_path

    conda_tesseract = Path(sys.prefix) / "Library" / "bin" / "tesseract.exe"
    if conda_tesseract.exists():
        return str(conda_tesseract)

    return None


def _find_tessdata_prefix() -> str | None:
    for candidate in (
        Path(sys.prefix) / "share" / "tessdata",
        Path(sys.prefix) / "Library" / "share" / "tessdata",
    ):
        if (candidate / "eng.traineddata").exists():
            return str(candidate)
    return None


def _ocr_pdf_bytes(content: bytes, pages: list[int] | None = None) -> str:
    import pypdfium2 as pdfium
    import pytesseract

    tesseract_binary = _find_tesseract_binary()
    if not tesseract_binary:
        raise RuntimeError("Tesseract binary not found")
    pytesseract.pytesseract.tesseract_cmd = tesseract_binary
    tessdata_prefix = _find_tessdata_prefix()
    if tessdata_prefix:
        os.environ.setdefault("TESSDATA_PREFIX", tessdata_prefix)

    pdf = pdfium.PdfDocument(content)
    chunks: list[str] = []
    selected = pages if pages is not None else list(range(len(pdf)))
    for page_index in selected:
        page = pdf[int(page_index)]
        try:
            bitmap = page.render(scale=2)
            image = bitmap.to_pil()
            text = pytesseract.image_to_string(image, lang="eng") or ""
        finally:
            close = getattr(page, "close", None)
            if callable(close):
                close()
        if text.strip():
            chunks.append(text.strip())
    close_pdf = getattr(pdf, "close", None)
    if callable(close_pdf):
        close_pdf()
    return "\n\n".join(chunks)
