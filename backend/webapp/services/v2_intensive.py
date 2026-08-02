"""Build a stable whole-document sentence projection for v2 intensive study."""
from __future__ import annotations

import re

import db
from webapp.services.mfa_alignment import get_alignment_status, load_lesson_alignment, numeric_token_ipa
from webapp.services.v2_translation import build_translation_units
from webapp.services.v2_vocab import highlight_reading_blocks, highlight_segments

READING_KEY_STRIDE = 10_000
_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def reading_sentence_key(block_index: int, sentence_index: int) -> int:
    return -(((int(block_index) + 1) * READING_KEY_STRIDE) + int(sentence_index) + 1)


def _normalize_sentence(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def _stored_translation(text: str) -> str:
    sentence = db.get_v2_sentence(text)
    return str((sentence or {}).get("translation") or "").strip()


def _enrich_aligned_words(words: list[dict]) -> list[dict]:
    enriched = []
    for word in words:
        item = dict(word)
        if not str(item.get("ipa") or "").strip():
            item["ipa"] = numeric_token_ipa(str(item.get("text") or item.get("word") or ""))
        enriched.append(item)
    return enriched


def _split_reading_text(text: str) -> list[str]:
    normalized = " ".join((text or "").split())
    if not normalized:
        return []
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", normalized) if part.strip()]


def _saved_lookup(lesson_id: int) -> tuple[dict[int, dict], dict[str, dict]]:
    saved = db.get_v2_phase_b_sentences(lesson_id)
    return (
        {int(item["segment_index"]): item for item in saved},
        {_normalize_sentence(item.get("text", "")): item for item in saved if item.get("text")},
    )


def _reading_highlighted_words(text: str, hidden_words: set[str]) -> list[str]:
    highlighted = highlight_reading_blocks(
        [{"text": text}],
        hidden_words=hidden_words,
        include_meanings=False,
    )
    words: list[str] = []
    seen: set[str] = set()
    for item in highlighted["blocks"][0].get("highlights", []):
        word = str(item.get("normalized") or item.get("word") or "").lower()
        if word and word not in seen:
            seen.add(word)
            words.append(word)
    return words


def _merge_saved_highlights(
    text: str,
    highlighted_words: list[str],
    saved_words: set[str],
    hidden_words: set[str],
) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for word in highlighted_words:
        normalized = str(word or "").lower()
        if normalized and normalized not in hidden_words and normalized not in seen:
            seen.add(normalized)
            merged.append(normalized)
    for token in _WORD_RE.findall(text):
        normalized = token.lower()
        if normalized in saved_words and normalized not in hidden_words and normalized not in seen:
            seen.add(normalized)
            merged.append(normalized)
    return merged


def build_intensive_document(lesson_id: int) -> dict:
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise ValueError("Lesson not found")

    saved_by_key, saved_by_text = _saved_lookup(lesson_id)
    hidden_words = db.get_v2_lesson_hidden_words(lesson_id) | db.get_mastered_review_targets()
    lesson_words = {
        str(item.get("word") or "").lower()
        for item in db.get_v2_lesson_words(lesson_id)
        if item.get("word")
    }
    sentences: list[dict] = []
    is_reading = str(lesson.get("source_type", "")).startswith("reading")
    alignment = None if is_reading else load_lesson_alignment(lesson_id)
    aligned_by_key = {
        int(item.get("key", -1)): item
        for item in (alignment or {}).get("sentences", [])
        if isinstance(item, dict)
    }

    if is_reading:
        for block in db.get_v2_reading_blocks(lesson_id):
            block_index = int(block["index"])
            block_texts = _split_reading_text(block.get("text", ""))
            timed_sentences = block.get("sentences") or []
            for sentence_index, text in enumerate(block_texts):
                key = reading_sentence_key(block_index, sentence_index)
                saved = saved_by_key.get(key) or saved_by_text.get(_normalize_sentence(text))
                timed = (
                    timed_sentences[sentence_index]
                    if sentence_index < len(timed_sentences)
                    and _normalize_sentence(timed_sentences[sentence_index].get("text", ""))
                    == _normalize_sentence(text)
                    else {}
                )
                sentences.append(
                    {
                        "key": key,
                        "phase_b_key": int(saved["segment_index"]) if saved else key,
                        "text": text,
                        "translation": _stored_translation(text),
                        "block_index": block_index,
                        "sentence_index": sentence_index,
                        "start_seconds": float(timed.get("start_seconds") or 0),
                        "end_seconds": float(timed.get("end_seconds") or 0),
                        "saved": bool(saved),
                        "sentence_id": int(saved["sentence_id"]) if saved and saved.get("sentence_id") else None,
                        "oral_analysis": ((saved.get("pattern") or {}).get("analysis") or {}) if saved else {},
                        "tags": saved.get("tags", []) if saved else [],
                        "highlighted_words": _merge_saved_highlights(
                            text,
                            _reading_highlighted_words(text, hidden_words),
                            lesson_words,
                            hidden_words,
                        ),
                    }
                )
    else:
        segments = highlight_segments(
            build_translation_units(db.get_v2_subtitle_segments(lesson_id)),
            hidden_words=hidden_words,
            include_meanings=False,
        )
        for fallback_index, segment in enumerate(segments):
            key = int(segment.get("index", fallback_index))
            text = " ".join(str(segment.get("text", "")).split())
            if not text:
                continue
            saved = saved_by_key.get(key)
            if saved and _normalize_sentence(saved.get("text", "")) != _normalize_sentence(text):
                saved = None
            saved = saved or saved_by_text.get(_normalize_sentence(text))
            aligned = aligned_by_key.get(key)
            if aligned and _normalize_sentence(aligned.get("text", "")) != _normalize_sentence(text):
                aligned = None
            aligned_ready = bool(
                aligned
                and aligned.get("boundary_confidence") in {"high", "medium"}
                and aligned.get("words")
            )
            sentences.append(
                {
                    "key": key,
                    "phase_b_key": int(saved["segment_index"]) if saved else key,
                    "text": text,
                    "translation": _stored_translation(text),
                    "block_index": None,
                    "sentence_index": fallback_index,
                    "start_seconds": float(
                        aligned.get("start_seconds")
                        if aligned_ready
                        else segment.get("start_seconds", segment.get("start", 0))
                        or 0
                    ),
                    "end_seconds": float(
                        aligned.get("end_seconds")
                        if aligned_ready
                        else segment.get("end_seconds", segment.get("end", 0))
                        or 0
                    ),
                    "timing_source": "mfa" if aligned_ready else "subtitle",
                    "alignment_coverage": float(aligned.get("coverage") or 0) if aligned else 0,
                    "boundary_confidence": (
                        str(aligned.get("boundary_confidence") or "fallback")
                        if aligned else "fallback"
                    ),
                    "pause_before_ms": int(aligned.get("pause_before_ms") or 0) if aligned else 0,
                    "pause_after_ms": int(aligned.get("pause_after_ms") or 0) if aligned else 0,
                    "aligned_words": _enrich_aligned_words(aligned.get("words", [])) if aligned_ready else [],
                    "saved": bool(saved),
                    "sentence_id": int(saved["sentence_id"]) if saved and saved.get("sentence_id") else None,
                    "oral_analysis": ((saved.get("pattern") or {}).get("analysis") or {}) if saved else {},
                    "tags": saved.get("tags", []) if saved else [],
                    "highlighted_words": _merge_saved_highlights(
                        text,
                        segment.get("highlighted_words", []),
                        lesson_words,
                        hidden_words,
                    ),
                }
            )

    return {
        "lesson": lesson,
        "lesson_words": sorted(lesson_words),
        "sentences": sentences,
        "alignment": (
            get_alignment_status(lesson_id)
            if not is_reading
            else {"lesson_id": lesson_id, "status": "not_applicable"}
        ),
    }
