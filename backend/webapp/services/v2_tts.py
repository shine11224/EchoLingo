"""Generate persistent audio and timed subtitles for Reading lessons."""
from __future__ import annotations

import threading
import wave
from pathlib import Path

import db
from analyzer import SentenceAnalyzer
from webapp.services.v2_review_export import synthesize_sentence_audio
from webapp.services.v2_translation import translate_lesson_subtitles
from webapp.storage.lessons import OUTPUT_DIR

_ACTIVE_LESSONS: set[int] = set()
_ACTIVE_LOCK = threading.Lock()


def build_timed_reading_blocks(source_blocks: list[dict], segments: list[dict]) -> list[dict]:
    """Attach generated-audio timing while preserving the imported paragraph layout."""
    timed_blocks = []
    segment_cursor = 0
    for block in source_blocks:
        sentence_group = [
            sentence
            for sentence in SentenceAnalyzer._split_text_sentences(str(block.get("text") or ""))
            if sentence.strip()
        ]
        timed_sentences = segments[segment_cursor:segment_cursor + len(sentence_group)]
        segment_cursor += len(sentence_group)
        block_copy = dict(block)
        if timed_sentences:
            block_copy.update({
                "start_seconds": timed_sentences[0]["start"],
                "end_seconds": timed_sentences[-1]["end"],
                "source_segment_ids": [item["index"] for item in timed_sentences],
                "sentences": [
                    {
                        "segment_index": item["index"],
                        "source_segment_ids": [item["index"]],
                        "text": item["text"],
                        "start_seconds": item["start"],
                        "end_seconds": item["end"],
                    }
                    for item in timed_sentences
                ],
            })
        timed_blocks.append(block_copy)
    return timed_blocks


def enqueue_reading_tts(lesson_id: int) -> bool:
    with _ACTIVE_LOCK:
        if lesson_id in _ACTIVE_LESSONS:
            return False
        _ACTIVE_LESSONS.add(lesson_id)
    db.configure_v2_lesson_translation(lesson_id, requested=True)
    db.set_v2_lesson_status(lesson_id, subtitle_status="pending")
    thread = threading.Thread(
        target=_run_reading_tts,
        args=(lesson_id,),
        daemon=True,
        name=f"reading-tts-{lesson_id}",
    )
    thread.start()
    return True


def _run_reading_tts(lesson_id: int) -> None:
    try:
        build_reading_tts(lesson_id)
    except Exception as exc:
        db.set_v2_lesson_status(lesson_id, subtitle_status="failed", subtitle_error=str(exc))
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_LESSONS.discard(lesson_id)


def build_reading_tts(lesson_id: int) -> dict:
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise ValueError(f"Lesson {lesson_id} not found")
    db.configure_v2_lesson_translation(lesson_id, requested=True)
    source_blocks = db.get_v2_reading_blocks(lesson_id)
    block_sentences = [
        [
            sentence
            for sentence in SentenceAnalyzer._split_text_sentences(str(block.get("text") or ""))
            if sentence.strip()
        ]
        for block in source_blocks
    ]
    sentences = [sentence for group in block_sentences for sentence in group]
    if not sentences:
        raise ValueError("Reading lesson has no text to synthesize")

    asset_dir = OUTPUT_DIR / "v2_assets" / str(lesson_id)
    parts_dir = asset_dir / "tts_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    output_path = asset_dir / "reading.wav"
    segments: list[dict] = []
    elapsed = 0.0
    audio_format: tuple[int, int, int, str, str] | None = None

    with wave.open(str(output_path), "wb") as combined:
        for index, sentence in enumerate(sentences):
            part_path = parts_dir / f"{index:05d}.wav"
            synthesize_sentence_audio(sentence, part_path)
            with wave.open(str(part_path), "rb") as part:
                current_format = (
                    part.getnchannels(), part.getsampwidth(), part.getframerate(),
                    part.getcomptype(), part.getcompname(),
                )
                if audio_format is None:
                    audio_format = current_format
                    combined.setnchannels(current_format[0])
                    combined.setsampwidth(current_format[1])
                    combined.setframerate(current_format[2])
                    combined.setcomptype(current_format[3], current_format[4])
                elif current_format != audio_format:
                    raise RuntimeError("Reading TTS produced incompatible WAV formats")
                frames = part.readframes(part.getnframes())
                duration = part.getnframes() / max(part.getframerate(), 1)
            combined.writeframes(frames)
            start = elapsed
            elapsed += duration
            segments.append({"index": index, "start": start, "end": elapsed, "text": sentence})
            part_path.unlink(missing_ok=True)

    try:
        parts_dir.rmdir()
    except OSError:
        pass
    db.replace_v2_subtitle_segments(lesson_id, segments)
    db.replace_v2_reading_blocks(
        lesson_id,
        build_timed_reading_blocks(source_blocks, segments),
    )
    db.update_v2_lesson_metadata(
        lesson_id,
        duration=elapsed,
        media_url=f"/output/v2_assets/{lesson_id}/reading.wav",
        media_kind="generated_audio",
    )
    db.set_v2_lesson_status(lesson_id, subtitle_status="ready")
    translation = translate_lesson_subtitles(lesson_id)
    return {
        "status": "ready",
        "sentence_count": len(segments),
        "duration": elapsed,
        "translation": translation,
    }
