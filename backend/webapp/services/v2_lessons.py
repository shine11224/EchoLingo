"""V2 lesson orchestration service."""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import hashlib
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import db
from sources.baidu import build_local_video_lesson
from sources.bilibili import build_bilibili_lesson
from sources.youtube import download_youtube_audio, extract_video_id, fetch_youtube_subtitles, source_bundle_to_segment_dicts
from webapp.services.media_reading import build_media_reading_blocks
from webapp.services.reading_import import build_reading_blocks_from_text, extract_text_from_pdf, extract_text_from_upload
from webapp.services.v2_translation import translate_lesson_subtitles
from webapp.services.v2_tts import build_timed_reading_blocks, enqueue_reading_tts
from webapp.storage.lessons import OUTPUT_DIR


_AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac"}
_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv"}
_READING_UPLOAD_JOBS: dict[str, dict] = {}
_READING_UPLOAD_JOBS_LOCK = threading.Lock()
_READING_UPLOAD_INFLIGHT: dict[str, str] = {}
_READING_UPLOAD_SLOTS = threading.BoundedSemaphore(2)
_READING_UPLOAD_JOB_LIMIT = 100


class ReadingUploadBusyError(RuntimeError):
    pass


def start_youtube_lesson(url: str, *, translate: bool = False) -> dict:
    video_id = extract_video_id(url)
    lesson = db.create_v2_lesson(
        source_type="youtube",
        source_url=url,
        video_id=video_id,
        title="",
        duration=0,
    )
    db.configure_v2_lesson_translation(lesson["id"], requested=translate)
    lesson = db.get_v2_lesson(lesson["id"]) or lesson
    return {"lesson": lesson, "workspace_url": f"/workspace/{lesson['id']}"}


def start_local_lesson(local_path: str, *, transcript_path: str | None = None,
                       whisper_model: str = "large-v3", translate: bool = False) -> dict:
    media_path = _resolve_local_path(local_path)
    if not media_path.exists():
        raise FileNotFoundError(f"Local media not found: {media_path}")
    media_kind = _media_kind(media_path)
    lesson = db.create_v2_lesson(
        source_type=media_kind,
        source_url=str(media_path),
        title=media_path.stem,
        media_kind=media_kind,
    )
    media_url = _copy_media_for_lesson(lesson["id"], media_path)
    db.update_v2_lesson_metadata(lesson["id"], media_url=media_url, media_kind=media_kind, title=media_path.stem)
    db.configure_v2_lesson_translation(lesson["id"], requested=translate)
    lesson = db.get_v2_lesson(lesson["id"]) or lesson
    enqueue_local_import(
        lesson["id"],
        str(media_path),
        transcript_path=transcript_path,
        whisper_model=whisper_model,
        translate=translate,
    )
    return {"lesson": lesson, "workspace_url": f"/workspace/{lesson['id']}"}


def start_bilibili_lesson(url: str, *, download_video: bool = False,
                          whisper_model: str = "large-v3", translate: bool = False) -> dict:
    lesson = db.create_v2_lesson(
        source_type="bilibili",
        source_url=url,
        title="Bilibili lesson",
    )
    db.configure_v2_lesson_translation(lesson["id"], requested=translate)
    lesson = db.get_v2_lesson(lesson["id"]) or lesson
    enqueue_bilibili_import(
        lesson["id"], url, download_video=download_video,
        whisper_model=whisper_model, translate=translate,
    )
    return {"lesson": lesson, "workspace_url": f"/workspace/{lesson['id']}"}


def start_reading_text_lesson(title: str, text: str, *, tts: bool = False) -> dict:
    imported = build_reading_blocks_from_text(text, title=title or "Reading Passage")
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url=f"manual:{imported['title']}",
        video_id="",
        title=imported["title"],
        duration=0,
        lesson_mode="reading",
    )
    db.replace_v2_reading_blocks(lesson["id"], imported["blocks"])
    if tts:
        enqueue_reading_tts(lesson["id"])
    lesson = db.get_v2_lesson(lesson["id"]) or lesson
    return {"lesson": lesson, "blocks": imported["blocks"], "workspace_url": f"/workspace/{lesson['id']}"}


def start_reading_pdf_lesson(local_path: str, *, title: str = "", tts: bool = False) -> dict:
    pdf_path = _resolve_local_path(local_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"Reading PDF not found: {pdf_path}")
    text = extract_text_from_pdf(pdf_path)
    return start_reading_text_lesson(title=title or pdf_path.stem, text=text, tts=tts)


def start_reading_upload_lesson(filename: str, content: bytes, *, tts: bool = False) -> dict:
    if not content:
        raise ValueError("Uploaded reading file is empty")
    digest = hashlib.sha1(content).hexdigest()[:12]
    source_url = f"upload:{digest}"
    cached_lesson = db.get_cached_v2_reading_lesson(source_url)
    if cached_lesson:
        if tts:
            enqueue_reading_tts(cached_lesson["id"])
        return {"cached": True, "workspace_url": f"/workspace/{cached_lesson['id']}"}
    with _READING_UPLOAD_JOBS_LOCK:
        inflight_job_id = _READING_UPLOAD_INFLIGHT.get(source_url)
        if inflight_job_id:
            return {"cached": False, "job_id": inflight_job_id, "status": "queued"}
    if not _READING_UPLOAD_SLOTS.acquire(blocking=False):
        raise ReadingUploadBusyError("Reading import service is busy; retry shortly")
    try:
        with _READING_UPLOAD_JOBS_LOCK:
            terminal_ids = [
                existing_id for existing_id, state in _READING_UPLOAD_JOBS.items()
                if state.get("stage") in {"done", "failed"}
            ]
            while len(_READING_UPLOAD_JOBS) >= _READING_UPLOAD_JOB_LIMIT and terminal_ids:
                _READING_UPLOAD_JOBS.pop(terminal_ids.pop(0), None)
            job_id = uuid.uuid4().hex
            _READING_UPLOAD_JOBS[job_id] = {
                "job_id": job_id,
                "stage": "queued",
                "percent": 0,
                "message": "Reading file queued",
                "error": "",
                "workspace_url": "",
            }
            _READING_UPLOAD_INFLIGHT[source_url] = job_id
        thread = db.spawn_with_db_context(
            _run_reading_upload,
            job_id, filename, content, source_url, tts,
            name=f"reading-import-{job_id[:8]}",
        )
    except Exception:
        _READING_UPLOAD_SLOTS.release()
        raise
    return {"cached": False, "job_id": job_id, "status": "queued"}


def _set_reading_upload_job(job_id: str, **changes) -> None:
    with _READING_UPLOAD_JOBS_LOCK:
        current = _READING_UPLOAD_JOBS.get(job_id, {"job_id": job_id})
        current.update(changes)
        _READING_UPLOAD_JOBS[job_id] = current


def get_reading_upload_status(job_id: str) -> dict:
    with _READING_UPLOAD_JOBS_LOCK:
        status = _READING_UPLOAD_JOBS.get(job_id)
        if not status:
            raise ValueError("Reading upload job not found")
        return dict(status)


def _process_reading_upload(job_id: str, filename: str, content: bytes, source_url: str, tts: bool = False) -> None:
    try:
        _set_reading_upload_job(job_id, stage="parsing", percent=10, message="Extracting text")
        text = extract_text_from_upload(filename, content)
        title = Path(filename or "Reading Passage").stem or "Reading Passage"
        _set_reading_upload_job(job_id, stage="building", percent=55, message="Building reading blocks")
        imported = build_reading_blocks_from_text(text, title=title)
        _set_reading_upload_job(job_id, stage="saving", percent=80, message="Saving lesson")
        lesson = db.create_v2_lesson(
            source_type="reading_upload",
            source_url=source_url,
            video_id="",
            title=imported["title"],
            duration=0,
            lesson_mode="reading",
        )
        db.replace_v2_reading_blocks(lesson["id"], imported["blocks"])
        if tts:
            enqueue_reading_tts(lesson["id"])
        _set_reading_upload_job(
            job_id,
            stage="done",
            percent=100,
            message="Reading lesson ready",
            error="",
            workspace_url=f"/workspace/{lesson['id']}",
        )
    except Exception as exc:
        _set_reading_upload_job(
            job_id,
            stage="failed",
            message="Reading import failed",
            error=str(exc),
        )
    finally:
        with _READING_UPLOAD_JOBS_LOCK:
            if _READING_UPLOAD_INFLIGHT.get(source_url) == job_id:
                _READING_UPLOAD_INFLIGHT.pop(source_url, None)


def _run_reading_upload(job_id: str, filename: str, content: bytes, source_url: str, tts: bool = False) -> None:
    try:
        _process_reading_upload(job_id, filename, content, source_url, tts)
    finally:
        _READING_UPLOAD_SLOTS.release()


def enqueue_subtitle_fetch(lesson_id: int, url: str, *, translate: bool = False) -> None:
    db.spawn_with_db_context(_fetch_and_store_subtitles, lesson_id, url, translate)


def _store_media_segments(lesson_id: int, segments: list[dict]) -> None:
    db.replace_v2_subtitle_segments(lesson_id, segments)
    db.replace_v2_reading_blocks(lesson_id, build_media_reading_blocks(segments))


def _enqueue_media_alignment(lesson_id: int) -> None:
    """Best-effort enhancement; alignment failure must never fail lesson import."""
    try:
        from webapp.services.mfa_alignment import enqueue_lesson_alignment

        enqueue_lesson_alignment(lesson_id)
    except Exception:
        return


def ensure_media_reading_blocks(lesson_id: int, lesson: dict) -> list[dict]:
    blocks = db.get_v2_reading_blocks(lesson_id)
    generated_audio = (
        str(lesson.get("source_type") or "").startswith("reading")
        and str(lesson.get("media_kind") or "") == "generated_audio"
        and bool(str(lesson.get("media_url") or "").strip())
    )
    if generated_audio:
        rebuilt = build_timed_reading_blocks(
            blocks,
            db.get_v2_subtitle_segments(lesson_id),
        )
        if rebuilt != blocks:
            db.replace_v2_reading_blocks(lesson_id, rebuilt)
            return rebuilt
        return blocks
    media_source = str(lesson.get("source_type") or "") in {
        "youtube", "bilibili", "local_audio", "local_video",
    }
    if not media_source:
        return blocks
    segments = db.get_v2_subtitle_segments(lesson_id)
    rebuilt = build_media_reading_blocks(segments)
    if rebuilt == blocks:
        return blocks
    if rebuilt:
        db.replace_v2_reading_blocks(lesson_id, rebuilt)
        return rebuilt
    return blocks


_SENTENCE_END_RE = re.compile(r"[.!?][\"')\]]?$")
# 自动字幕几乎无句末标点；低于此比例判定为"无标点字幕"，改用 whisper 转录
_MIN_PUNCTUATION_RATIO = 0.25


def _segments_punctuation_ratio(segments: list[dict]) -> float:
    texts = [str(s.get("text") or "").strip() for s in segments]
    texts = [t for t in texts if t]
    if not texts:
        return 0.0
    hits = sum(1 for t in texts if _SENTENCE_END_RE.search(t))
    return hits / len(texts)


def _transcribe_youtube_segments(url: str) -> list[dict]:
    """Download audio and transcribe (paraformer/openai/groq/faster-whisper cascade)
    for punctuation-free auto-captions; whisper output carries sentence punctuation
    so downstream rule-based segmentation works."""
    from sources.baidu import _transcribe_with_optional_whisper

    model = os.environ.get("YOUTUBE_TRANSCRIBE_MODEL", "").strip() or (
        "paraformer" if os.environ.get("DASHSCOPE_API_KEY") else "large-v3"
    )
    audio_path = download_youtube_audio(url)
    segments = _transcribe_with_optional_whisper(Path(audio_path), model, output_dir=OUTPUT_DIR)
    return [
        {"index": i + 1, "start": float(s.start), "end": float(s.end), "text": s.text}
        for i, s in enumerate(segments)
        if str(s.text or "").strip()
    ]


def _fetch_and_store_subtitles(lesson_id: int, url: str, translate: bool = False) -> None:
    try:
        bundle = fetch_youtube_subtitles(url)
        segments = source_bundle_to_segment_dicts(bundle)
        ratio = _segments_punctuation_ratio(segments)
        if segments and ratio < _MIN_PUNCTUATION_RATIO:
            print(
                f"[v2] lesson {lesson_id}: 字幕标点率 {ratio:.0%}（疑似自动字幕），改用 whisper 转录",
                flush=True,
            )
            try:
                transcribed = _transcribe_youtube_segments(url)
                if transcribed:
                    segments = transcribed
            except Exception as exc:
                # 转录失败保留原字幕，课程仍可用（断句质量差但不阻断导入）
                print(f"[v2] lesson {lesson_id}: whisper 转录失败，保留原字幕：{exc}", flush=True)
        _store_media_segments(lesson_id, segments)
        db.set_v2_lesson_status(lesson_id, subtitle_status="ready")
        _enqueue_media_alignment(lesson_id)
        if translate:
            translate_lesson_subtitles(lesson_id)
    except Exception as exc:
        db.set_v2_lesson_status(lesson_id, subtitle_status="failed", subtitle_error=str(exc))
        if translate:
            db.update_v2_translation_status(
                lesson_id, status="failed", ready=False,
                error=f"Subtitle generation failed: {exc}",
            )


def enqueue_local_import(lesson_id: int, media_path: str, *, transcript_path: str | None = None,
                         whisper_model: str = "large-v3", translate: bool = False) -> None:
    db.spawn_with_db_context(
        _import_local_media, lesson_id, media_path, transcript_path, whisper_model, translate)


def enqueue_bilibili_import(lesson_id: int, url: str, *, download_video: bool = False,
                            whisper_model: str = "large-v3", translate: bool = False) -> None:
    db.spawn_with_db_context(
        _import_bilibili_media, lesson_id, url, download_video, whisper_model, translate)


def _import_local_media(lesson_id: int, media_path: str, transcript_path: str | None,
                        whisper_model: str, translate: bool = False) -> None:
    try:
        media = Path(media_path)
        media_url = _copy_media_for_lesson(lesson_id, media)
        bundle = build_local_video_lesson(
            media_path,
            transcript_path=transcript_path or None,
            whisper_model=whisper_model,
            output_dir=OUTPUT_DIR,
        )
        _store_media_segments(lesson_id, source_bundle_to_segment_dicts(bundle))
        db.update_v2_lesson_metadata(
            lesson_id,
            title=bundle.title or media.stem,
            media_url=media_url,
            media_kind=_media_kind(media),
        )
        db.set_v2_lesson_status(lesson_id, subtitle_status="ready")
        _enqueue_media_alignment(lesson_id)
        if translate:
            translate_lesson_subtitles(lesson_id)
    except Exception as exc:
        db.set_v2_lesson_status(lesson_id, subtitle_status="failed", subtitle_error=str(exc))
        if translate:
            db.update_v2_translation_status(
                lesson_id, status="failed", ready=False,
                error=f"Subtitle generation failed: {exc}",
            )


def _import_bilibili_media(lesson_id: int, url: str, download_video: bool,
                           whisper_model: str, translate: bool = False) -> None:
    try:
        bundle = build_bilibili_lesson(url, download_video=download_video, whisper_model=whisper_model)
        media_url = ""
        media_kind = "local_video" if download_video else "local_audio"
        if bundle.local_video:
            media_url = _copy_media_for_lesson(lesson_id, Path(bundle.local_video))
            media_kind = _media_kind(Path(bundle.local_video))
        _store_media_segments(lesson_id, source_bundle_to_segment_dicts(bundle))
        db.update_v2_lesson_metadata(
            lesson_id,
            title=bundle.title or "Bilibili lesson",
            media_url=media_url,
            media_kind=media_kind,
        )
        db.set_v2_lesson_status(lesson_id, subtitle_status="ready")
        _enqueue_media_alignment(lesson_id)
        if translate:
            translate_lesson_subtitles(lesson_id)
    except Exception as exc:
        db.set_v2_lesson_status(lesson_id, subtitle_status="failed", subtitle_error=str(exc))
        if translate:
            db.update_v2_translation_status(
                lesson_id, status="failed", ready=False,
                error=f"Subtitle generation failed: {exc}",
            )


def _resolve_local_path(value: str) -> Path:
    raw = str(value or "").strip()
    if raw.lower().startswith("file://"):
        parsed = urlparse(raw)
        return Path(url2pathname(unquote(parsed.path))).expanduser().resolve()
    return Path(raw).expanduser().resolve()


def _media_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _AUDIO_EXTS:
        return "local_audio"
    if suffix in _VIDEO_EXTS:
        return "local_video"
    return "local_video"


def _copy_media_for_lesson(lesson_id: int, source: Path) -> str:
    target_dir = OUTPUT_DIR / "v2_assets" / str(lesson_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    if not target.exists() or target.stat().st_size != source.stat().st_size:
        shutil.copy2(source, target)
    return f"/output/v2_assets/{lesson_id}/{target.name}"


def get_available_modes(lesson: dict) -> list[str]:
    modes: list[str] = []
    if str(lesson.get("video_id") or "").strip() or str(lesson.get("media_url") or "").strip():
        modes.append("listening")
    if "reading_block_count" in lesson:
        has_reading = int(lesson.get("reading_block_count") or 0) > 0
    else:
        has_reading = bool(db.get_v2_reading_blocks(int(lesson["id"])))
    if has_reading:
        modes.append("reading")
    return modes


def get_course_library(include_archived: bool = False) -> list[dict]:
    courses = []
    for lesson in db.list_v2_lessons(include_archived=include_archived):
        lesson_id = int(lesson["id"])
        duration = float(lesson.get("duration") or 0)
        position = float(lesson.get("last_position_seconds") or 0)
        export_name = f"v2-intensive-{lesson_id}.html"
        export_path = Path(OUTPUT_DIR) / export_name
        intensive_ready = export_path.is_file()
        raw_tags = lesson.get("tags") or ""
        try:
            tags = [t for t in json.loads(raw_tags) if isinstance(t, str)] if raw_tags else []
        except (json.JSONDecodeError, TypeError):
            tags = []
        courses.append({
            **lesson,
            "archived": bool(lesson.get("archived")),
            "tags": tags,
            "available_modes": get_available_modes(lesson),
            "progress_percent": min(100, max(0, round(position / duration * 100))) if duration > 0 else 0,
            "last_active_at": (
                lesson.get("progress_updated_at")
                or lesson.get("updated_at")
                or lesson.get("created_at")
                or ""
            ),
            "intensive_ready": intensive_ready,
            "intensive_url": f"/workspace/{lesson_id}/intensive" if intensive_ready else "",
        })
    return courses


def get_lesson_status(lesson_id: int) -> dict:
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise ValueError(f"Lesson {lesson_id} not found")
    progress = db.get_v2_lesson_progress(lesson_id) or {}
    return {"lesson": lesson, "available_modes": get_available_modes(lesson), "progress": {
        "last_position_seconds": progress.get("last_position_seconds", 0),
        "last_segment_index": progress.get("last_segment_index", 0),
    }}
