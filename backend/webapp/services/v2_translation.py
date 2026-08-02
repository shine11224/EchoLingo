"""Course-level Hy-MT subtitle translation with adaptive playback readiness."""
from __future__ import annotations

import re
import time

import db
from analyzer import SentenceAnalyzer
from schemas import Segment
from webapp.services.hy_translate import is_ready as hy_ready
from webapp.services.hy_translate import translate as hy_translate

_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
_SENTENCE_END_RE = re.compile(r"[.!?][\"')\]]?$")
MAX_TRANSLATION_UNIT_WORDS = 48
_SAFE_BUFFER_SECONDS = 120.0
_SAFE_TRANSLATION_RATE = 2.0


def _split_source_segments(segments: list[dict]) -> list[dict]:
    """Split strong punctuation inside source chunks before cross-chunk merging."""
    pieces: list[dict] = []
    for fallback_index, segment in enumerate(segments):
        source_index = int(segment.get("index", segment.get("segment_index", fallback_index)))
        source = Segment(
            index=source_index,
            text=str(segment.get("text") or ""),
            start=float(segment.get("start", segment.get("start_seconds", 0)) or 0),
            end=float(segment.get("end", segment.get("end_seconds", 0)) or 0),
        )
        for piece in SentenceAnalyzer._split_segment_sentences(source):
            words = {word.casefold() for word in _WORD_RE.findall(piece.text)}
            highlighted = [
                word for word in segment.get("highlighted_words", [])
                if str(word).casefold() in words
            ]
            meanings = {
                word: meaning
                for word, meaning in (segment.get("word_meanings") or {}).items()
                if str(word).casefold() in words
            }
            pieces.append({
                **segment,
                "index": source_index,
                "text": piece.text,
                "start": float(piece.start or 0),
                "end": float(piece.end or piece.start or 0),
                "highlighted_words": highlighted,
                "word_meanings": meanings,
            })
    return pieces


def build_translation_units(segments: list[dict]) -> list[dict]:
    """Mirror the workspace sentence-unit boundaries used for playback."""
    units: list[dict] = []
    parts: list[str] = []
    word_count = 0
    highlighted: set[str] = set()
    word_meanings: dict[str, str] = {}
    segment_ids: list[int] = []
    start: float | None = None
    end = 0.0

    def flush() -> None:
        nonlocal parts, word_count, highlighted, word_meanings, segment_ids, start, end
        text = " ".join(" ".join(parts).split())
        if text:
            units.append({
                "index": len(units),
                "text": text,
                "start": float(start or 0),
                "end": float(end),
                "highlighted_words": sorted(highlighted),
                "word_meanings": dict(word_meanings),
                "segment_ids": list(segment_ids),
            })
        parts = []
        word_count = 0
        highlighted = set()
        word_meanings = {}
        segment_ids = []
        start = None
        end = 0.0

    split_segments = _split_source_segments(segments)
    for index, segment in enumerate(split_segments):
        text = " ".join(str(segment.get("text") or "").split())
        if not text:
            continue
        if start is None:
            start = float(segment.get("start") or 0)
        end = float(segment.get("end") or segment.get("start") or start)
        parts.append(text)
        word_count += len(_WORD_RE.findall(text))
        segment_id = int(segment.get("index", index))
        if not segment_ids or segment_ids[-1] != segment_id:
            segment_ids.append(segment_id)
        highlighted.update(str(word) for word in segment.get("highlighted_words", []))
        for word, meaning in (segment.get("word_meanings") or {}).items():
            if meaning and word not in word_meanings:
                word_meanings[word] = meaning
        next_segment = split_segments[index + 1] if index + 1 < len(split_segments) else None
        gap = (float(next_segment.get("start") or 0) - end) if next_segment else 0.0
        next_ends_sentence = bool(
            next_segment
            and _SENTENCE_END_RE.search(str(next_segment.get("text") or "").strip())
        )
        if (
            _SENTENCE_END_RE.search(text)
            or (word_count >= MAX_TRANSLATION_UNIT_WORDS and not next_ends_sentence)
            or (gap > 1.4 and word_count >= 8)
            or index == len(split_segments) - 1
        ):
            flush()
    return units


def translate_lesson_subtitles(lesson_id: int) -> dict:
    units = build_translation_units(db.get_v2_subtitle_segments(lesson_id))
    total = len(units)
    if not units:
        error = "No subtitles are available for Hy-MT translation"
        db.update_v2_translation_status(
            lesson_id, status="failed", done=0, total=0, ready=False, error=error
        )
        return {"status": "failed", "done": 0, "total": 0, "error": error}

    pending = []
    for unit in units:
        cached = db.get_v2_sentence(unit["text"])
        if not cached or not str(cached.get("translation") or "").strip():
            pending.append(unit)
    total_duration = float(units[-1]["end"] or 0)
    if not pending:
        db.update_v2_translation_status(
            lesson_id,
            status="ready",
            done=total,
            total=total,
            buffer_seconds=total_duration,
            rate=0,
            ready=True,
            error="",
        )
        return {"status": "ready", "done": total, "total": total}
    if pending and not hy_ready():
        error = "Hy-MT translation model is not ready"
        db.update_v2_translation_status(
            lesson_id, status="failed", done=total - len(pending), total=total,
            ready=False, error=error,
        )
        return {"status": "failed", "done": total - len(pending), "total": total, "error": error}

    started = time.monotonic()
    done = 0
    target_buffer = min(_SAFE_BUFFER_SECONDS, total_duration)
    db.update_v2_translation_status(
        lesson_id, status="translating", done=0, total=total,
        buffer_seconds=0, rate=0, ready=False, error="",
    )
    try:
        for unit in units:
            cached = db.get_v2_sentence(unit["text"])
            translation = str((cached or {}).get("translation") or "").strip()
            if not translation:
                translation = hy_translate(unit["text"])
                if not translation:
                    raise RuntimeError("Hy-MT returned an empty translation")
                db.upsert_v2_sentence(unit["text"], translation=translation)
            done += 1
            buffer_seconds = float(unit["end"] or 0)
            rate = buffer_seconds / max(time.monotonic() - started, 0.001)
            ready = buffer_seconds >= target_buffer and rate >= _SAFE_TRANSLATION_RATE
            db.update_v2_translation_status(
                lesson_id,
                status="translating",
                done=done,
                total=total,
                buffer_seconds=buffer_seconds,
                rate=rate,
                ready=ready,
            )
    except Exception as exc:
        db.update_v2_translation_status(
            lesson_id, status="failed", done=done, total=total,
            ready=False, error=str(exc),
        )
        return {"status": "failed", "done": done, "total": total, "error": str(exc)}

    db.update_v2_translation_status(
        lesson_id,
        status="ready",
        done=total,
        total=total,
        buffer_seconds=total_duration,
        ready=True,
        error="",
    )
    return {"status": "ready", "done": total, "total": total}
