"""Reading passage import helpers for v2 reading mode."""
from __future__ import annotations

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

    chunks: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        selected = pages if pages is not None else list(range(len(pdf.pages)))
        for page_index in selected:
            text = pdf.pages[int(page_index)].extract_text() or ""
            if text.strip():
                chunks.append(text)
    return "\n\n".join(chunks)


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

    chunks: list[str] = []
    with pdfplumber.open(BytesIO(content)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if text.strip():
                chunks.append(text)
    return "\n\n".join(chunks)


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
