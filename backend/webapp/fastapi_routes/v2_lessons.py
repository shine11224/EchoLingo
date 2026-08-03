"""V2 lesson routes."""
from __future__ import annotations

import datetime
import json
import shutil
from pathlib import Path
import re

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import db
from prompts import PATTERN_EXTRACTION_PROMPT, PATTERN_SCENARIO_PROMPT
from webapp.runtime import ai_config
from webapp.services import v2_lessons as service
from webapp.storage.lessons import OUTPUT_DIR
from webapp.services.v2_intensive import build_intensive_document
from webapp.services.v2_intensive_export import export_intensive_html
from webapp.services.document_outline import (
    get_document_outline_status,
    start_document_outline_generation,
)
from webapp.services.v2_review_export import export_review_html, synthesize_sentence_audio
from webapp.services.natural_tts import is_current_tts_audio
from webapp.services.v2_vocab import (
    forget_word_meaning_cache,
    highlight_reading_blocks,
    highlight_segments,
    is_word_meaning_placeholder,
    load_exclude_words,
    load_lists_for_keys,
    load_word_meanings,
    lookup_word_meaning,
    remember_word_meaning,
)
from webapp.services.v2_translation import build_translation_units

router = APIRouter(prefix="/api/v2/lessons", tags=["v2-lessons"])


def _highlight_context(lesson_id: int, wordlists: str | None) -> tuple[set[str] | None, list[tuple[str, set[str]]] | None, set[str]]:
    """高亮上下文：source_words=None 表示按默认中频词表；hidden 始终包含 exclude 词表与已掌握词。"""
    hidden = (
        db.get_v2_lesson_hidden_words(lesson_id)
        | db.get_mastered_review_targets()
        | db.get_known_words()
        | load_exclude_words()
    )
    if wordlists is None:
        return None, None, hidden
    keys = [k for k in (part.strip() for part in wordlists.split(",")) if re.fullmatch(r"[a-z0-9_]+", k)]
    source_lists = load_lists_for_keys(keys)
    source_words: set[str] = set()
    for _, list_words in source_lists:
        source_words |= list_words
    return source_words, source_lists, hidden


class StartLessonBody(BaseModel):
    source_type: str
    url: str = ""
    local_path: str = ""
    transcript_path: str = ""
    download_mode: str = "audio"
    bilibili_page: str = ""
    whisper_model: str = "large-v3"
    translate: bool = True
    tts: bool = False
    title: str = ""
    text: str = ""


class ProgressBody(BaseModel):
    last_position_seconds: float = 0
    last_segment_index: int = 0


class PhaseBBody(BaseModel):
    segment_index: int
    start_seconds: float = 0
    end_seconds: float = 0
    text: str


class WordSaveBody(BaseModel):
    word: str
    meaning: str = ""
    sentence: str = ""
    target_type: str = "word"
    lemma: str = ""
    display_text: str = ""
    sentence_key: int | None = None
    mode: str = ""
    source: str = ""


class ReadingSentenceBody(BaseModel):
    block_index: int
    text: str
    start_seconds: float = 0
    end_seconds: float = 0
    mode: str = "reading"
    source: str = ""


class TagBody(BaseModel):
    category: str
    name: str


class SentenceTagsBody(BaseModel):
    tags: list[TagBody] = []


class OutlineSummaryBody(BaseModel):
    force: bool = False


class AlignmentBody(BaseModel):
    force: bool = False


class LessonModeBody(BaseModel):
    mode: str


class LessonLibraryPatchBody(BaseModel):
    title: str | None = None
    archived: bool | None = None
    tags: list[str] | None = None


class SentenceReviewBody(BaseModel):
    rating: str


class SentenceListeningResultBody(BaseModel):
    result: str


class SentenceLibraryPatchBody(BaseModel):
    archived: bool


class SentencePatternPatchBody(BaseModel):
    pattern_template: str


class SentencePatternScenarioBody(BaseModel):
    regenerate: bool = False


def _normalize_word_for_lesson(word: str) -> str:
    return re.sub(r"[^a-zA-Z']", "", word or "").lower().strip("'")


def _sync_highlighted_words_to_lesson(lesson: dict, items: list[tuple[str, str, str]]) -> int:
    lesson_id = int(lesson["id"])
    today = datetime.date.today().isoformat()
    meanings = load_word_meanings()
    existing_words = {
        str(item.get("word") or "").lower()
        for item in db.get_v2_lesson_words(lesson_id)
        if item.get("word")
    }
    hidden_words = db.get_v2_lesson_hidden_words(lesson_id) | db.get_mastered_review_targets()
    seen: set[str] = set()
    synced = 0
    for raw_word, sentence, meaning_hint in items:
        word = _normalize_word_for_lesson(raw_word)
        if not word or word in seen or word in existing_words or word in hidden_words:
            continue
        seen.add(word)
        meaning = str(meaning_hint or meanings.get(word) or "").strip()
        analysis = {"basic_meaning": meaning} if meaning else None
        db.upsert_word(word, today, level="v2", analysis=analysis)
        db.save_v2_lesson_word(lesson_id, word, sentence)
        existing_words.add(word)
        synced += 1
        if meaning:
            remember_word_meaning(word, meaning)
    return synced


@router.post("/start")
def start_lesson(body: StartLessonBody):
    source_type = (body.source_type or "").lower()
    if source_type == "youtube":
        result = service.start_youtube_lesson(url=body.url, translate=True)
        service.enqueue_subtitle_fetch(result["lesson"]["id"], body.url, translate=True)
        return result
    if source_type in {"local", "local_audio", "local_video"}:
        path = body.local_path or body.url
        if not path:
            raise HTTPException(status_code=400, detail="Local media path required")
        try:
            return service.start_local_lesson(
                path,
                transcript_path=body.transcript_path or None,
                whisper_model=body.whisper_model,
                translate=True,
            )
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
    if source_type == "bilibili":
        url = body.url
        if not url:
            raise HTTPException(status_code=400, detail="Bilibili URL required")
        if body.bilibili_page and "&p=" not in url and "?p=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}p={body.bilibili_page}"
        return service.start_bilibili_lesson(
            url,
            download_video=body.download_mode == "video",
            whisper_model=body.whisper_model,
            translate=True,
        )
    if source_type == "reading_text":
        if not body.text.strip():
            raise HTTPException(status_code=400, detail="Reading text is required")
        return service.start_reading_text_lesson(
            title=body.title or "Reading Passage", text=body.text, tts=body.tts
        )
    if source_type == "reading_pdf":
        path = body.local_path or body.url
        if not path:
            raise HTTPException(status_code=400, detail="Reading PDF path required")
        try:
            return service.start_reading_pdf_lesson(path, title=body.title or "", tts=body.tts)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    raise HTTPException(status_code=400, detail=f"Unsupported v2 source type: {body.source_type}")


@router.post("/reading/upload")
async def upload_reading_file(file: UploadFile = File(...), tts: bool = Form(False)):
    filename = file.filename or ""
    try:
        content = await file.read()
        return service.start_reading_upload_lesson(filename, content, tts=tts)
    except service.ReadingUploadBusyError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/reading/upload-status/{job_id}")
def reading_upload_status(job_id: str):
    try:
        return service.get_reading_upload_status(job_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/library")
def course_library(include_archived: bool = False):
    return {"lessons": service.get_course_library(include_archived=include_archived)}


@router.patch("/library/{lesson_id}")
def patch_course_library_item(lesson_id: int, body: LessonLibraryPatchBody):
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="Title cannot be empty")
        db.update_v2_lesson_metadata(lesson_id, title=title[:200])
    if body.archived is not None:
        db.set_v2_lesson_archived(lesson_id, body.archived)
    if body.tags is not None:
        db.set_v2_lesson_tags(lesson_id, body.tags)
    return {"ok": True, "lesson": db.get_v2_lesson(lesson_id)}


@router.delete("/library/{lesson_id}")
def delete_course_library_item(lesson_id: int):
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    if not db.delete_v2_lesson(lesson_id):
        raise HTTPException(status_code=500, detail="Lesson delete failed")
    output_dir = Path(service.OUTPUT_DIR)
    shutil.rmtree(output_dir / "v2_assets" / str(lesson_id), ignore_errors=True)
    shutil.rmtree(output_dir / "v2_exports" / str(lesson_id), ignore_errors=True)
    for export in output_dir.glob(f"v2-intensive-{lesson_id}.html"):
        export.unlink(missing_ok=True)
    return {"ok": True, "deleted": lesson_id}


class RetrySubtitlesBody(BaseModel):
    whisper_model: str = "large-v3"


@router.post("/{lesson_id}/retry-subtitles")
def retry_subtitles(lesson_id: int, body: RetrySubtitlesBody | None = None):
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    if lesson["subtitle_status"] not in {"failed", "ready"}:
        raise HTTPException(status_code=409, detail="Subtitle pipeline is already running")
    model = (body.whisper_model if body else "") or "large-v3"
    translate = bool(lesson.get("translation_requested"))
    db.set_v2_lesson_status(lesson_id, subtitle_status="pending")
    db.clear_v2_lesson_subtitle_error(lesson_id)
    source_type = str(lesson["source_type"])
    if source_type == "bilibili":
        service.enqueue_bilibili_import(
            lesson_id, lesson["source_url"], download_video=False,
            whisper_model=model, translate=translate,
        )
    elif source_type == "youtube":
        service.enqueue_subtitle_fetch(lesson_id, lesson["source_url"], translate=translate)
    elif source_type in {"local", "local_audio", "local_video"}:
        service.enqueue_local_import(
            lesson_id, lesson["source_url"], whisper_model=model, translate=translate,
        )
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported source type: {source_type}")
    return {"ok": True, "whisper_model": model}


@router.get("/sentence-review")
def sentence_review_queue(include_archived: bool = False):
    sentences = db.list_v2_saved_sentences(
        datetime.date.today().isoformat(),
        include_archived=include_archived,
    )
    return {
        "sentences": sentences,
        "total": len(sentences),
        "due_count": sum(1 for sentence in sentences if sentence["is_due"]),
    }


class ManualSentenceBody(BaseModel):
    text: str
    translation: str = ""


@router.post("/sentence-review/manual")
def save_manual_sentence(body: ManualSentenceBody):
    """收藏无课程来源的句子（如 AI 生成句），直接进句子库。"""
    text = " ".join(body.text.split())
    if not text:
        raise HTTPException(status_code=400, detail="句子内容不能为空")
    if len(text) > 2000:
        raise HTTPException(status_code=400, detail="句子过长，无法收藏")
    sentence = db.save_v2_manual_sentence(text, body.translation.strip())
    return {"ok": True, "sentence": sentence}


@router.post("/sentence-review/{sentence_id}")
def review_sentence(sentence_id: int, body: SentenceReviewBody):
    if body.rating not in {"again", "hard", "good"}:
        raise HTTPException(status_code=400, detail="Invalid sentence review rating")
    sentence = db.review_v2_sentence(
        sentence_id,
        body.rating,
        datetime.date.today().isoformat(),
    )
    if not sentence:
        raise HTTPException(status_code=404, detail="Saved sentence not found")
    return {"ok": True, "sentence": sentence}


@router.post("/sentence-review/{sentence_id}/listening-result")
def save_sentence_listening_result(sentence_id: int, body: SentenceListeningResultBody):
    if body.result not in {"understood", "not_understood"}:
        raise HTTPException(status_code=400, detail="Invalid listening result")
    sentence = db.review_v2_sentence(
        sentence_id,
        body.result,
        datetime.date.today().isoformat(),
    )
    if not sentence:
        raise HTTPException(status_code=404, detail="Saved sentence not found")
    return {"ok": True, "sentence": sentence}


@router.patch("/sentence-review/{sentence_id}")
def patch_saved_sentence(sentence_id: int, body: SentenceLibraryPatchBody):
    sentence = db.set_v2_sentence_archived(sentence_id, body.archived)
    if not sentence:
        raise HTTPException(status_code=404, detail="Saved sentence not found")
    return {"ok": True, "sentence": sentence}


def _saved_sentence_or_404(sentence_id: int) -> dict:
    sentence = db.get_v2_sentence_by_id(sentence_id)
    if not sentence:
        raise HTTPException(status_code=404, detail="Saved sentence not found")
    saved_ids = {
        item["id"]
        for item in db.list_v2_saved_sentences(include_archived=True)
    }
    if sentence_id not in saved_ids:
        raise HTTPException(status_code=404, detail="Saved sentence not found")
    return sentence


@router.patch("/sentence-review/{sentence_id}/tags")
def update_saved_sentence_tags(sentence_id: int, body: SentenceTagsBody):
    _saved_sentence_or_404(sentence_id)
    try:
        tags = db.replace_v2_sentence_tags(
            sentence_id,
            [item.model_dump() for item in body.tags],
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"ok": True, "sentence_id": sentence_id, "tags": tags}


def _request_pattern_json(prompt: str) -> dict:
    response = ai_config.client.chat.completions.create(
        model=ai_config.AI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.3,
    )
    result = json.loads(response.choices[0].message.content)
    if not isinstance(result, dict):
        raise ValueError("AI pattern response must be an object")
    return result


def _pattern_response(pattern: dict, *, cached: bool) -> dict:
    return {
        "ok": True,
        "cached": cached,
        "has_pattern": bool(pattern.get("pattern_template")),
        "pattern_template": str(pattern.get("pattern_template") or ""),
        "scenario": str(pattern.get("scenario_cn") or ""),
        "pattern": pattern,
    }


@router.post("/sentence-review/{sentence_id}/pattern")
def create_sentence_pattern(sentence_id: int):
    sentence = _saved_sentence_or_404(sentence_id)
    cached = db.get_v2_sentence_pattern(sentence_id)
    if cached and cached.get("pattern_template"):
        return _pattern_response(cached, cached=True)
    try:
        result = _request_pattern_json(
            PATTERN_EXTRACTION_PROMPT.format(english=sentence["text"])
        )
        pattern_template = str(result.get("pattern_template") or "").strip()
        pattern = db.save_v2_sentence_pattern(sentence_id, pattern_template)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return _pattern_response(pattern, cached=False)


@router.patch("/sentence-review/{sentence_id}/pattern")
def update_sentence_pattern(sentence_id: int, body: SentencePatternPatchBody):
    _saved_sentence_or_404(sentence_id)
    try:
        pattern = db.save_v2_sentence_pattern(sentence_id, body.pattern_template)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return _pattern_response(pattern, cached=False)


@router.post("/sentence-review/{sentence_id}/pattern/scenario")
def create_sentence_pattern_scenario(
    sentence_id: int,
    body: SentencePatternScenarioBody,
):
    sentence = _saved_sentence_or_404(sentence_id)
    pattern = db.get_v2_sentence_pattern(sentence_id)
    has_template = bool(pattern and pattern.get("pattern_template"))
    # 无 AI 句式分析时直接以原句为迁移参考
    template = pattern["pattern_template"] if has_template else sentence["text"]
    if has_template and pattern.get("scenario_cn") and not body.regenerate:
        return _pattern_response(pattern, cached=True)
    try:
        result = _request_pattern_json(
            PATTERN_SCENARIO_PROMPT.format(
                pattern_template=template,
                english=sentence["text"],
            )
        )
        scenario_cn = str(result.get("scenario_cn") or "").strip()
        if has_template:
            pattern = db.save_v2_sentence_pattern_scenario(sentence_id, scenario_cn)
            return _pattern_response(pattern, cached=False)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return {"scenario_cn": scenario_cn, "cached": False, "reference": "original_sentence"}


@router.get("/sentence-audio/{sentence_id}")
def sentence_audio(sentence_id: int):
    """Reading 课收藏句没有原音，用 SAPI 合成 wav 并缓存。"""
    sentence = db.get_v2_sentence_by_id(sentence_id)
    if not sentence:
        raise HTTPException(status_code=404, detail="Sentence not found")
    text = str(sentence.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=404, detail="Sentence has no text")
    audio_dir = OUTPUT_DIR / "v2_sentence_audio"
    audio_path = audio_dir / f"sentence-{sentence_id}.wav"
    if not is_current_tts_audio(audio_path, text):
        synthesize_sentence_audio(text, audio_path)
    if not audio_path.exists():
        raise HTTPException(status_code=502, detail="Sentence audio synthesis failed")
    return FileResponse(audio_path, media_type="audio/wav")


@router.get("/sentence-phonetics/{sentence_id}")
def sentence_phonetics(sentence_id: int):
    """收藏句音标：优先返回已存版本（含 AI 分析结果），否则规则生成并缓存。"""
    sentence = db.get_v2_sentence_by_id(sentence_id)
    if not sentence:
        raise HTTPException(status_code=404, detail="Sentence not found")
    cached = str(sentence.get("phonetics") or "").strip()
    if cached:
        return {
            "phonetics": cached,
            "source": str(sentence.get("phonetics_source") or "rule"),
        }
    text = str(sentence.get("text") or "").strip()
    if not text:
        return {"phonetics": "", "source": "rule"}
    import eng_to_ipa as ipa_lib
    from phonetics_processor import annotate
    from webapp.services import dicts as dict_service

    raw = dict_service.strip_ipa_asterisks(ipa_lib.convert(text))
    natural = annotate(text, raw)
    if natural:
        db.set_v2_sentence_phonetics(sentence_id, natural, source="rule")
    return {"phonetics": natural, "source": "rule"}


@router.get("/{lesson_id}/status")
def lesson_status(lesson_id: int):
    try:
        return service.get_lesson_status(lesson_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.patch("/{lesson_id}/mode")
def update_lesson_mode(lesson_id: int, body: LessonModeBody):
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    available_modes = service.get_available_modes(lesson)
    if body.mode not in available_modes:
        raise HTTPException(status_code=400, detail="Study mode is not available for this lesson")
    db.update_v2_lesson_metadata(lesson_id, lesson_mode=body.mode)
    return {"lesson_mode": body.mode, "available_modes": available_modes}


@router.get("/{lesson_id}/subtitles")
def lesson_subtitles(lesson_id: int, wordlists: str | None = None):
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    segments = db.get_v2_subtitle_segments(lesson_id)
    source_words, source_lists, hidden_words = _highlight_context(lesson_id, wordlists)
    highlighted = highlight_segments(
        segments, hidden_words=hidden_words, source_words=source_words, source_lists=source_lists
    )
    return {
        "lesson_id": lesson_id,
        "subtitle_status": lesson["subtitle_status"],
        "segments": highlighted,
        "sentence_units": build_translation_units(highlighted),
    }


@router.get("/{lesson_id}/reading")
def lesson_reading(lesson_id: int, wordlists: str | None = None):
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    blocks = service.ensure_media_reading_blocks(lesson_id, lesson)
    if not blocks:
        raise HTTPException(status_code=409, detail="Reading content is not ready")
    source_words, _, hidden_words = _highlight_context(lesson_id, wordlists)
    highlighted = highlight_reading_blocks(blocks, hidden_words=hidden_words, source_words=source_words)
    return {"lesson": lesson, "blocks": highlighted["blocks"], "candidate_count": highlighted["candidate_count"]}


@router.post("/{lesson_id}/highlighted-words/sync")
def sync_highlighted_words(lesson_id: int, wordlists: str | None = None):
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    source_words, source_lists, hidden_words = _highlight_context(lesson_id, wordlists)
    if lesson.get("lesson_mode") == "reading":
        blocks = db.get_v2_reading_blocks(lesson_id)
        highlighted = highlight_reading_blocks(blocks, hidden_words=hidden_words, source_words=source_words)
        items = [
            (
                item.get("normalized") or item.get("word") or "",
                str(block.get("text") or ""),
                "",
            )
            for block in highlighted["blocks"]
            for item in block.get("highlights", [])
        ]
    else:
        segments = highlight_segments(
            db.get_v2_subtitle_segments(lesson_id),
            hidden_words=hidden_words,
            source_words=source_words,
            source_lists=source_lists,
        )
        items = [
            (
                word,
                str(segment.get("text") or ""),
                str((segment.get("word_meanings") or {}).get(str(word).lower()) or ""),
            )
            for segment in segments
            for word in segment.get("highlighted_words", [])
        ]
    return {"ok": True, "synced": _sync_highlighted_words_to_lesson(lesson, items)}


@router.post("/{lesson_id}/reading/saved-sentences")
def save_reading_sentence(lesson_id: int, body: ReadingSentenceBody):
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    if not db.get_v2_reading_blocks(lesson_id):
        raise HTTPException(status_code=409, detail="Reading content is not ready")
    saved = db.save_v2_phase_b_sentence(
        lesson_id=lesson_id,
        segment_index=body.block_index,
        start_seconds=body.start_seconds,
        end_seconds=body.end_seconds,
        text=body.text,
    )
    return {"ok": True, "saved": True, "sentence": saved}


@router.get("/{lesson_id}/word-meaning/{word}")
def word_meaning(lesson_id: int, word: str):
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    lookup = lookup_word_meaning(word, allow_external_fallback=True)
    normalized = str(lookup.get("word") or word).strip().lower()
    return {
        **lookup,
        "in_review_book": bool(normalized and db.is_word_in_review(normalized)),
    }


class TranslateSentencesBody(BaseModel):
    sentences: list[str] = []


class TranslateSelectionBody(BaseModel):
    text: str


def _hy_mt_translate(text: str) -> str:
    from webapp.services.hy_translate import is_ready, translate

    if not is_ready():
        raise HTTPException(status_code=503, detail="混元翻译引擎未就绪")
    translation = translate(text)
    if not translation:
        raise HTTPException(status_code=502, detail="混元翻译未返回内容")
    return translation


@router.post("/{lesson_id}/translate-selection")
def translate_selection(lesson_id: int, body: TranslateSelectionBody):
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    text = " ".join(body.text.split())
    if not text:
        raise HTTPException(status_code=400, detail="选区不能为空")
    if len(text) > 4000:
        raise HTTPException(status_code=400, detail="选区不能超过 4000 字符")
    cached = db.get_v2_sentence(text)
    translation = str((cached or {}).get("translation") or "").strip()
    if not translation:
        translation = _hy_mt_translate(text)
        db.upsert_v2_sentence(text, translation=translation)
    return {"translation": translation, "engine": "hy-mt"}


@router.post("/{lesson_id}/translate-sentences")
def translate_sentences(lesson_id: int, body: TranslateSentencesBody):
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    texts = [s.strip() for s in body.sentences if s.strip()]
    if not texts:
        return {"translations": {}}
    results = {}
    for text in texts:
        cached = db.get_v2_sentence(text)
        if cached and cached.get("translation"):
            results[text] = cached["translation"]
    pending = [t for t in texts if t not in results]
    if not pending:
        return {"translations": results}
    for text in pending:
        translation = _hy_mt_translate(text)
        db.upsert_v2_sentence(text, translation=translation)
        results[text] = translation
    return {"translations": results, "engine": "hy-mt"}


@router.get("/{lesson_id}/sentence-translations")
def get_sentence_translations(lesson_id: int):
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    units = build_translation_units(db.get_v2_subtitle_segments(lesson_id))
    seen = set()
    translations = {}
    fully_cached = bool(units)
    for unit in units:
        text = " ".join((unit.get("text") or "").strip().split())
        if not text or text in seen:
            continue
        seen.add(text)
        cached = db.get_v2_sentence(text)
        if cached and cached.get("translation"):
            translations[text] = cached["translation"]
        else:
            fully_cached = False
    if fully_cached and int(lesson.get("translation_requested") or 0) == 1:
        total = len(units)
        duration = float(units[-1].get("end") or 0)
        db.update_v2_translation_status(
            lesson_id,
            status="ready",
            done=total,
            total=total,
            buffer_seconds=duration,
            rate=0,
            ready=True,
            error="",
        )
    return {
        "translations": translations,
        "cached": fully_cached,
        "translation_status": "ready" if fully_cached else lesson.get("translation_status"),
    }


@router.post("/{lesson_id}/progress")
def save_progress(lesson_id: int, body: ProgressBody):
    db.upsert_v2_lesson_progress(lesson_id, body.last_position_seconds, body.last_segment_index)
    return {"ok": True}


@router.get("/{lesson_id}/progress")
def get_progress(lesson_id: int):
    progress = db.get_v2_lesson_progress(lesson_id)
    if not progress:
        return {"last_position_seconds": 0, "last_segment_index": 0}
    return progress


@router.post("/{lesson_id}/phase-b")
def save_phase_b(lesson_id: int, body: PhaseBBody):
    saved = db.save_v2_phase_b_sentence(
        lesson_id=lesson_id,
        segment_index=body.segment_index,
        start_seconds=body.start_seconds,
        end_seconds=body.end_seconds,
        text=body.text,
    )
    return {"ok": True, "saved": True, "sentence": saved}


@router.delete("/{lesson_id}/phase-b/{segment_index}")
def delete_phase_b(lesson_id: int, segment_index: int):
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    deleted = db.delete_v2_phase_b_sentence(lesson_id, segment_index)
    return {"ok": True, "saved": False, "deleted": deleted}


@router.get("/{lesson_id}/phase-b")
def get_phase_b(lesson_id: int):
    sentences = db.get_v2_phase_b_sentences(lesson_id)
    return {"lesson_id": lesson_id, "sentences": sentences}


@router.get("/{lesson_id}/intensive")
def intensive_document(lesson_id: int, wordlists: str | None = None):
    try:
        source_words, _, hidden_words = _highlight_context(lesson_id, wordlists)
        return build_intensive_document(lesson_id, source_words=source_words, extra_hidden=hidden_words)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{lesson_id}/alignment")
def alignment_status(lesson_id: int):
    if not db.get_v2_lesson(lesson_id):
        raise HTTPException(status_code=404, detail="Lesson not found")
    from webapp.services.mfa_alignment import get_alignment_status

    return get_alignment_status(lesson_id)


@router.post("/{lesson_id}/alignment")
def start_alignment(lesson_id: int, body: AlignmentBody | None = None):
    from webapp.services.mfa_alignment import enqueue_lesson_alignment

    try:
        return enqueue_lesson_alignment(lesson_id, force=bool(body and body.force))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{lesson_id}/intensive-export")
def export_intensive(lesson_id: int):
    try:
        return export_intensive_html(lesson_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/{lesson_id}/outline-summary")
def outline_summary(lesson_id: int, body: OutlineSummaryBody | None = None):
    try:
        result = start_document_outline_generation(
            lesson_id,
            force=bool(body and body.force),
        )
        if result.get("status") == "pending":
            return JSONResponse(status_code=202, content=result)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Outline generation failed: {exc}") from exc


@router.get("/{lesson_id}/outline-summary")
def outline_summary_status(lesson_id: int):
    try:
        return get_document_outline_status(lesson_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/sentence-tags")
def sentence_tag_catalog():
    return {
        "categories": [
            {"id": "vocabulary", "label": "词汇"},
            {"id": "pronunciation", "label": "发音"},
            {"id": "structure", "label": "句式"},
            {"id": "expression", "label": "表达"},
            {"id": "practice", "label": "练习"},
        ],
        "tags": db.list_v2_tags(),
    }


@router.post("/sentence-tags")
def create_sentence_tag(body: TagBody):
    try:
        return {"ok": True, "tag": db.upsert_v2_tag(body.category, body.name)}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.post("/{lesson_id}/phase-b/{segment_index}/tags")
def update_phase_b_sentence_tags(lesson_id: int, segment_index: int, body: SentenceTagsBody):
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    sentences = db.get_v2_phase_b_sentences(lesson_id)
    sentence = next((item for item in sentences if int(item["segment_index"]) == int(segment_index)), None)
    if not sentence:
        raise HTTPException(status_code=404, detail="Saved sentence not found")
    try:
        tags = db.replace_v2_sentence_tags(sentence["sentence_id"], [item.model_dump() for item in body.tags])
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "sentence_id": sentence["sentence_id"], "tags": tags}


@router.get("/{lesson_id}/review-export")
def export_review(lesson_id: int):
    try:
        return export_review_html(lesson_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{lesson_id}/word-state/{word}")
def word_state(lesson_id: int, word: str):
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    if " " in word.strip():
        normalized = db.normalize_vocab_target(word, target_type="phrase")
        lookup = {"meaning": ""}
    else:
        lookup = lookup_word_meaning(word)
        normalized = lookup["word"]
    saved = bool(db.get_v2_lesson_word(lesson_id, normalized)) if normalized else False
    return {"word": normalized, "saved": saved, "meaning": lookup.get("meaning", "")}


@router.get("/{lesson_id}/words")
def lesson_words(lesson_id: int):
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    words = []
    meanings = {}
    for item in db.get_v2_lesson_words(lesson_id):
        word = item.get("word", "")
        if not word:
            continue
        words.append(word)
        analysis = item.get("cached_analysis")
        if isinstance(analysis, dict):
            meaning = str(analysis.get("basic_meaning") or "").strip()
            if meaning:
                meanings[word] = meaning
    review_words = db.get_review_word_set()
    return {
        "words": words,
        "meanings": meanings,
        "review_words": sorted(set(words) & review_words),
        "hidden_words": sorted(db.get_v2_lesson_hidden_words(lesson_id)),
    }


@router.post("/{lesson_id}/word")
def save_word(lesson_id: int, body: WordSaveBody):
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    submitted_meaning = str(body.meaning or "").strip()
    if is_word_meaning_placeholder(submitted_meaning):
        submitted_meaning = ""
    target_type = str(body.target_type or "word").strip().lower()
    if target_type == "phrase":
        try:
            word = db.normalize_vocab_target(body.word, target_type="phrase")
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        lemma = word
        meaning = submitted_meaning
    else:
        lookup = lookup_word_meaning(
            body.word,
            allow_external_fallback=not bool(submitted_meaning),
        )
        lemma = str(body.lemma or lookup.get("lemma") or lookup.get("word") or "")
        try:
            word = db.normalize_vocab_target(
                str(lookup.get("word") or body.word),
                target_type="word",
                lemma=lemma,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        meaning = submitted_meaning or lookup.get("meaning", "")
    analysis = {"basic_meaning": meaning} if meaning else None
    today = datetime.date.today().isoformat()
    count, is_new = db.upsert_word(word, today, level="v2", analysis=analysis)
    db.save_v2_lesson_word(lesson_id, word, body.sentence)
    db.activate_word_review(
        word,
        source=body.source if body.source in {
            "reading", "listening", "intensive", "sentence_library",
        } else "manual",
        lesson_id=lesson_id,
        analysis=analysis,
        target_type=target_type,
        lemma=word,
        display_text=body.display_text or body.word,
    )
    remember_word_meaning(word, meaning)
    if body.sentence:
        db.add_context(word, lesson.get("title") or "v2 workspace", body.sentence)
    return {"ok": True, "word": word, "saved": True, "count": count, "is_new": is_new, "meaning": meaning}


@router.delete("/{lesson_id}/word/{word}")
def delete_saved_word(lesson_id: int, word: str):
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    normalized = (
        db.normalize_vocab_target(word, target_type="phrase")
        if " " in word.strip()
        else lookup_word_meaning(word)["word"]
    )
    deleted = db.delete_v2_lesson_word(lesson_id, normalized) if normalized else False
    if normalized:
        db.hide_v2_lesson_word(lesson_id, normalized)
    forget_word_meaning_cache(normalized)
    return {"ok": True, "word": normalized, "saved": False, "deleted": deleted}
