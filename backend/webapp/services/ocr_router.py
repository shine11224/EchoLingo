"""Automatic OCR backend routing for scanned Reading PDFs.

The router deliberately keeps imports lazy: Tesseract, EasyOCR, NumPy and
PyTorch are only loaded when an image page actually needs OCR. This keeps
normal text-layer PDFs lightweight and allows the optional EasyOCR backend to
be absent without breaking PDF import.
"""
from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import os
from pathlib import Path
import shutil
import sys
from typing import Any


DEFAULT_CONFIDENCE_THRESHOLD = 65.0
_easyocr_reader: Any | None = None
_REPO_ROOT = Path(__file__).resolve().parents[3]
_BUNDLED_TESSERACT_DIR = _REPO_ROOT / "tools" / "tesseract"


@dataclass(frozen=True)
class OCRResult:
    text: str
    confidence: float | None
    engine: str
    reason: str = ""


def _configured_engine() -> str:
    value = os.environ.get("ELT_OCR_ENGINE", "auto").strip().lower()
    return value if value in {"auto", "tesseract", "easyocr"} else "auto"


def _confidence_threshold() -> float:
    raw = os.environ.get("ELT_OCR_CONFIDENCE_THRESHOLD", "")
    try:
        return max(0.0, min(100.0, float(raw))) if raw else DEFAULT_CONFIDENCE_THRESHOLD
    except ValueError:
        return DEFAULT_CONFIDENCE_THRESHOLD


def _easyocr_available() -> bool:
    """Return whether the optional EasyOCR package is importable.

    This is intentionally a package probe rather than a model probe. Model
    downloads happen only if the router actually chooses EasyOCR.
    """
    return importlib.util.find_spec("easyocr") is not None


def _find_tesseract_binary() -> str | None:
    from_path = shutil.which("tesseract")
    if from_path:
        return from_path

    candidates = (
        _BUNDLED_TESSERACT_DIR / "tesseract.exe",
        Path(sys.prefix) / "Library" / "bin" / "tesseract.exe",
        Path(sys.prefix) / "bin" / "tesseract",
        Path(sys.prefix) / "bin" / "tesseract.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR" / "tesseract.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Tesseract-OCR" / "tesseract.exe",
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _find_tessdata_prefix() -> str | None:
    configured = os.environ.get("TESSDATA_PREFIX", "").strip()
    candidates = (
        Path(configured) if configured else None,
        _BUNDLED_TESSERACT_DIR / "tessdata",
        Path(sys.prefix) / "share" / "tessdata",
        Path(sys.prefix) / "Library" / "share" / "tessdata",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR" / "tessdata",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Tesseract-OCR" / "tessdata",
    )
    for candidate in candidates:
        if candidate is None:
            continue
        if (candidate / "eng.traineddata").exists():
            return str(candidate)
    return None


def _run_tesseract(image: Any) -> OCRResult:
    import pytesseract

    tesseract_binary = _find_tesseract_binary()
    if not tesseract_binary:
        raise RuntimeError("Tesseract binary not found")
    pytesseract.pytesseract.tesseract_cmd = tesseract_binary
    tessdata_prefix = _find_tessdata_prefix()
    if tessdata_prefix:
        os.environ.setdefault("TESSDATA_PREFIX", tessdata_prefix)

    language = os.environ.get("ELT_OCR_LANG", "eng").strip() or "eng"
    data = pytesseract.image_to_data(
        image,
        lang=language,
        output_type=pytesseract.Output.DICT,
    )
    lines: dict[tuple[int, int, int], list[str]] = {}
    confidences: list[float] = []
    texts = data.get("text", [])
    for index, raw_text in enumerate(texts):
        text = str(raw_text or "").strip()
        if not text:
            continue
        key = (
            int(data.get("block_num", [0] * len(texts))[index]),
            int(data.get("par_num", [0] * len(texts))[index]),
            int(data.get("line_num", [0] * len(texts))[index]),
        )
        lines.setdefault(key, []).append(text)
        try:
            confidence = float(data.get("conf", ["-1"] * len(texts))[index])
        except (TypeError, ValueError):
            confidence = -1.0
        if confidence >= 0:
            confidences.append(confidence)

    text = "\n".join(" ".join(words) for words in lines.values()).strip()
    confidence = sum(confidences) / len(confidences) if confidences else None
    return OCRResult(text=text, confidence=confidence, engine="tesseract")


def _easyocr_languages() -> list[str]:
    raw = os.environ.get("ELT_EASYOCR_LANGS", "en")
    languages = [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]
    return languages or ["en"]


def _easyocr_gpu() -> bool:
    value = os.environ.get("ELT_EASYOCR_GPU", "auto").strip().lower()
    if value in {"0", "false", "off", "no"}:
        return False
    if value in {"1", "true", "on", "yes"}:
        return True
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _get_easyocr_reader() -> Any:
    global _easyocr_reader
    if _easyocr_reader is None:
        import easyocr

        kwargs: dict[str, Any] = {"gpu": _easyocr_gpu()}
        model_dir = os.environ.get("ELT_EASYOCR_MODEL_DIR", "").strip()
        if model_dir:
            kwargs["model_storage_directory"] = model_dir
        _easyocr_reader = easyocr.Reader(_easyocr_languages(), **kwargs)
    return _easyocr_reader


def _run_easyocr(image: Any) -> OCRResult:
    import numpy as np

    reader = _get_easyocr_reader()
    detections = reader.readtext(np.asarray(image), detail=1, paragraph=False)
    ordered: list[tuple[float, float, str, float]] = []
    for detection in detections:
        if len(detection) < 3:
            continue
        box, raw_text, raw_confidence = detection[0], str(detection[1] or "").strip(), detection[2]
        if not raw_text:
            continue
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError):
            confidence = -1.0
        ordered.append((min(ys), min(xs), raw_text, confidence))
    ordered.sort(key=lambda item: (item[0], item[1]))
    text = "\n".join(item[2] for item in ordered).strip()
    valid_confidences = [item[3] for item in ordered if item[3] >= 0]
    confidence = (
        sum(valid_confidences) / len(valid_confidences) * 100
        if valid_confidences
        else None
    )
    return OCRResult(text=text, confidence=confidence, engine="easyocr")


def route_page(image: Any, *, engine: str | None = None) -> OCRResult:
    """OCR one rendered page and choose the best available backend."""
    mode = (engine or _configured_engine()).strip().lower()
    if mode not in {"auto", "tesseract", "easyocr"}:
        mode = "auto"

    if mode == "easyocr":
        return _run_easyocr(image)
    if mode == "tesseract":
        return _run_tesseract(image)

    try:
        tesseract_result = _run_tesseract(image)
    except Exception as tesseract_error:
        if not _easyocr_available():
            raise
        try:
            easy_result = _run_easyocr(image)
        except Exception:
            raise tesseract_error
        return OCRResult(
            easy_result.text,
            easy_result.confidence,
            easy_result.engine,
            reason=f"tesseract unavailable: {tesseract_error}",
        )

    low_confidence = (
        not tesseract_result.text.strip()
        or (
            tesseract_result.confidence is not None
            and tesseract_result.confidence < _confidence_threshold()
        )
    )
    if not low_confidence:
        return tesseract_result
    if not _easyocr_available():
        return OCRResult(
            tesseract_result.text,
            tesseract_result.confidence,
            tesseract_result.engine,
            reason="easyocr unavailable; kept tesseract result",
        )

    try:
        easy_result = _run_easyocr(image)
    except Exception:
        return OCRResult(
            tesseract_result.text,
            tesseract_result.confidence,
            tesseract_result.engine,
            reason="easyocr failed; kept tesseract result",
        )
    if easy_result.text.strip():
        return OCRResult(
            easy_result.text,
            easy_result.confidence,
            easy_result.engine,
            reason="tesseract low confidence; escalated to easyocr",
        )
    return OCRResult(
        tesseract_result.text,
        tesseract_result.confidence,
        tesseract_result.engine,
        reason="easyocr returned no text; kept tesseract result",
    )
