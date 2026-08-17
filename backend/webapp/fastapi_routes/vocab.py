"""Phase 4A/4B — vocabulary book endpoints migrated from Flask."""

from __future__ import annotations

import csv as csv_mod
import datetime
import io
import json
import re
from typing import Any, List, Optional
from urllib.parse import quote

import db

try:
    from webapp.services import planning as planning_service
except ImportError:  # pragma: no cover - 公开库不含规划模块
    planning_service = None
from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, Response
from prompts import STORY_CHAT_SYSTEM_PROMPT, STORY_PROMPT
from pydantic import BaseModel
from webapp.runtime import ai_config
from webapp.runtime import credit_meter
from webapp.services.v2_vocab import lookup_word_meaning
from webapp.storage import user_assets
from webapp.storage.lessons import OUTPUT_DIR, extract_js_var

router = APIRouter()


def _billing_error_response(exc: Exception) -> JSONResponse:
    """计费异常统一映射：402 不足（结构化载荷）/409 语义冲突/400 缺 key。"""
    status, detail = credit_meter.billing_error(exc)
    if isinstance(detail, dict):
        return JSONResponse({"error": detail.get("code", "billing_error"),
                             "error_info": detail}, status_code=status)
    return JSONResponse({"error": str(detail)}, status_code=status)


def _begin(request: Request, operation_type: str, **kwargs):
    try:
        return credit_meter.begin_sync_operation(request, operation_type, **kwargs), None
    except (credit_meter.InsufficientCredits,
            credit_meter.OperationConflictError, ValueError) as exc:
        return None, _billing_error_response(exc)

# GET /vocab is a compatibility redirect to the homepage Vocabulary Workshop.
# stays in Flask until a shared Jinja2 setup is added in a later phase.


class LogWordBody(BaseModel):
    word: Optional[str] = None
    level: str = ""
    analysis: Optional[Any] = None
    lesson_title: str = ""
    sentence: str = ""


class ReviewWordBody(BaseModel):
    word: Optional[str] = None


class ActivateReviewWordBody(BaseModel):
    word: Optional[str] = None
    target: Optional[str] = None
    source: str = "manual"
    lesson_id: Optional[int] = None
    meaning: str = ""
    target_type: str = "word"
    lemma: str = ""
    display_text: str = ""


class ReviewWordFamiliarityBody(BaseModel):
    familiarity: str


class ReviewWordLifecycleBody(BaseModel):
    archived: Optional[bool] = None
    mastered: Optional[bool] = None


class ReviewWordTagsBody(BaseModel):
    tags: List[str] = []


@router.get("/vocab-log")
def get_vocab_log(include_archived: bool = False, include_mastered: bool = False):
    words = db.get_review_words(
        include_archived=include_archived,
        include_mastered=include_mastered,
    )
    return _attach_vocab_context_audio(words)


def _context_text_key(text: str) -> str:
    return " ".join(str(text or "").lower().split()).strip(" .!?\"'“”‘’")


def _matching_context_item(items: list[dict], sentence: str) -> dict | None:
    target = _context_text_key(sentence)
    if not target:
        return None
    for item in items:
        if _context_text_key(item.get("text", "")) != target:
            continue
        return item
    return None


def _item_time_range(item: dict) -> tuple[float, float] | None:
    start = float(item.get("start_seconds", item.get("start", 0)) or 0)
    end = float(item.get("end_seconds", item.get("end", 0)) or 0)
    if end > start:
        return start, end
    return None


def _legacy_lesson_audio(meta: dict, sentence: str, cache: dict) -> dict | None:
    filename = str(meta.get("filename") or "")
    if not filename:
        return None
    if filename not in cache:
        html_path = user_assets.current_output_root(OUTPUT_DIR) / filename
        try:
            raw = html_path.read_text(encoding="utf-8")
            source_match = re.search(r'<source\s+[^>]*src=["\']([^"\']+)["\']', raw, re.I)
            cache[filename] = {
                "segments": extract_js_var(raw, "segments") or [],
                "source_type": extract_js_var(raw, "sourceType") or meta.get("source_type") or "",
                "video_id": extract_js_var(raw, "youtubeId") or "",
                "media_url": (
                    "/output/" + quote(source_match.group(1).replace("\\", "/"), safe="/")
                    if source_match else ""
                ),
            }
        except (OSError, UnicodeError):
            cache[filename] = {}
    payload = cache[filename]
    item = _matching_context_item(payload.get("segments") or [], sentence)
    time_range = _item_time_range(item) if item else None
    if not time_range:
        return None
    start, end = time_range
    if payload.get("source_type") == "youtube" and payload.get("video_id"):
        return {"kind": "youtube", "video_id": payload["video_id"], "start": start, "end": end}
    if payload.get("media_url"):
        return {"kind": "media", "url": payload["media_url"], "start": start, "end": end}
    return None


def _v2_lesson_sentence_item(lesson: dict, sentence: str, cache: dict) -> dict | None:
    """按原文匹配课内句子行；阅读课的块内句补齐 segment_index（阅读 key 语义）。"""
    lesson_id = int(lesson["id"])
    if lesson_id not in cache:
        ranges = db.get_v2_phase_b_sentences(lesson_id)
        if not ranges:
            from webapp.services.v2_intensive import reading_sentence_key
            ranges = [
                {
                    **sentence_item,
                    "segment_index": sentence_item.get("segment_index")
                        if sentence_item.get("segment_index") is not None
                        else reading_sentence_key(int(block.get("index", 0)), sentence_index),
                }
                for block in db.get_v2_reading_blocks(lesson_id)
                for sentence_index, sentence_item in enumerate(block.get("sentences") or [])
            ]
        cache[lesson_id] = ranges
    return _matching_context_item(cache[lesson_id], sentence)


def _v2_lesson_audio(lesson: dict, item: dict | None) -> dict | None:
    if item is None:
        return None
    source_type = str(lesson.get("source_type") or "")
    if source_type == "reading_text":
        return None
    time_range = _item_time_range(item)
    if not time_range:
        return None
    start, end = time_range
    if source_type == "youtube" and lesson.get("video_id"):
        return {"kind": "youtube", "video_id": lesson["video_id"], "start": start, "end": end}
    if lesson.get("media_url"):
        return {"kind": "media", "url": lesson["media_url"], "start": start, "end": end}
    return None


def _attach_vocab_context_audio(words: dict) -> dict:
    legacy_by_title = {item.get("title"): item for item in db.get_lessons(include_archived=True)}
    v2_by_title = {item.get("title"): item for item in db.list_v2_lessons(include_archived=True)}
    legacy_cache: dict = {}
    v2_cache: dict = {}
    translation_cache: dict = {}
    for entry in words.values():
        for context in entry.get("contexts") or []:
            title = context.get("lesson")
            sentence = context.get("sentence") or ""
            text_key = _context_text_key(sentence)
            if text_key and text_key not in translation_cache:
                row = db.get_v2_sentence(sentence)
                translation_cache[text_key] = str((row or {}).get("translation") or "").strip()
            if translation_cache.get(text_key):
                context["translation"] = translation_cache[text_key]
            audio = None
            if title in v2_by_title:
                lesson = v2_by_title[title]
                item = _v2_lesson_sentence_item(lesson, sentence, v2_cache)
                if item is not None:
                    context["lesson_id"] = int(lesson["id"])
                    if item.get("segment_index") is not None:
                        context["segment_index"] = int(item["segment_index"])
                audio = _v2_lesson_audio(lesson, item)
            if not audio and title in legacy_by_title:
                audio = _legacy_lesson_audio(legacy_by_title[title], sentence, legacy_cache)
            if audio:
                context["audio"] = audio
    return words


@router.get("/api/active-words")
def api_active_words():
    words = db.get_review_words(include_archived=True)
    active = [w for w in words if not w.startswith("__")]
    meanings = {}
    for word in active:
        analysis = words.get(word, {}).get("cached_analysis")
        if isinstance(analysis, dict):
            meaning = str(analysis.get("basic_meaning") or "").strip()
            if meaning:
                meanings[word] = meaning
    return {"words": active, "meanings": meanings}


@router.post("/log-word")
def log_word(body: Optional[LogWordBody] = Body(default=None)):
    data = body or LogWordBody()
    word = (data.word or "").strip().lower()
    if not word:
        return JSONResponse({"error": "word required"}, status_code=400)

    today = datetime.date.today().isoformat()
    new_count, is_new = db.upsert_word(
        word, today,
        level=data.level,
        analysis=data.analysis,
    )

    if data.lesson_title or data.sentence:
        db.add_context(word, data.lesson_title, data.sentence)

    db.activate_word_review(
        word,
        source="manual",
        analysis=data.analysis if isinstance(data.analysis, dict) else None,
    )
    return {"word": word, "count": new_count, "is_new": is_new}


@router.post("/api/vocab-review/activate")
def activate_review_word(body: Optional[ActivateReviewWordBody] = Body(default=None)):
    data = body or ActivateReviewWordBody()
    word = (data.word or data.target or "").strip()
    if not word:
        return JSONResponse({"error": "word required"}, status_code=400)
    if data.source in {"lookup", "practice"}:
        return JSONResponse(
            {"error": "explicit collection source required"},
            status_code=400,
        )
    source = data.source if data.source in {
        "manual", "reading", "listening", "intensive", "sentence_library", "story",
    } else "manual"
    analysis = {"basic_meaning": data.meaning.strip()} if data.meaning.strip() else None
    try:
        item = db.activate_word_review(
            word,
            source=source,
            lesson_id=data.lesson_id,
            analysis=analysis,
            target_type=data.target_type,
            lemma=data.lemma,
            display_text=data.display_text,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {**item, "in_review_book": True}


@router.patch("/api/vocab-review/{target}/familiarity")
def set_review_familiarity(target: str, body: ReviewWordFamiliarityBody):
    try:
        item = db.set_review_word_familiarity(target, body.familiarity)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not item:
        return JSONResponse({"error": "review target not found"}, status_code=404)
    if planning_service is not None:
        try:
            planning_service.record_verified_event(
                "review_vocabulary", item.get("target_type") or "word", target,
                evidence_ref=f"word-familiarity:{target}:{datetime.date.today().isoformat()}",
            )
        except Exception:
            pass
    return item


@router.patch("/api/vocab-review/{target}/lifecycle")
def set_review_lifecycle(target: str, body: ReviewWordLifecycleBody):
    if body.archived is None and body.mastered is None:
        return JSONResponse({"error": "lifecycle change required"}, status_code=400)
    item = db.set_review_word_lifecycle(
        target,
        archived=body.archived,
        mastered=body.mastered,
    )
    if not item:
        return JSONResponse({"error": "review target not found"}, status_code=404)
    return item


@router.patch("/api/vocab-review/{target}/tags")
def set_review_tags(target: str, body: ReviewWordTagsBody):
    tags = db.set_word_review_tags(target, body.tags)
    if tags is None:
        return JSONResponse({"error": "review target not found"}, status_code=404)
    return {"word": target.strip().lower(), "tags": tags}


@router.post("/api/review-word")
def review_word(body: Optional[ReviewWordBody] = Body(default=None)):
    data = body or ReviewWordBody()
    word = (data.word or "").strip().lower()
    if not word:
        return JSONResponse({"error": "word required"}, status_code=400)
    today = datetime.date.today().isoformat()
    new_count = db.review_word(word, today)
    if new_count is None:
        return JSONResponse({"error": "word not found"}, status_code=404)
    if planning_service is not None:
        try:
            review_item = db.get_review_word_item(word) or {}
            planning_service.record_verified_event(
                "review_vocabulary", review_item.get("target_type") or "word", word,
                evidence_ref=f"word-review:{word}:{today}",
            )
        except Exception:
            pass
    return {"word": word, "count": new_count, "last_studied": today}


# ── Phase 4B ──────────────────────────────────────────────────────────────


class VocabStoryBody(BaseModel):
    words: Optional[List[str]] = None
    learner_level: str = "B1"
    theme: str = ""
    force_new: bool = False


@router.get("/api/vocab-story/history")
def vocab_story_history(page: int = 1, page_size: int = 9, limit: Optional[int] = None):
    requested_size = limit if limit is not None else page_size
    size = max(1, min(int(requested_size or 9), 100))
    total = db.count_story_history()
    pages = max(1, (total + size - 1) // size)
    current_page = max(1, min(int(page or 1), pages))
    stories = []
    for row in db.list_story_history(limit=size, offset=(current_page - 1) * size):
        try:
            words = json.loads(row.get("words_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            words = []
        try:
            parsed = json.loads(row.get("story") or "{}")
        except (json.JSONDecodeError, TypeError):
            parsed = {"story_content": row.get("story") or ""}
        story_content = str(parsed.get("story_content") or parsed.get("story") or "").strip()
        if not story_content:
            continue
        stories.append({
            "id": row["id"],
            "date": row.get("date") or "",
            "created_at": row.get("created_at") or "",
            "learner_level": row.get("learner_level") or "",
            "theme": row.get("theme") or "",
            "words": words if isinstance(words, list) else [],
            "story": story_content,
            "used_words": parsed.get("used_words") if isinstance(parsed.get("used_words"), list) else [],
            "review_questions": parsed.get("review_questions") if isinstance(parsed.get("review_questions"), list) else [],
        })
    return {
        "stories": stories,
        "total": total,
        "page": current_page,
        "page_size": size,
        "pages": pages,
    }


@router.delete("/api/vocab-story/history/{story_id}")
def delete_vocab_story_history(story_id: int):
    if not db.delete_story_history(story_id):
        return JSONResponse({"error": "story not found"}, status_code=404)
    return {"id": story_id, "deleted": True}


@router.post("/api/vocab-story")
def vocab_story(body: Optional[VocabStoryBody] = Body(default=None), request: Request = None):
    data = body or VocabStoryBody()
    words = list(dict.fromkeys(
        w.strip().lower() for w in (data.words or []) if w.strip()
    ))[:20]
    if not words:
        return JSONResponse({"error": "no words"}, status_code=400)

    today = datetime.date.today().isoformat()
    cache_key = today + "|" + ",".join(sorted(words))

    # cache-before-reserve：当日同词故事直接免费返回，不产生 operation
    cached = db.get_story(cache_key)
    if cached and not data.force_new:
        try:
            parsed = json.loads(cached)
            return {"story": parsed.get("story_content", cached), "used_words": parsed.get("used_words", []), "review_questions": parsed.get("review_questions", []), "credits": {"charged": 0, "cached": True}}
        except (json.JSONDecodeError, TypeError):
            return {"story": cached, "credits": {"charged": 0, "cached": True}}

    begun, error = _begin(request, "vocab_story", quantity=1)
    if error:
        return error
    (op, replay) = begun
    if replay is not None:
        return replay

    all_words = db.get_all_words()
    details = []
    for w in words:
        entry = all_words.get(w, {})
        meaning = (entry.get("cached_analysis") or {}).get("basic_meaning", "")
        details.append(f"- {w}: {meaning}" if meaning else f"- {w}")

    prompt = STORY_PROMPT.format(
        word_list="\n".join(details),
        learner_level=data.learner_level,
        theme=data.theme or "（请根据词汇含义自动选择）",
    )

    story_model = (
        "deepseek-chat"
        if "api.deepseek.com" in str(ai_config.AI_BASE_URL or "").lower()
        else ai_config.AI_MODEL
    )
    try:
        resp = ai_config.client.chat.completions.create(
            model=story_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=2500,
            timeout=90,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        # AI/网络异常：释放预授权
        credit_meter.release_sync(op, reason=f"vocab_story request failed: {exc}"[:500])
        return JSONResponse({"error": f"AI 请求失败：{exc}"}, status_code=502)
    choice = resp.choices[0]
    finish_reason = str(getattr(choice, "finish_reason", "") or "").strip().lower()
    raw = str(choice.message.content or "").strip()
    if not raw:
        credit_meter.release_sync(op, reason=f"vocab_story empty response (finish={finish_reason})")
        if finish_reason == "length":
            return JSONResponse(
                {"error": "AI 输出被截断，请重试生成"},
                status_code=502,
            )
        return JSONResponse({"error": "AI 未返回故事内容"}, status_code=502)
    # Strip markdown code fences if present
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(l for l in lines if not l.strip().startswith("```")).strip()

    story_content = used_words = review_questions = None
    try:
        parsed = json.loads(raw)
        story_content = parsed.get("story_content") or parsed.get("story") or ""
        used_words = parsed.get("used_words", [])
        review_questions = parsed.get("review_questions", [])
    except (json.JSONDecodeError, TypeError):
        # 格式校验失败：同样释放，不扣分
        credit_meter.release_sync(op, reason=f"vocab_story invalid JSON (finish={finish_reason})")
        if finish_reason == "length":
            return JSONResponse(
                {"error": "AI 输出被截断，请重试生成"},
                status_code=502,
            )
        return JSONResponse({"error": "AI 返回格式异常，请重试"}, status_code=502)

    if not story_content:
        credit_meter.release_sync(op, reason="vocab_story missing story_content field")
        return JSONResponse({"error": "AI 未返回有效故事字段，请重试"}, status_code=502)

    parsed["story_content"] = story_content
    db.save_story(cache_key, words, json.dumps(parsed, ensure_ascii=False), today)
    db.save_story_history(
        cache_key,
        words,
        json.dumps(parsed, ensure_ascii=False),
        today,
        learner_level=data.learner_level,
        theme=data.theme,
    )
    payload = {"story": story_content, "used_words": used_words or [],
               "review_questions": review_questions or []}
    credit_meter.settle_sync(op, actual_usage=credit_meter.usage_from_response(
        resp, model=story_model, extra={"words": len(words), "force_new": data.force_new}),
        response=payload)
    return payload


# ── Phase 4C ──────────────────────────────────────────────────────────────


class StoryContextBody(BaseModel):
    used_words: Optional[List[dict]] = None


@router.post("/api/story-context")
def save_story_context(body: Optional[StoryContextBody] = Body(default=None)):
    data = body or StoryContextBody()
    for item in (data.used_words or []):
        word = (item.get("target_word") or "").strip().lower()
        sentence = (item.get("sentence") or "").strip()
        if word and sentence:
            db.add_context(word, "故事语境", sentence)
    return {"ok": True}


class StorySelectionBody(BaseModel):
    text: str = ""


class StoryChatBody(BaseModel):
    story: str = ""
    selection: str = ""
    message: str = ""
    history: List[dict] = []


@router.get("/api/story-word-meaning/{word}")
def story_word_meaning(word: str):
    lookup = lookup_word_meaning(word, allow_external_fallback=True)
    normalized = str(lookup.get("word") or word).strip().lower()
    return {
        **lookup,
        "in_review_book": bool(normalized and db.is_word_in_review(normalized)),
    }


def _translate_story_selection(text: str) -> str:
    from webapp.services.hy_translate import is_ready, translate

    if not is_ready():
        raise RuntimeError("混元翻译引擎未就绪")
    translated = str(translate(text) or "").strip()
    if not translated:
        raise RuntimeError("混元翻译未返回内容")
    return translated


@router.post("/api/story-translate")
def translate_story_selection(body: StorySelectionBody, request: Request = None):
    text = " ".join(body.text.split())
    if not text:
        return JSONResponse({"error": "选区不能为空"}, status_code=400)
    if len(text) > 4000:
        return JSONResponse({"error": "选区不能超过 4000 字符"}, status_code=400)
    # cache-before-reserve：已缓存翻译免费返回
    cached = db.get_v2_sentence(text)
    translation = str((cached or {}).get("translation") or "").strip()
    if translation:
        return {"translation": translation, "engine": "hy-mt",
                "credits": {"charged": 0, "cached": True}}
    begun, error = _begin(request, "story_translation")
    if error:
        return error
    (op, replay) = begun
    if replay is not None:
        return replay
    try:
        translation = _translate_story_selection(text)
    except RuntimeError as exc:
        credit_meter.release_sync(op, reason=f"story_translation failed: {exc}"[:500])
        return JSONResponse({"error": str(exc)}, status_code=503)
    db.upsert_v2_sentence(text, translation=translation)
    credit_meter.settle_sync(op, actual_usage={
        "engine": "hy-mt", "characters": len(text)},
        response={"translation": translation, "engine": "hy-mt"})
    return {"translation": translation, "engine": "hy-mt"}


@router.post("/api/story-chat")
def story_chat(body: StoryChatBody, request: Request = None):
    story = " ".join(body.story.split())[:8000]
    selection = " ".join(body.selection.split())[:2000]
    message = " ".join(body.message.split())[:1000]
    if not message:
        return JSONResponse({"error": "问题不能为空"}, status_code=400)
    if not story:
        return JSONResponse({"error": "请先生成故事"}, status_code=400)

    begun, error = _begin(request, "story_chat")
    if error:
        return error
    (op, replay) = begun
    if replay is not None:
        return replay

    messages = [{"role": "system", "content": STORY_CHAT_SYSTEM_PROMPT}]
    context = f"故事原文：\n{story}"
    if selection:
        context += f"\n\n当前选区：\n{selection}"
    messages.append({"role": "system", "content": context})
    for item in body.history[-8:]:
        role = str(item.get("role") or "").strip().lower()
        content = " ".join(str(item.get("content") or "").split())[:2000]
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})

    chat_model = (
        "deepseek-chat"
        if "api.deepseek.com" in str(ai_config.AI_BASE_URL or "").lower()
        else ai_config.AI_MODEL
    )
    try:
        resp = ai_config.client.chat.completions.create(
            model=chat_model,
            messages=messages,
            temperature=0.4,
            max_tokens=1000,
            timeout=60,
        )
        answer = str(resp.choices[0].message.content or "").strip()
    except Exception as exc:
        credit_meter.release_sync(op, reason=f"story_chat failed: {exc}"[:500])
        return JSONResponse({"error": str(exc)}, status_code=502)
    if not answer:
        credit_meter.release_sync(op, reason="story_chat empty response")
        return JSONResponse({"error": "AI 未返回内容，请重试"}, status_code=502)
    credit_meter.settle_sync(op, actual_usage=credit_meter.usage_from_response(
        resp, model=chat_model), response={"answer": answer})
    return {"answer": answer}


class AddKnownWordBody(BaseModel):
    word: Optional[str] = None


@router.get("/api/known-words")
def get_known_words():
    return sorted(db.get_known_words())


@router.post("/api/known-words")
def add_known_word(body: Optional[AddKnownWordBody] = Body(default=None)):
    data = body or AddKnownWordBody()
    word = (data.word or "").strip().lower()
    if not word:
        return JSONResponse({"error": "word required"}, status_code=400)
    today = datetime.date.today().isoformat()
    db.add_known_word(word, today)
    return {"word": word, "known": True}


@router.delete("/api/known-words/{word}")
def remove_known_word(word: str):
    db.remove_known_word(word.lower())
    return {"word": word.lower(), "known": False}


@router.delete("/api/vocab-words/{word}")
def delete_vocab_word(word: str):
    deleted = db.delete_word(word.lower())
    if not deleted:
        return JSONResponse({"error": "word not found"}, status_code=404)
    return {"word": word.lower(), "deleted": True}


@router.patch("/api/vocab-words/{word}/next-review")
def set_word_next_review(word: str, body: dict):
    date_str = body.get("date", "")
    if date_str:
        import re
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
            return JSONResponse({"error": "invalid date format"}, status_code=400)
    updated = db.set_next_review(word.lower(), date_str)
    if not updated:
        return JSONResponse({"error": "word not found"}, status_code=404)
    return {"word": word.lower(), "next_review": date_str}


@router.get("/api/export/vocab")
def api_export_vocab(format: str = "csv"):
    fmt = format.lower()
    with db._db() as conn:
        words = conn.execute("SELECT * FROM words ORDER BY last_studied DESC").fetchall()
        ctx_map: dict = {}
        for row in conn.execute("SELECT word, sentence FROM contexts ORDER BY id"):
            if row["word"] not in ctx_map:
                ctx_map[row["word"]] = row["sentence"]

    if fmt == "anki":
        lines = ["#separator:tab", "#html:false", "#deck:EchoLingo - Vocab"]
        for w in words:
            analysis = json.loads(w["cached_analysis"]) if w["cached_analysis"] else None
            vocab_items = analysis.get("vocabulary", []) if isinstance(analysis, dict) else (analysis or [])
            first = next((v for v in vocab_items if v.get("word")), None) if vocab_items else None
            ipa = first.get("ipa", "") if first else ""
            meaning = first.get("meaning", "") if first else ""
            back_parts = [p for p in [w["level"], ipa, meaning] if p]
            back = " | ".join(back_parts) if back_parts else "见课程上下文"
            context = ctx_map.get(w["word"], "")
            if context:
                back += f"<br>{context}"
            lines.append(f"{w['word']}\t{back}")
        content = "\n".join(lines)
        return Response(
            content,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=vocab_anki.txt"},
        )

    # default: csv
    output = io.StringIO()
    writer = csv_mod.writer(output)
    writer.writerow(["word", "level", "count", "first_studied", "last_studied", "meaning", "lesson_context"])
    for w in words:
        analysis = json.loads(w["cached_analysis"]) if w["cached_analysis"] else None
        vocab_items = analysis.get("vocabulary", []) if isinstance(analysis, dict) else (analysis or [])
        first = next((v for v in vocab_items if v.get("word")), None) if vocab_items else None
        meaning = first.get("meaning", "") if first else ""
        context = ctx_map.get(w["word"], "")
        writer.writerow([w["word"], w["level"], w["count"],
                         w["first_studied"], w["last_studied"], meaning, context])
    content = output.getvalue()
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=vocab.csv"},
    )
