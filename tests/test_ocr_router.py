import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from webapp.services import ocr_router


def test_auto_keeps_high_confidence_tesseract(monkeypatch):
    easy_calls = []
    monkeypatch.setenv("ELT_OCR_ENGINE", "auto")
    monkeypatch.setattr(
        ocr_router,
        "_run_tesseract",
        lambda image: ocr_router.OCRResult("clean text", 92.0, "tesseract"),
    )
    monkeypatch.setattr(ocr_router, "_easyocr_available", lambda: True)
    monkeypatch.setattr(
        ocr_router,
        "_run_easyocr",
        lambda image: easy_calls.append(image) or ocr_router.OCRResult("easy text", 98.0, "easyocr"),
    )

    result = ocr_router.route_page(object())

    assert result.engine == "tesseract"
    assert result.text == "clean text"
    assert easy_calls == []


def test_auto_escalates_low_confidence_tesseract_to_easyocr(monkeypatch):
    monkeypatch.setenv("ELT_OCR_ENGINE", "auto")
    monkeypatch.setattr(
        ocr_router,
        "_run_tesseract",
        lambda image: ocr_router.OCRResult("garbled text", 42.0, "tesseract"),
    )
    monkeypatch.setattr(ocr_router, "_easyocr_available", lambda: True)
    monkeypatch.setattr(
        ocr_router,
        "_run_easyocr",
        lambda image: ocr_router.OCRResult("recovered text", 88.0, "easyocr"),
    )

    result = ocr_router.route_page(object())

    assert result.engine == "easyocr"
    assert result.text == "recovered text"
    assert "low confidence" in result.reason


def test_auto_keeps_tesseract_when_easyocr_is_unavailable(monkeypatch):
    monkeypatch.setenv("ELT_OCR_ENGINE", "auto")
    monkeypatch.setattr(
        ocr_router,
        "_run_tesseract",
        lambda image: ocr_router.OCRResult("usable text", 38.0, "tesseract"),
    )
    monkeypatch.setattr(ocr_router, "_easyocr_available", lambda: False)

    result = ocr_router.route_page(object())

    assert result.engine == "tesseract"
    assert result.text == "usable text"
    assert "unavailable" in result.reason


def test_auto_uses_easyocr_when_tesseract_is_missing(monkeypatch):
    monkeypatch.setenv("ELT_OCR_ENGINE", "auto")

    def missing_tesseract(image):
        raise RuntimeError("Tesseract binary not found")

    monkeypatch.setattr(ocr_router, "_run_tesseract", missing_tesseract)
    monkeypatch.setattr(ocr_router, "_easyocr_available", lambda: True)
    monkeypatch.setattr(
        ocr_router,
        "_run_easyocr",
        lambda image: ocr_router.OCRResult("easy fallback", 81.0, "easyocr"),
    )

    result = ocr_router.route_page(object())

    assert result.engine == "easyocr"
    assert result.text == "easy fallback"
    assert "tesseract" in result.reason


def test_explicit_tesseract_does_not_silently_switch(monkeypatch):
    monkeypatch.setenv("ELT_OCR_ENGINE", "tesseract")
    monkeypatch.setattr(
        ocr_router,
        "_run_tesseract",
        lambda image: (_ for _ in ()).throw(RuntimeError("Tesseract binary not found")),
    )
    monkeypatch.setattr(ocr_router, "_easyocr_available", lambda: True)

    with pytest.raises(RuntimeError, match="Tesseract binary not found"):
        ocr_router.route_page(object())
