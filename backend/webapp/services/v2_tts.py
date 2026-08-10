"""Generate persistent audio and timed subtitles for Reading lessons."""
from __future__ import annotations

import os
import re
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import db
from analyzer import SentenceAnalyzer
from webapp.runtime import credit_meter
from webapp.services.natural_tts import synthesize_natural_speech_with_timestamps
from webapp.services.v2_translation import translate_lesson_subtitles
from webapp.storage import user_assets
from webapp.storage.lessons import OUTPUT_DIR

_ACTIVE_LESSONS: set[tuple[str, int]] = set()
_ACTIVE_LOCK = threading.Lock()

# 块级合成并发路数：edge_tts 是网络型任务，3 路约 3 倍吞吐；超限流风险低（调用数已是块级）
_TTS_WORKERS = max(1, int(os.environ.get("ELT_TTS_CONCURRENCY", "3")))


def reading_tts_output_path(lesson_id: int) -> Path:
    return user_assets.user_output_subdir(
        "v2_assets", str(lesson_id),
        fallback=OUTPUT_DIR / "v2_assets" / str(lesson_id),
    ) / "reading.wav"


def reading_tts_is_cached(lesson_id: int) -> bool:
    """朗读成品已存在且课程元数据指向它：GET/重放可直接播放，绝不再计费。"""
    lesson = db.get_v2_lesson(lesson_id)
    return bool(
        lesson
        and lesson.get("media_kind") == "generated_audio"
        and str(lesson.get("media_url") or "")
        and reading_tts_output_path(lesson_id).exists()
    )


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
    # 进行中集合按 (用户 scope, lesson_id)：不同用户相同 lesson_id 可同时排队
    active_key = (user_assets.current_scope_key(), int(lesson_id))
    with _ACTIVE_LOCK:
        if active_key in _ACTIVE_LESSONS:
            return False
        _ACTIVE_LESSONS.add(active_key)
    db.configure_v2_lesson_translation(lesson_id, requested=True)
    db.set_v2_lesson_status(lesson_id, subtitle_status="pending")
    # 排队即声明会有生成音频：能力立即开放精听（加载态），不再等到成品落盘
    db.update_v2_lesson_metadata(lesson_id, media_kind="generated_audio")
    db.spawn_with_db_context(
        _run_reading_tts, lesson_id, active_key,
        name=f"reading-tts-{lesson_id}",
    )
    return True


def _run_reading_tts(lesson_id: int, active_key: tuple[str, int] | None = None) -> None:
    if active_key is None:
        active_key = (user_assets.current_scope_key(), int(lesson_id))
    try:
        result = build_reading_tts(lesson_id)
    except Exception as exc:
        db.set_v2_lesson_status(lesson_id, subtitle_status="failed", subtitle_error=str(exc))
        # 合成失败：撤回生成音频声明，课程回到「无精听」能力
        db.update_v2_lesson_metadata(lesson_id, media_kind="")
        # Task 8：合成失败释放 reading_tts 预授权（上下文 op 不存在时 no-op）
        credit_meter.release_current(reason=f"reading_tts failed: {exc}"[:500])
    else:
        credit_meter.settle_current(actual_usage={
            "engine": "sapi",
            "lesson_id": int(lesson_id),
            "characters": result.get("characters", 0),
            "sentences": result.get("sentence_count", 0),
            "duration": result.get("duration", 0),
        })
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_LESSONS.discard(active_key)


_WORD_TOKEN = re.compile(r"[a-z0-9']+")


def align_sentences_to_boundaries(
    sentences: list[str],
    boundaries: list[dict],
    block_duration: float,
) -> list[dict]:
    """把整段合成里的词边界（WordBoundary）切回句级 [start, end) 时间轴。

    按顺序把每句首词匹配到边界事件；任一句失配时整段回退为按字符数比例切分。
    """
    bounds: list[tuple[str, float, float]] = []
    for item in boundaries:
        tokens = _WORD_TOKEN.findall(str(item.get("text") or "").lower())
        if not tokens:
            continue
        offset = float(item.get("offset") or 0.0)
        bounds.append((tokens[0], offset, offset + float(item.get("duration") or 0.0)))

    starts: list[float] = []
    cursor = 0
    aligned = bool(sentences)
    for sentence in sentences:
        words = _WORD_TOKEN.findall(sentence.lower())
        hit = None
        if words:
            for j in range(cursor, min(cursor + 8, len(bounds))):
                if bounds[j][0] == words[0]:
                    hit = j
                    break
        if hit is None:
            aligned = False
            break
        starts.append(bounds[hit][1])
        cursor = hit + 1

    if aligned:
        end_cap = bounds[-1][2] if bounds else block_duration
        return [
            {"start": start, "end": max(starts[i + 1] if i + 1 < len(starts) else end_cap, start)}
            for i, start in enumerate(starts)
        ]

    total = sum(max(len(sentence), 1) for sentence in sentences) or 1
    spans = []
    start = 0.0
    for i, sentence in enumerate(sentences):
        end = block_duration if i == len(sentences) - 1 else start + block_duration * max(len(sentence), 1) / total
        spans.append({"start": start, "end": end})
        start = end
    return spans


def _synthesize_block_with_retry(block_text: str, part_path: Path, *, index: int,
                                 attempts: int = 3) -> list[dict]:
    """edge_tts 偶发 NoAudioReceived/网络抖动：逐块重试，避免一次抖动毁掉全文合成。"""
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return synthesize_natural_speech_with_timestamps(block_text, part_path)
        except Exception as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(
        f"Reading TTS block {index} failed after {attempts} attempts: {last_exc}"
    ) from last_exc


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

    asset_dir = user_assets.user_output_subdir(
        "v2_assets", str(lesson_id), fallback=OUTPUT_DIR / "v2_assets" / str(lesson_id)
    )
    parts_dir = asset_dir / "tts_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    output_path = asset_dir / "reading.wav"
    segments: list[dict] = []
    elapsed = 0.0
    audio_format: tuple[int, int, int, str, str] | None = None

    # 块级合成并发执行；拼装仍在主线程按块序串行，保证时间轴确定
    synthesis_jobs = [
        (block_index, group)
        for block_index, group in enumerate(block_sentences)
        if group
    ]
    block_boundaries: dict[int, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=_TTS_WORKERS) as pool:
        futures = {
            pool.submit(
                _synthesize_block_with_retry,
                " ".join(group),
                parts_dir / f"{block_index:05d}.wav",
                index=block_index,
            ): block_index
            for block_index, group in synthesis_jobs
        }
        for future in as_completed(futures):
            block_boundaries[futures[future]] = future.result()

    with wave.open(str(output_path), "wb") as combined:
        # 预置参数：首块合成前失败时 close 不会以 "# channels not specified" 掩盖真实错误
        combined.setnchannels(1)
        combined.setsampwidth(2)
        combined.setframerate(24_000)
        for block_index, group in synthesis_jobs:
            part_path = parts_dir / f"{block_index:05d}.wav"
            boundaries = block_boundaries[block_index]
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
                block_duration = part.getnframes() / max(part.getframerate(), 1)
            combined.writeframes(frames)
            for sentence, span in zip(group, align_sentences_to_boundaries(group, boundaries, block_duration)):
                segments.append({
                    "index": len(segments),
                    "start": elapsed + span["start"],
                    "end": elapsed + span["end"],
                    "text": sentence,
                })
            elapsed += block_duration
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
        "characters": sum(len(sentence) for sentence in sentences),
        "duration": elapsed,
        "translation": translation,
    }
