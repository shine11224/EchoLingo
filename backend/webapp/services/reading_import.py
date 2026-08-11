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
    if pages is None:
        # 高质量档：Docling 版式重排（可选组件，失败/未安装自动回退几何管线）
        from webapp.services.docling_import import extract_text_with_docling

        docling_text = extract_text_with_docling(pdf_path)
        if docling_text and docling_text.strip():
            return docling_text
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
_HYPHEN_BREAK_RE = re.compile(
    r"([A-Za-zÀ-ÖØ-öø-ÿ]{2,})-\s*\n\s*([a-zà-öø-ÿ]{2,})"
)
_GUTTER_CLEAN_RATIO = 0.15   # page votes for the document gutter only below this
_GUTTER_AGREE_PT = 12        # candidates within ±12pt count as the same gutter
_LINE_TOP_TOLERANCE = 2.5    # words within ±2.5pt share a baseline
_LINE_GAP_SPLIT_PT = 8.0     # larger horizontal gap splits a row into two lines
_GUTTER_SPAN_MARGIN = 12.0   # line crossing the gutter by >12pt both sides is full-width


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
    return _dehyphenate("\n\n".join(chunks))


def _extract_pdf_page_text(page, gutter: float | None) -> str:
    """Extract one page in reading order.

    Without a document gutter the page is single-column and pdfplumber's own
    ordering is kept. With a gutter, words are clustered into physical lines
    and re-ordered by vertical region: full-width lines (titles, figures,
    captions) stay in natural position; two-column bands emit the left column
    before the right one.
    """
    if gutter is None:
        return _clean_pdf_text(page.extract_text(x_tolerance=_X_TOLERANCE) or "")
    words = _page_words(page)
    lines = _cluster_words_into_lines(words)
    if not lines:
        return ""
    chunks = _order_lines_into_chunks(lines, gutter)
    return _clean_pdf_text("\n\n".join(chunks))


def _page_words(page) -> list[dict]:
    """Words on a page minus rotated margin strings and text inside images.

    arXiv-style papers stamp a vertical left-margin string (upright=False);
    academic diagrams embed words inside raster/vector image regions. Both
    pollute reading order, so they are dropped. Captions sit outside image
    bboxes and survive. extra_attrs needs a real pdfplumber; test doubles
    fall back to plain extract_words.
    """
    try:
        words = page.extract_words(x_tolerance=_X_TOLERANCE, extra_attrs=["upright"])
    except Exception:
        words = page.extract_words(x_tolerance=_X_TOLERANCE)
    words = [w for w in words if w.get("upright", True)]
    boxes: list[tuple[float, float, float, float]] = []
    for image in getattr(page, "images", None) or []:
        try:
            boxes.append((
                float(image["x0"]), float(image["top"]),
                float(image["x1"]), float(image["bottom"]),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    if not boxes:
        return words
    return [w for w in words if not _word_center_in_boxes(w, boxes)]


def _word_center_in_boxes(word: dict, boxes: list[tuple[float, float, float, float]]) -> bool:
    cx = (float(word["x0"]) + float(word["x1"])) / 2
    cy = (float(word["top"]) + float(word.get("bottom", word["top"]))) / 2
    return any(x0 <= cx <= x1 and top <= cy <= bottom for x0, top, x1, bottom in boxes)


def _cluster_words_into_lines(words: list[dict]) -> list[dict]:
    """Group words into physical lines: same baseline, then split on wide gaps.

    A row holding both columns (same baseline) splits at the gutter gap, so
    each returned line lies fully in one column or genuinely spans the page.
    """
    rows: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (float(w["top"]), float(w["x0"]))):
        if rows and abs(float(word["top"]) - float(rows[-1][0]["top"])) <= _LINE_TOP_TOLERANCE:
            rows[-1].append(word)
        else:
            rows.append([word])
    lines: list[dict] = []
    for row in rows:
        current: list[dict] = []
        current_x1 = 0.0
        for word in sorted(row, key=lambda w: float(w["x0"])):
            if current and float(word["x0"]) - current_x1 > _LINE_GAP_SPLIT_PT:
                lines.append(_build_line(current))
                current = []
            current.append(word)
            current_x1 = float(word["x1"])
        if current:
            lines.append(_build_line(current))
    return lines


def _build_line(words: list[dict]) -> dict:
    return {
        "top": min(float(w["top"]) for w in words),
        "x0": min(float(w["x0"]) for w in words),
        "x1": max(float(w["x1"]) for w in words),
        "text": " ".join(str(w["text"]) for w in words),
    }


def _order_lines_into_chunks(lines: list[dict], gutter: float) -> list[str]:
    """Order lines into text chunks: full-width lines in place, column bands
    left-then-right. Chunk boundaries become blank lines downstream."""
    chunks: list[str] = []
    full_buf: list[str] = []
    left_buf: list[str] = []
    right_buf: list[str] = []

    def flush_columns() -> None:
        if left_buf:
            chunks.append("\n".join(left_buf))
            left_buf.clear()
        if right_buf:
            chunks.append("\n".join(right_buf))
            right_buf.clear()

    def flush_full() -> None:
        if full_buf:
            chunks.append("\n".join(full_buf))
            full_buf.clear()

    for line in sorted(lines, key=lambda l: (l["top"], l["x0"])):
        spans_gutter = (
            line["x0"] < gutter - _GUTTER_SPAN_MARGIN
            and line["x1"] > gutter + _GUTTER_SPAN_MARGIN
        )
        if spans_gutter:
            flush_columns()
            full_buf.append(line["text"])
        else:
            flush_full()
            center = (line["x0"] + line["x1"]) / 2
            (left_buf if center < gutter else right_buf).append(line["text"])
    flush_columns()
    flush_full()
    return chunks


def _clean_pdf_text(text: str) -> str:
    lines = [line for line in text.splitlines() if not _PAGE_NUMBER_RE.match(line.strip())]
    return "\n".join(lines)


def _dehyphenate(text: str) -> str:
    """Join words broken by typesetting hyphens at line breaks.

    With ECDICT available (private/cloud) the merged word must be a real
    dictionary entry. Without it (public repo ships no ECDICT) fall back to
    document-internal attestation: join when the merged word is attested
    elsewhere (reconcil-\ning with "reconciling" in the next paragraph);
    preserve the hyphen only when both fragments are independently attested
    as standalone words (citation-\ndriven); otherwise join. Fragments at
    the break point itself never count as attestation.
    """
    stripped = _HYPHEN_BREAK_RE.sub(" ", text)
    counts: dict[str, int] = {}
    for w in re.findall(r"[A-Za-z]+", stripped):
        counts[w.lower()] = counts.get(w.lower(), 0) + 1
    def independently_attested(word: str) -> bool:
        # ``stripped`` already removes every line-break occurrence, so any
        # remaining count is independent document evidence.
        return counts.get(word, 0) > 0

    def decide(match: re.Match) -> str:
        head, tail = match.group(1), match.group(2)
        merged = head + tail
        try:
            from webapp.services import dicts as dict_service

            if dict_service.lookup_ecdict(merged.lower()):
                return merged
        except Exception:
            pass
        if counts.get(merged.lower(), 0):
            return merged
        if independently_attested(head.lower()) and independently_attested(tail.lower()):
            return f"{head}-{tail}"
        return merged

    return _HYPHEN_BREAK_RE.sub(decide, text)


def extract_text_from_upload(filename: str, content: bytes) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".txt", ".md"}:
        return _decode_text(content)
    if suffix == ".docx":
        return extract_text_from_docx_bytes(content)
    if suffix == ".doc":
        return extract_text_from_doc_bytes(content)
    if suffix == ".pdf":
        return extract_text_from_pdf_bytes(content)
    raise ValueError("Unsupported reading file type. Use txt, doc, docx, or pdf.")


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def extract_text_from_doc_bytes(content: bytes) -> str:
    """老二进制 .doc 文本提取，按可用转换器链式回退：

    Word COM（本机 Windows + Office）→ LibreOffice → catdoc → antiword（云端容器内置）。
    全部不可用时明确报错提示另存 docx，不静默失败。
    """
    errors: list[str] = []
    for extractor in (
        _doc_text_via_word_com,
        _doc_text_via_soffice,
        _doc_text_via_catdoc,
        _doc_text_via_antiword,
    ):
        try:
            text = extractor(content)
        except Exception as exc:
            errors.append(str(exc))
            continue
        if text and text.strip():
            return text
        errors.append(f"{extractor.__name__} 提取结果为空")
    raise ValueError(
        f".doc 解析失败（{'; '.join(errors) or '无可用转换器'}），请将文件另存为 .docx 再导入"
    )


def _doc_tmp_path(content: bytes) -> str:
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as tmp_file:
        tmp_file.write(content)
        return tmp_file.name


def _doc_text_via_word_com(content: bytes) -> str:
    import pythoncom
    import win32com.client
    tmp_path = _doc_tmp_path(content)
    pythoncom.CoInitialize()
    word = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(
            os.path.abspath(tmp_path),
            ConfirmConversions=False, ReadOnly=True,
            AddToRecentFiles=False, Visible=False,
        )
        try:
            return str(doc.Content.Text or "")
        finally:
            doc.Close(False)
    finally:
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()
        os.unlink(tmp_path)


def _doc_text_via_soffice(content: bytes) -> str:
    import subprocess
    import tempfile
    binary = shutil.which("soffice") or shutil.which("libreoffice")
    if not binary:
        raise ValueError("soffice 未安装")
    tmp_path = _doc_tmp_path(content)
    try:
        with tempfile.TemporaryDirectory() as out_dir:
            subprocess.run(
                [binary, "--headless", "--convert-to", "txt:Text",
                 "--outdir", out_dir, tmp_path],
                capture_output=True, timeout=120, check=True,
            )
            txt_path = Path(out_dir) / (Path(tmp_path).stem + ".txt")
            return txt_path.read_text(encoding="utf-8", errors="replace")
    finally:
        os.unlink(tmp_path)


def _doc_text_via_catdoc(content: bytes) -> str:
    import subprocess
    binary = shutil.which("catdoc")
    if not binary:
        raise ValueError("catdoc 未安装")
    tmp_path = _doc_tmp_path(content)
    try:
        proc = subprocess.run(
            [binary, "-d", "utf-8", tmp_path],
            capture_output=True, timeout=60,
        )
        if proc.returncode != 0:
            raise ValueError(f"catdoc 退出码 {proc.returncode}")
        return proc.stdout.decode("utf-8", errors="replace")
    finally:
        os.unlink(tmp_path)


def _doc_text_via_antiword(content: bytes) -> str:
    import subprocess
    binary = shutil.which("antiword")
    if not binary:
        raise ValueError("antiword 未安装")
    tmp_path = _doc_tmp_path(content)
    try:
        proc = subprocess.run(
            [binary, "-m", "UTF-8.txt", tmp_path],
            capture_output=True, timeout=60,
        )
        if proc.returncode != 0:
            raise ValueError(f"antiword 退出码 {proc.returncode}")
        return proc.stdout.decode("utf-8", errors="replace")
    finally:
        os.unlink(tmp_path)


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
    # 高质量档：Docling 版式重排（可选组件，失败/未安装自动回退几何管线）
    from webapp.services.docling_import import extract_text_with_docling_bytes

    docling_text = extract_text_with_docling_bytes(content)
    if docling_text and docling_text.strip():
        return docling_text

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
