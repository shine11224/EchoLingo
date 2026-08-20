"""V2 lesson routes."""
from __future__ import annotations

import datetime
import json
import shutil
import typing
from pathlib import Path
import re

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import db
from prompts import LESSON_AI_RECOMMENDATION_PROMPT, PATTERN_EXTRACTION_PROMPT, PATTERN_SCENARIO_PROMPT
from webapp.runtime import ai_config
from webapp.runtime import credit_meter
from webapp.runtime.access import is_admin_request, multiuser_enabled, require_admin

try:  # 公开库无 webapp.auth 时静默降级（积分路径整体 no-op）
    from webapp.auth.credits import InsufficientCredits
except ImportError:  # pragma: no cover
    class InsufficientCredits(Exception):
        pass
from webapp.services import dicts as dict_service
from webapp.services import v2_lessons as service
from webapp.storage import user_assets
from webapp.storage.lessons import OUTPUT_DIR
from webapp.services.v2_intensive import build_intensive_document
from webapp.services.v2_intensive_export import export_intensive_html
from webapp.services.document_outline import (
    get_document_outline_status,
    start_document_outline_generation,
)
from webapp.services.v2_review_export import export_review_html, synthesize_sentence_audio
from webapp.services.natural_tts import is_current_tts_audio
from webapp.services.v2_tts import cancel_reading_tts
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
from webapp.services import baidu_pan

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
    upload_id: str = ""
    transcript_path: str = ""
    download_mode: str = "audio"
    bilibili_page: str = ""
    whisper_model: str = "large-v3"
    translate: bool = True
    tts: bool = False
    title: str = ""
    text: str = ""


class BaiduPanImportBody(BaseModel):
    share_link: str
    pwd: str = ""


class BaiduPanDriveImportBody(BaseModel):
    file_id: str
    name: str
    path: str
    size: int
    mtime: str = ""


class BaiduPanPasswordBody(BaseModel):
    pwd: str


class BaiduPanRetryBody(BaseModel):
    pwd: str = ""


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
    # 前端会把 source 作为对象上送（lesson_id/mode/block_index 等上下文）；
    # 后端当前不消费该字段，Any 兼容对象与字符串两种形态（2026-08-07 422 修复）
    source: typing.Any = ""


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


def _require_reading_file_path_allowed(request: Request, path: str) -> None:
    """reading_file 路径守卫：单用户/管理员放行任意本地路径；
    多用户普通用户仅允许读取本人 uploads 目录内文件（网盘导入产物落点），
    防任意服务器文件读取。"""
    if not multiuser_enabled() or is_admin_request(request):
        return
    try:
        candidate = Path(path).resolve()
        uploads_root = user_assets.current_uploads_root().resolve()
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(status_code=403, detail="仅允许导入本人 uploads 目录内的文件")
    if candidate != uploads_root and uploads_root not in candidate.parents:
        raise HTTPException(
            status_code=403,
            detail="仅允许导入本人 uploads 目录内的文件（如网盘导入产物）")


@router.post("/start")
def start_lesson(body: StartLessonBody, request: Request):
    source_type = (body.source_type or "").lower()
    # 平台链接与服务器本地路径建课仅管理员可用（多用户模式非管理员 404）；
    # 普通用户走浏览器上传（uploaded_media）或文本课程。
    # reading_file 不在此列：普通用户可导入本人 uploads 目录内文件（如网盘导入
    # 产物），由 _require_reading_file_path_allowed 做目录包含校验。
    if source_type in {"youtube", "bilibili", "local", "local_audio", "local_video", "reading_pdf"}:
        require_admin(request)
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
        try:
            return service.start_reading_text_lesson(
                title=body.title or "Reading Passage", text=body.text, tts=body.tts,
                username=str(request.scope.get("elt_username") or ""),
                idempotency_key=request.headers.get("Idempotency-Key", ""),
            )
        except InsufficientCredits as e:
            raise HTTPException(status_code=402,
                                detail=credit_meter.insufficient_payload(e))
        except credit_meter.OperationConflictError as e:
            raise HTTPException(status_code=409, detail=e.detail or str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    if source_type == "reading_pdf":
        path = body.local_path or body.url
        if not path:
            raise HTTPException(status_code=400, detail="Reading PDF path required")
        try:
            return service.start_reading_pdf_lesson(
                path, title=body.title or "", tts=body.tts,
                username=str(request.scope.get("elt_username") or ""),
                idempotency_key=request.headers.get("Idempotency-Key", ""),
            )
        except InsufficientCredits as e:
            raise HTTPException(status_code=402,
                                detail=credit_meter.insufficient_payload(e))
        except credit_meter.OperationConflictError as e:
            raise HTTPException(status_code=409, detail=e.detail or str(e))
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    if source_type == "reading_file":
        # 服务器本地路径的 txt/md/docx/pdf（如网盘导入产物）→ 复用上传解析管线
        path = body.local_path or body.url
        if not path:
            raise HTTPException(status_code=400, detail="Reading file path required")
        _require_reading_file_path_allowed(request, path)
        try:
            return service.start_reading_file_lesson(
                path, tts=body.tts,
                username=str(request.scope.get("elt_username") or ""),
                idempotency_key=request.headers.get("Idempotency-Key", ""),
            )
        except InsufficientCredits as e:
            raise HTTPException(status_code=402,
                                detail=credit_meter.insufficient_payload(e))
        except credit_meter.OperationConflictError as e:
            raise HTTPException(status_code=409, detail=e.detail or str(e))
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except service.ReadingUploadBusyError as e:
            raise HTTPException(status_code=503, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    if source_type == "uploaded_media":
        # 普通用户浏览器上传建课：只认 upload_id，忽略任何 local_path/url 字段；
        # 上传记录在当前用户 DB，跨用户 upload_id 自然 404。
        if not body.upload_id.strip():
            raise HTTPException(status_code=400, detail="upload_id required")
        try:
            if credit_meter.mode() in ("shadow", "enforce"):
                # 多用户计费链路（Task 7）：服务端按 upload 时长报价 →
                # reserve（enforce 原子占位）→ 消费/建课/后台管线 settle/release。
                # Idempotency-Key 重放返回已有课程，不重复建课、不重复扣费。
                return service.start_uploaded_media_lesson_billed(
                    body.upload_id.strip(),
                    whisper_model=body.whisper_model,
                    translate=True,
                    username=str(request.scope.get("elt_username") or ""),
                    idempotency_key=request.headers.get("Idempotency-Key", ""),
                )
            return service.start_uploaded_media_lesson(
                body.upload_id.strip(),
                whisper_model=body.whisper_model,
                translate=True,
            )
        except InsufficientCredits as e:
            raise HTTPException(status_code=402,
                                detail=credit_meter.insufficient_payload(e))
        except credit_meter.OperationConflictError as e:
            # 幂等语义冲突（跨操作类型/跨 upload 复用、并发败者）：显式 409
            raise HTTPException(status_code=409, detail=e.detail or str(e))
        except service.MissingIdempotencyKeyError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except service.CreditOperationReleasedError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
    raise HTTPException(status_code=400, detail=f"Unsupported v2 source type: {body.source_type}")


@router.post("/media-uploads")
def create_media_upload(file: UploadFile = File(...)):
    """普通用户浏览器音视频分块上传：大小限制 + 扩展名/ffprobe 双重校验。

    同步 def：FastAPI 放入线程池执行，stream.read/磁盘写/ffprobe 不阻塞事件循环。
    """
    try:
        return service.save_media_upload(file.filename or "", file.file)
    except service.MediaUploadTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except service.MediaUploadError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/media-uploads/{upload_id}")
def delete_media_upload(upload_id: str):
    try:
        service.delete_media_upload(upload_id)
        return {"ok": True}
    except service.MediaUploadError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


def _baidu_identity(request: Request) -> tuple[str, bool]:
    return (
        str(request.scope.get("elt_username") or ""),
        is_admin_request(request) or not multiuser_enabled(),
    )


@router.get("/baidu-pan/capability")
def baidu_pan_capability(request: Request, refresh: bool = False):
    data = baidu_pan.capability(refresh=refresh) if refresh else baidu_pan.capability()
    _username, privileged = _baidu_identity(request)
    data["can_browse"] = bool(privileged and data.get("enabled"))
    data["can_manage_auth"] = bool(multiuser_enabled() and is_admin_request(request))
    data["max_bytes"] = baidu_pan._max_bytes()
    return data


@router.post("/baidu-pan/imports")
def create_baidu_pan_import(body: BaiduPanImportBody, request: Request):
    try:
        username, privileged = _baidu_identity(request)
        return baidu_pan.start_import(
            body.share_link, body.pwd,
            username=username, is_admin=privileged,
        )
    except baidu_pan.BaiduPanBusyError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except baidu_pan.BaiduPanError as e:
        raise HTTPException(status_code=400, detail=baidu_pan.friendly_message(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/baidu-pan/imports/{import_id}")
def baidu_pan_import_status(import_id: str, request: Request):
    try:
        return baidu_pan.get_import_status(
            import_id, username=str(request.scope.get("elt_username") or ""))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/baidu-pan/imports")
def baidu_pan_import_list(request: Request, limit: int = 50):
    username, privileged = _baidu_identity(request)
    return {"jobs": baidu_pan.list_imports(
        username=username, is_admin=bool(multiuser_enabled() and privileged), limit=limit)}


@router.post("/baidu-pan/imports/{import_id}/cancel")
def baidu_pan_import_cancel(import_id: str, request: Request):
    username, _ = _baidu_identity(request)
    try:
        return baidu_pan.cancel_import(import_id, username=username)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/baidu-pan/imports/{import_id}/password")
def baidu_pan_import_password(import_id: str, body: BaiduPanPasswordBody, request: Request):
    username, _ = _baidu_identity(request)
    try:
        return baidu_pan.supply_password(import_id, body.pwd, username=username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/baidu-pan/imports/{import_id}/retry")
def baidu_pan_import_retry(import_id: str, body: BaiduPanRetryBody, request: Request):
    username, _ = _baidu_identity(request)
    try:
        return baidu_pan.retry_import(import_id, username=username, pwd=body.pwd)
    except baidu_pan.BaiduPanBusyError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except (ValueError, baidu_pan.BaiduPanError) as e:
        raise HTTPException(status_code=400, detail=baidu_pan.friendly_message(e))


@router.get("/baidu-pan/drive")
def baidu_pan_drive_list(request: Request, path: str = "", page: int = 1,
                         page_size: int = 50, order: str = "time", desc: bool = True):
    _username, privileged = _baidu_identity(request)
    if not privileged:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        return baidu_pan.list_drive(path, page=page, page_size=page_size,
                                    order=order, desc=desc)
    except (ValueError, baidu_pan.BaiduPanError) as e:
        raise HTTPException(status_code=400, detail=baidu_pan.friendly_message(e))


@router.get("/baidu-pan/drive/search")
def baidu_pan_drive_search(request: Request, q: str, page: int = 1, page_size: int = 50):
    _username, privileged = _baidu_identity(request)
    if not privileged:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        return baidu_pan.search_drive(q, page=page, page_size=page_size)
    except (ValueError, baidu_pan.BaiduPanError) as e:
        raise HTTPException(status_code=400, detail=baidu_pan.friendly_message(e))


@router.post("/baidu-pan/drive/imports")
def baidu_pan_drive_import(body: BaiduPanDriveImportBody, request: Request):
    username, privileged = _baidu_identity(request)
    if not privileged:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        return baidu_pan.start_drive_import(
            body.model_dump(), username=username, is_admin=privileged)
    except baidu_pan.BaiduPanBusyError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except (PermissionError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/reading/upload")
async def upload_reading_file(request: Request, file: UploadFile = File(...),
                              tts: bool = Form(False)):
    filename = file.filename or ""
    try:
        content = await file.read()
        return service.start_reading_upload_lesson(
            filename, content, tts=tts,
            username=str(request.scope.get("elt_username") or ""),
            idempotency_key=request.headers.get("Idempotency-Key", ""),
        )
    except InsufficientCredits as e:
        raise HTTPException(status_code=402,
                            detail=credit_meter.insufficient_payload(e))
    except credit_meter.OperationConflictError as e:
        raise HTTPException(status_code=409, detail=e.detail or str(e))
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
    output_dir = user_assets.current_output_root(service.OUTPUT_DIR)
    shutil.rmtree(output_dir / "v2_assets" / str(lesson_id), ignore_errors=True)
    shutil.rmtree(output_dir / "v2_exports" / str(lesson_id), ignore_errors=True)
    for export in output_dir.glob(f"v2-intensive-{lesson_id}.html"):
        export.unlink(missing_ok=True)
    return {"ok": True, "deleted": lesson_id}


class RetrySubtitlesBody(BaseModel):
    whisper_model: str = "large-v3"


@router.post("/{lesson_id}/retry-subtitles")
def retry_subtitles(lesson_id: int, request: Request, body: RetrySubtitlesBody | None = None):
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    # 重新拉取 YouTube/B 站字幕会走平台下载链路，仅管理员可用
    if str(lesson["source_type"]) in {"youtube", "bilibili"}:
        require_admin(request)
    if lesson["subtitle_status"] not in {"failed", "ready"}:
        raise HTTPException(status_code=409, detail="Subtitle pipeline is already running")
    model = (body.whisper_model if body else "") or "large-v3"
    translate = bool(lesson.get("translation_requested"))
    source_type = str(lesson["source_type"])

    op = None
    billed = credit_meter.mode() in ("shadow", "enforce")
    if billed:
        # 用户主动重转录 = 新的独立幂等 operation（Task 7）；
        # 同 key 重放不重复 enqueue、不重复扣费
        try:
            idem_key = credit_meter.require_idempotency_key(
                request.headers.get("Idempotency-Key", ""))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        try:
            op, replayed = service.retranscribe_operation(
                lesson, str(request.scope.get("elt_username") or ""), idem_key)
        except InsufficientCredits as e:
            raise HTTPException(status_code=402,
                                detail=credit_meter.insufficient_payload(e))
        except credit_meter.OperationConflictError as e:
            # key 跨操作类型或跨 lesson 复用：显式 409，不动原 operation
            raise HTTPException(status_code=409, detail=e.detail or str(e))
        except service.CreditOperationReleasedError as e:
            raise HTTPException(status_code=409, detail=str(e))
        if replayed:
            return {"ok": True, "replayed": True, "whisper_model": model,
                    "credits": service._op_billing(op)}

    db.set_v2_lesson_status(lesson_id, subtitle_status="pending")
    db.clear_v2_lesson_subtitle_error(lesson_id)
    try:
        with credit_meter.use_operation(op):  # None 时为空上下文，单用户无影响
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
            elif source_type == "uploaded_media":
                # source_url 为 upload:<id>，重转录从本用户暂存文件重新导入
                upload = db.get_v2_media_upload(str(lesson["source_url"]).split(":", 1)[-1])
                if not upload:
                    raise HTTPException(status_code=409, detail="原始上传已不存在，无法重转录")
                media_path = user_assets.current_uploads_root() / upload["stored_relpath"]
                service.enqueue_local_import(
                    lesson_id, str(media_path), whisper_model=model, translate=translate,
                )
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported source type: {source_type}")
    except Exception:
        # enqueue 失败：后台管线不会运行，立即释放本次重转录占位
        if op is not None:
            credit_meter.release(op["id"], reason="retranscribe enqueue failed")
        raise
    result = {"ok": True, "whisper_model": model, "replayed": False}
    if op is not None:
        result["credits"] = service._op_billing(op)
    return result


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


def _begin_billed(request: Request, operation_type: str, **kwargs):
    """同步 AI 计费开始；异常映射 402/409/400（统一载荷）。"""
    try:
        return credit_meter.begin_sync_operation(request, operation_type, **kwargs)
    except (credit_meter.InsufficientCredits,
            credit_meter.OperationConflictError, ValueError) as exc:
        status, detail = credit_meter.billing_error(exc)
        raise HTTPException(status_code=status, detail=detail) from exc


# ── 课时级 AI 推荐：点击生成，持久保存，白名单校验 ──────────────────

_LESSON_REC_SENTENCE_LIMIT = 150
_LESSON_REC_CHAR_LIMIT = 12000
_LESSON_REC_WORD_LIMIT = 12
_LESSON_REC_PATTERN_LIMIT = 8
_WORD_TOKEN_RE = re.compile(r"[a-z]+(?:'[a-z]+)?")


def _lesson_rec_context(lesson_id: int) -> tuple[str, list[dict]]:
    """取课程标题 + (key, text) 句子清单，与精学页 key 语义完全一致。"""
    document = build_intensive_document(lesson_id)
    title = str((document.get("lesson") or {}).get("title") or "")
    sentences: list[dict] = []
    total_chars = 0
    for item in document.get("sentences") or []:
        text = " ".join(str(item.get("text") or "").split())
        if not text:
            continue
        if len(sentences) >= _LESSON_REC_SENTENCE_LIMIT or total_chars + len(text) > _LESSON_REC_CHAR_LIMIT:
            break
        sentences.append({"key": int(item.get("key")), "text": text})
        total_chars += len(text)
    if not sentences:
        raise ValueError("这节课还没有可分析的句子")
    return title, sentences


def _clean_rec_text(value, maximum: int) -> str:
    return " ".join(str(value or "").split())[:maximum]


def _validate_lesson_recs(result: dict, sentences: list[dict]) -> dict:
    """白名单校验：词必须出自课文、句式必须定位到存在的句子且 phrase 出自该句。"""
    text_by_key = {int(s["key"]): s["text"] for s in sentences}
    token_set = set()
    for text in text_by_key.values():
        token_set.update(_WORD_TOKEN_RE.findall(text.lower()))

    words: list[dict] = []
    seen_words: set[str] = set()
    for item in result.get("words") or []:
        if not isinstance(item, dict):
            continue
        word = _clean_rec_text(item.get("word"), 40).lower()
        if (not re.fullmatch(r"[a-z]+(?:'[a-z]+)?", word)
                or len(word) < 3 or word in seen_words or word not in token_set):
            continue
        seen_words.add(word)
        words.append({"word": word, "reason": _clean_rec_text(item.get("reason"), 120)})
        if len(words) >= _LESSON_REC_WORD_LIMIT:
            break

    patterns: list[dict] = []
    seen_keys: set[int] = set()
    for item in result.get("patterns") or []:
        if not isinstance(item, dict):
            continue
        try:
            key = int(item.get("sentence_key"))
        except (TypeError, ValueError):
            continue
        text = text_by_key.get(key)
        if not text or key in seen_keys:
            continue
        phrase = _clean_rec_text(item.get("phrase"), 120)
        if not phrase or phrase.lower() not in text.lower():
            continue
        seen_keys.add(key)
        patterns.append({
            "sentence_key": key,
            "phrase": phrase,
            "reason": _clean_rec_text(item.get("reason"), 120),
        })
        if len(patterns) >= _LESSON_REC_PATTERN_LIMIT:
            break

    if not words and not patterns:
        raise ValueError("AI 没有返回可用推荐，请重试")
    return {"words": words, "patterns": patterns}


@router.get("/{lesson_id}/ai-recommendations")
def get_lesson_ai_recommendations(lesson_id: int):
    if not db.get_v2_lesson(lesson_id):
        raise HTTPException(status_code=404, detail="Lesson not found")
    record = db.get_v2_lesson_ai_recommendation(lesson_id)
    return {"recommendation": record["payload"] if record else None,
            "generated_at": record["updated_at"] if record else ""}


class LessonAiRecommendationBody(BaseModel):
    regenerate: bool = False


@router.post("/{lesson_id}/ai-recommendations/refresh")
def refresh_lesson_ai_recommendations(lesson_id: int, request: Request,
                                      body: LessonAiRecommendationBody | None = None):
    if not db.get_v2_lesson(lesson_id):
        raise HTTPException(status_code=404, detail="Lesson not found")
    regenerate = bool(body and body.regenerate)
    existing = db.get_v2_lesson_ai_recommendation(lesson_id)
    # cache-before-reserve：已有推荐且非主动重生成 → 免费返回
    if existing and not regenerate:
        return {"ok": True, "cached": True, "recommendation": existing["payload"],
                "generated_at": existing["updated_at"],
                "credits": {"charged": 0, "cached": True}}
    op, replay = _begin_billed(request, "recommendation_refresh",
                               reference_type="v2_lesson",
                               reference_id=str(lesson_id))
    if replay is not None:
        return replay
    try:
        title, sentences = _lesson_rec_context(lesson_id)
        prompt = LESSON_AI_RECOMMENDATION_PROMPT.format(
            lesson_title=title or f"课程 {lesson_id}",
            sentences_json=json.dumps(sentences, ensure_ascii=False),
        )
        result = _request_pattern_json(prompt)
        payload = _validate_lesson_recs(result, sentences)
        record = db.save_v2_lesson_ai_recommendation(
            lesson_id, payload, model=ai_config.AI_MODEL)
    except Exception as exc:
        credit_meter.release_sync(op, reason=f"lesson_ai_recommendation failed: {exc}"[:500])
        return JSONResponse({"error": str(exc)}, status_code=500)
    response = {"ok": True, "cached": False, "recommendation": record["payload"],
                "generated_at": record["updated_at"]}
    credit_meter.settle_sync(op, actual_usage={
        "model": ai_config.AI_MODEL, "lesson_id": lesson_id}, response=response)
    return response


@router.post("/sentence-review/{sentence_id}/pattern")
def create_sentence_pattern(sentence_id: int, request: Request):
    sentence = _saved_sentence_or_404(sentence_id)
    # cache-before-reserve：已有句型免费返回
    cached = db.get_v2_sentence_pattern(sentence_id)
    if cached and cached.get("pattern_template"):
        return {**_pattern_response(cached, cached=True),
                "credits": {"charged": 0, "cached": True}}
    op, replay = _begin_billed(request, "sentence_pattern",
                               reference_type="v2_sentence",
                               reference_id=str(sentence_id))
    if replay is not None:
        return replay
    try:
        result = _request_pattern_json(
            PATTERN_EXTRACTION_PROMPT.format(english=sentence["text"])
        )
        pattern_template = str(result.get("pattern_template") or "").strip()
        pattern = db.save_v2_sentence_pattern(sentence_id, pattern_template)
    except Exception as exc:
        credit_meter.release_sync(op, reason=f"sentence_pattern failed: {exc}"[:500])
        return JSONResponse({"error": str(exc)}, status_code=500)
    payload = _pattern_response(pattern, cached=False)
    credit_meter.settle_sync(op, actual_usage={
        "model": ai_config.AI_MODEL, "sentence_id": sentence_id}, response=payload)
    return payload


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
    request: Request,
):
    sentence = _saved_sentence_or_404(sentence_id)
    pattern = db.get_v2_sentence_pattern(sentence_id)
    has_template = bool(pattern and pattern.get("pattern_template"))
    # 无 AI 句式分析时直接以原句为迁移参考
    template = pattern["pattern_template"] if has_template else sentence["text"]
    # cache-before-reserve：已有情景且非主动重生成 → 免费返回
    if has_template and pattern.get("scenario_cn") and not body.regenerate:
        return {**_pattern_response(pattern, cached=True),
                "credits": {"charged": 0, "cached": True}}
    op, replay = _begin_billed(request, "sentence_scenario",
                               reference_type="v2_sentence",
                               reference_id=str(sentence_id))
    if replay is not None:
        return replay
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
            payload = _pattern_response(pattern, cached=False)
            credit_meter.settle_sync(op, actual_usage={
                "model": ai_config.AI_MODEL, "sentence_id": sentence_id,
                "regenerate": bool(body.regenerate)}, response=payload)
            return payload
    except Exception as exc:
        credit_meter.release_sync(op, reason=f"sentence_scenario failed: {exc}"[:500])
        return JSONResponse({"error": str(exc)}, status_code=500)
    payload = {"scenario_cn": scenario_cn, "cached": False,
               "reference": "original_sentence"}
    credit_meter.settle_sync(op, actual_usage={
        "model": ai_config.AI_MODEL, "sentence_id": sentence_id,
        "reference": "original_sentence"}, response=payload)
    return payload


def _sentence_audio_path(sentence_id: int) -> Path:
    audio_dir = user_assets.user_output_subdir(
        "v2_sentence_audio", fallback=OUTPUT_DIR / "v2_sentence_audio"
    )
    return audio_dir / f"sentence-{sentence_id}.wav"


def _sentence_text_or_404(sentence_id: int) -> str:
    sentence = db.get_v2_sentence_by_id(sentence_id)
    if not sentence:
        raise HTTPException(status_code=404, detail="Sentence not found")
    text = str(sentence.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=404, detail="Sentence has no text")
    return text


@router.get("/sentence-audio/{sentence_id}")
def sentence_audio(sentence_id: int):
    """Reading 课收藏句没有原音，用 SAPI 合成 wav 并缓存。"""
    text = _sentence_text_or_404(sentence_id)
    audio_path = _sentence_audio_path(sentence_id)
    if credit_meter.billing_active():
        # 计费模式：GET 只读缓存，新合成必须走 POST /sentence-audio/prepare
        if not is_current_tts_audio(audio_path, text):
            raise HTTPException(
                status_code=409,
                detail={"code": "prepare_required",
                        "prepare_url": "/api/v2/lessons/sentence-audio/prepare"})
        if not audio_path.exists():
            raise HTTPException(status_code=404, detail="Sentence audio cache missing")
    else:
        if not is_current_tts_audio(audio_path, text):
            synthesize_sentence_audio(text, audio_path)
        if not audio_path.exists():
            raise HTTPException(status_code=502, detail="Sentence audio synthesis failed")
    return FileResponse(audio_path, media_type="audio/wav")


@router.post("/sentence-audio/prepare")
def sentence_audio_prepare(request: Request, body: dict | None = None):
    """显式句子音频合成（Task 8 sentence_tts）：缓存优先，只对新合成计费。"""
    sentence_id = int((body or {}).get("sentence_id") or 0)
    text = _sentence_text_or_404(sentence_id)
    audio_path = _sentence_audio_path(sentence_id)
    audio_url = f"/api/v2/lessons/sentence-audio/{sentence_id}"
    if is_current_tts_audio(audio_path, text) and audio_path.exists():
        return {"audio_url": audio_url, "cached": True,
                "credits": {"charged": 0, "cached": True}}

    op, replay = _begin_billed(request, "sentence_tts",
                               char_count=len(text),
                               reference_type="v2_sentence",
                               reference_id=str(sentence_id))
    if replay is not None:
        return replay
    try:
        synthesize_sentence_audio(text, audio_path)
    except Exception as exc:
        credit_meter.release_sync(op, reason=f"sentence_tts failed: {exc}"[:500])
        raise HTTPException(status_code=502,
                            detail=f"Sentence audio synthesis failed: {exc}")
    if not audio_path.exists():
        credit_meter.release_sync(op, reason="sentence_tts produced no audio")
        raise HTTPException(status_code=502, detail="Sentence audio synthesis failed")

    payload = {"audio_url": audio_url, "cached": False}
    credit_meter.settle_sync(op, actual_usage={
        "engine": "sapi",
        "characters": len(text),
        "sentence_id": sentence_id,
        "audio_path": audio_path.name,
    }, response=payload)
    return payload


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


@router.post("/{lesson_id}/reading/tts/cancel")
def cancel_reading_tts_route(lesson_id: int):
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    was_running = cancel_reading_tts(lesson_id)
    return {"lesson_id": lesson_id, "cancel_requested": True, "was_running": was_running}


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
    from webapp.services.mfa_alignment import apply_aligned_unit_times

    return {
        "lesson_id": lesson_id,
        "subtitle_status": lesson["subtitle_status"],
        "segments": highlighted,
        "sentence_units": apply_aligned_unit_times(
            lesson_id, build_translation_units(highlighted)
        ),
    }


@router.get("/{lesson_id}/reading")
def lesson_reading(lesson_id: int, wordlists: str | None = None):
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    blocks = service.ensure_media_reading_blocks(lesson_id, lesson)
    if not blocks:
        raise HTTPException(status_code=409, detail="Reading content is not ready")
    source_words, source_lists, hidden_words = _highlight_context(lesson_id, wordlists)
    highlighted = highlight_reading_blocks(
        blocks, hidden_words=hidden_words, source_words=source_words, source_lists=source_lists
    )
    return {"lesson": lesson, "blocks": highlighted["blocks"], "candidate_count": highlighted["candidate_count"]}


@router.post("/{lesson_id}/highlighted-words/sync")
def sync_highlighted_words(lesson_id: int, wordlists: str | None = None):
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    source_words, source_lists, hidden_words = _highlight_context(lesson_id, wordlists)
    if lesson.get("lesson_mode") == "reading":
        blocks = db.get_v2_reading_blocks(lesson_id)
        highlighted = highlight_reading_blocks(
            blocks, hidden_words=hidden_words, source_words=source_words, source_lists=source_lists
        )
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
    dict_meta = dict_service.format_ecdict_meta(
        dict_service.lookup_ecdict_meta(str(lookup.get("lemma") or normalized))
    )
    return {
        **lookup,
        "dict_meta": dict_meta,
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
def translate_selection(lesson_id: int, body: TranslateSelectionBody, request: Request):
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    text = " ".join(body.text.split())
    if not text:
        raise HTTPException(status_code=400, detail="选区不能为空")
    if len(text) > 4000:
        raise HTTPException(status_code=400, detail="选区不能超过 4000 字符")
    # cache-before-reserve：已缓存翻译免费返回
    cached = db.get_v2_sentence(text)
    translation = str((cached or {}).get("translation") or "").strip()
    if translation:
        return {"translation": translation, "engine": "hy-mt",
                "credits": {"charged": 0, "cached": True}}
    op, replay = _begin_billed(request, "selection_translation",
                               reference_type="v2_lesson",
                               reference_id=str(lesson_id))
    if replay is not None:
        return replay
    try:
        translation = _hy_mt_translate(text)
    except HTTPException as exc:
        credit_meter.release_sync(op, reason=f"selection_translation failed: {exc.detail}"[:500])
        raise
    db.upsert_v2_sentence(text, translation=translation)
    credit_meter.settle_sync(op, actual_usage={
        "engine": "hy-mt", "characters": len(text), "lesson_id": lesson_id},
        response={"translation": translation, "engine": "hy-mt"})
    return {"translation": translation, "engine": "hy-mt"}


@router.post("/{lesson_id}/translate-sentences")
def translate_sentences(lesson_id: int, body: TranslateSentencesBody, request: Request):
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
    # batch_translation 计费：只按未缓存句子的字符数报价；命中缓存部分免费
    char_count = sum(len(t) for t in pending)
    op, replay = _begin_billed(request, "batch_translation",
                               char_count=char_count,
                               reference_type="v2_lesson",
                               reference_id=str(lesson_id))
    if replay is not None:
        return replay
    try:
        for text in pending:
            translation = _hy_mt_translate(text)
            db.upsert_v2_sentence(text, translation=translation)
            results[text] = translation
    except Exception as exc:
        detail = getattr(exc, "detail", str(exc))
        credit_meter.release_sync(op, reason=f"batch_translation failed: {detail}"[:500])
        raise
    payload = {"translations": results, "engine": "hy-mt"}
    credit_meter.settle_sync(op, actual_usage={
        "engine": "hy-mt", "characters": char_count, "lesson_id": lesson_id,
        "sentences": len(pending)}, response=payload)
    return payload


@router.get("/{lesson_id}/sentence-translations")
def get_sentence_translations(lesson_id: int):
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    units = build_translation_units(db.get_v2_subtitle_segments(lesson_id))
    if not units and str(lesson.get("source_type") or "").startswith("reading"):
        # Reading 课 TTS 未完成时还没有字幕段：从阅读块取句，让并行翻译的缓存立即可见
        from webapp.services.v2_tts import _synthesizable_sentences

        units = [
            {"text": sentence, "end": 0.0}
            for block in db.get_v2_reading_blocks(lesson_id)
            for sentence in _synthesizable_sentences(str(block.get("text") or ""))
        ]
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
def start_alignment(lesson_id: int, request: Request, body: AlignmentBody | None = None):
    from webapp.services.mfa_alignment import enqueue_lesson_alignment, get_alignment_status

    force = bool(body and body.force)
    op, free_retry, replayed = _ancillary_billing(
        request, lesson_id, force=force, capability="alignment",
        operation_type="alignment_rebuild", per_minute=True,
        current_status=get_alignment_status(lesson_id).get("status")
        if db.get_v2_lesson(lesson_id) else None,
        running_states={"queued", "running"},
        failed_states={"failed"},
    )
    if replayed:
        result = get_alignment_status(lesson_id)
        result["credits"] = service._op_billing(op)
        result["replayed"] = True
        return result
    try:
        with credit_meter.use_operation(op):
            result = enqueue_lesson_alignment(lesson_id, force=force)
    except Exception as exc:
        if op is not None:
            credit_meter.release(op["id"], reason="alignment enqueue rejected")
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise
    if op is not None:
        result["credits"] = service._op_billing(op)
    if free_retry:
        result["credits"] = {"mode": credit_meter.mode(), "charged": 0,
                             "free_retry": True}
    return result


def _ancillary_billing(request: Request, lesson_id: int, *, force: bool,
                       capability: str, operation_type: str, per_minute: bool,
                       current_status, running_states: set, failed_states: set):
    """附属能力（导航/对齐）计费编排（Task 7）。返回 (op, free_retry, replayed)。

    - force=False：首次生成属于课程 bundle，免费，(None, False, False)。
    - force=True 且当前状态为失败：每个 (用户, 能力, 课程) 恰好第一次免费
      （审计写入 credit_free_retries），返回 (None, True, False)；之后才计费。
    - force=True 计费：独立幂等 operation；同 key 重放返回 (op, False, True)，
      调用方返回当前状态、不重复 enqueue；已 released → 409。
    - 正在运行中：409，不产生任何计费。
    """
    if not force or credit_meter.mode() not in ("shadow", "enforce"):
        return None, False, False
    if current_status in running_states:
        raise HTTPException(status_code=409, detail=f"{capability} is already running")
    username = str(request.scope.get("elt_username") or "")
    try:
        idem_key = credit_meter.require_idempotency_key(
            request.headers.get("Idempotency-Key", ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    op = credit_meter.get_operation_by_key(username, idem_key)
    if op is not None:
        try:
            credit_meter.require_operation_identity(
                op, operation_type=operation_type,
                reference_type="lesson", reference_id=str(lesson_id))
        except credit_meter.OperationConflictError as e:
            raise HTTPException(status_code=409, detail=e.detail or str(e))
        if op["status"] == "released":
            raise HTTPException(status_code=409,
                                detail="该 Idempotency-Key 的上一次操作已失败释放，请用新的 key 重试")
        return op, False, True
    if current_status in failed_states and credit_meter.try_consume_free_retry(
            username, capability, str(lesson_id),
            reason=f"course build bundled {capability} failed; first retry free"):
        return None, True, False
    kwargs: dict = {"idempotency_key": idem_key,
                    "reference_type": "lesson", "reference_id": str(lesson_id)}
    if per_minute:
        lesson = db.get_v2_lesson(lesson_id) or {}
        kwargs["duration_seconds"] = max(1.0, float(lesson.get("duration") or 60.0))
    try:
        op = credit_meter.reserve(username, operation_type, **kwargs)
    except InsufficientCredits as e:
        raise HTTPException(status_code=402,
                            detail=credit_meter.insufficient_payload(e))
    if op is None:
        raise HTTPException(status_code=500, detail="credit reserve unavailable")
    return op, False, False


@router.post("/{lesson_id}/outline-summary")
def outline_summary(lesson_id: int, request: Request, body: OutlineSummaryBody | None = None):
    force = bool(body and body.force)
    try:
        current = get_document_outline_status(lesson_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    op, free_retry, replayed = _ancillary_billing(
        request, lesson_id, force=force, capability="outline",
        operation_type="outline_regenerate", per_minute=False,
        current_status=current.get("status"),
        running_states={"pending"},
        failed_states={"error"},
    )
    if replayed:
        result = dict(current)
        result["credits"] = service._op_billing(op)
        result["replayed"] = True
        return result

    def _with_credits(result: dict) -> dict:
        result = dict(result)
        if op is not None:
            result["credits"] = service._op_billing(op)
        if free_retry:
            result["credits"] = {"mode": credit_meter.mode(), "charged": 0,
                                 "free_retry": True}
        return result

    try:
        with credit_meter.use_operation(op):
            result = start_document_outline_generation(lesson_id, force=force)
        if result.get("status") == "pending":
            return JSONResponse(status_code=202, content=_with_credits(result))
        return _with_credits(result)
    except Exception as exc:
        # 启动失败：后台任务不会运行，立即释放 fresh 占位
        if op is not None:
            credit_meter.release(op["id"], reason="outline start failed")
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if isinstance(exc, RuntimeError):
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=502, detail=f"Outline generation failed: {exc}") from exc


@router.post("/{lesson_id}/intensive-export")
def export_intensive(lesson_id: int):
    try:
        return export_intensive_html(lesson_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

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
    # 已掌握/已认识词不再出现在学习收藏面板（取消掌握后自动恢复）
    excluded = db.get_mastered_review_targets() | db.get_known_words()
    words = []
    meanings = {}
    for item in db.get_v2_lesson_words(lesson_id):
        word = item.get("word", "")
        if not word or word.lower() in excluded:
            continue
        words.append(word)
        analysis = item.get("cached_analysis")
        if isinstance(analysis, dict):
            meaning = str(analysis.get("basic_meaning") or "").strip()
            if meaning:
                meanings[word] = meaning
        # 收藏时未带释义的词，用 ECDICT 首义项回填，保证 lookup 角标有内容
        if word not in meanings:
            backfill = dict_service.lookup_ecdict_translation(word)
            if backfill:
                meanings[word] = backfill
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
    # 词句对应守卫：前端句子来自对齐数据，错位课程会传不含该词的句子（2026-08-06 bug3）。
    # 不含词时改用 v2_sentences 中真正含该词的句子，找不到则不写语境，避免脏数据入 contexts。
    context_sentence = str(body.sentence or "")
    if context_sentence and not db.sentence_contains_word(context_sentence, word):
        context_sentence = db.find_v2_sentence_containing(word) or ""
    db.save_v2_lesson_word(lesson_id, word, context_sentence)
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
    if context_sentence:
        db.add_context(word, lesson.get("title") or "v2 workspace", context_sentence)
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


@router.post("/{lesson_id}/word/{word}/master")
def master_lesson_word(lesson_id: int, word: str):
    """Apply the complete mastered lifecycle through one authoritative request."""
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    normalized = (
        db.normalize_vocab_target(word, target_type="phrase")
        if " " in word.strip()
        else lookup_word_meaning(word)["word"]
    )
    if not normalized:
        raise HTTPException(status_code=400, detail="Invalid word")

    lifecycle = db.set_review_word_lifecycle(normalized, mastered=True)
    if not lifecycle or not lifecycle.get("mastered"):
        raise HTTPException(status_code=500, detail="Failed to mark word as mastered")
    deleted = db.delete_v2_lesson_word(lesson_id, normalized)
    db.hide_v2_lesson_word(lesson_id, normalized)
    forget_word_meaning_cache(normalized)
    return {
        "ok": True,
        "word": normalized,
        "saved": False,
        "mastered": True,
        "deleted": deleted,
    }
