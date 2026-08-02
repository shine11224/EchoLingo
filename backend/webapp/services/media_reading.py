"""Build the canonical Reading projection for timed media subtitles."""

import re

from webapp.services.v2_translation import build_translation_units


MAX_GAP_SECONDS = 1.5
MAX_PARAGRAPH_WORDS = 80
MAX_PARAGRAPH_SECONDS = 35.0


def _normalize_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _sentence_from_segment(segment: dict) -> dict | None:
    text = _normalize_text(segment.get("text"))
    if not text:
        return None
    source_segment_ids = [
        int(value) for value in (segment.get("segment_ids") or [segment.get("index")])
        if value is not None
    ]
    return {
        "sentence_key": int(segment.get("index", 0)),
        "segment_index": source_segment_ids[0] if source_segment_ids else segment.get("index"),
        "source_segment_ids": source_segment_ids,
        "text": text,
        "start_seconds": float(segment.get("start", 0.0)),
        "end_seconds": float(segment.get("end", segment.get("start", 0.0))),
    }


def _should_break(current: list[dict], sentence: dict) -> bool:
    if not current:
        return False
    gap = sentence["start_seconds"] - current[-1]["end_seconds"]
    word_count = sum(len(item["text"].split()) for item in current)
    word_count += len(sentence["text"].split())
    paragraph_span = sentence["end_seconds"] - current[0]["start_seconds"]
    return (
        gap >= MAX_GAP_SECONDS
        or word_count > MAX_PARAGRAPH_WORDS
        or paragraph_span > MAX_PARAGRAPH_SECONDS
    )


def _flush_block(blocks: list[dict], sentences: list[dict]) -> None:
    if not sentences:
        return
    source_segment_ids = []
    for sentence in sentences:
        for segment_id in sentence.get("source_segment_ids", []):
            if segment_id not in source_segment_ids:
                source_segment_ids.append(segment_id)
    blocks.append({
        "index": len(blocks) + 1,
        "text": " ".join(sentence["text"] for sentence in sentences),
        "start_seconds": sentences[0]["start_seconds"],
        "end_seconds": sentences[-1]["end_seconds"],
        "source_segment_ids": source_segment_ids,
        "sentences": [dict(sentence) for sentence in sentences],
    })


def build_media_reading_blocks(segments: list[dict]) -> list[dict]:
    """Group subtitle sentences into deterministic, timed Reading paragraphs."""
    blocks: list[dict] = []
    current: list[dict] = []
    for segment in build_translation_units(segments):
        sentence = _sentence_from_segment(segment)
        if sentence is None:
            continue
        if _should_break(current, sentence):
            _flush_block(blocks, current)
            current = []
        current.append(sentence)
    _flush_block(blocks, current)
    return blocks
