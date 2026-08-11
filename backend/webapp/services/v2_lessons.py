"""V2 lesson orchestration service."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import threading
import hashlib
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import db
from sources.baidu import build_local_video_lesson, _find_ffprobe
from sources.bilibili import build_bilibili_lesson
from sources.youtube import download_youtube_audio, extract_video_id, fetch_youtube_subtitles, source_bundle_to_segment_dicts
from webapp.services.media_reading import build_media_reading_blocks
from webapp.services.reading_import import build_reading_blocks_from_text, extract_text_from_pdf, extract_text_from_upload
from webapp.services.v2_translation import translate_lesson_subtitles
from webapp.services.v2_tts import build_timed_reading_blocks, enqueue_reading_tts, reading_tts_is_cached
from webapp.runtime import credit_meter
from webapp.storage import user_assets
from webapp.storage.lessons import OUTPUT_DIR


_AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac"}
_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv"}
_READING_UPLOAD_JOBS: dict[str, dict] = {}
_READING_UPLOAD_JOBS_LOCK = threading.Lock()
# inflight 按 (用户 scope, 文件摘要) 去重；不同用户上传同一文件各自建课
_READING_UPLOAD_INFLIGHT: dict[tuple[str, str], str] = {}
# 容量限制保持全局：信号量与 job 表上限不随用户放大
_READING_UPLOAD_SLOTS = threading.BoundedSemaphore(2)
_READING_UPLOAD_JOB_LIMIT = 100


class ReadingUploadBusyError(RuntimeError):
    pass


# ── 普通用户浏览器音视频上传（Task 3）────────────────────────
# 暂存于当前用户 uploads/<uuid>/，记录在当前用户 vocab.db 的
# v2_media_uploads；跨用户 upload_id 不可见（404）。积分影子费率
# 版本 shadow-v1：course_build_media 5 分/开始一分钟（向上取整）。

_MEDIA_UPLOAD_CHUNK = 1024 * 1024
_MEDIA_UPLOAD_RATE_VERSION = "shadow-v1"
_MEDIA_UPLOAD_POINTS_PER_MINUTE = 5


class MediaUploadError(ValueError):
    """上传内容不合法（扩展名伪装、ffprobe 无法识别、空文件等）→ 400。"""


class MediaUploadTooLargeError(MediaUploadError):
    """超过 ELT_MEDIA_UPLOAD_MAX_MB → 413。"""


class MediaUploadConsumedError(MediaUploadError):
    """并发消费竞争中失败：另一请求已 claim 该 upload（课程可能正在创建）。"""


def _media_upload_max_bytes() -> int:
    try:
        mb = int(os.environ.get("ELT_MEDIA_UPLOAD_MAX_MB", "500"))
    except ValueError:
        mb = 500
    return max(1, mb) * 1024 * 1024


def media_upload_quote(duration_seconds: float) -> dict:
    """上传建课报价：多用户模式与积分费率同源（credits.quote），
    单用户/公开库回退到内置 shadow-v1 数字（5 分/开始一分钟）。"""
    q = credit_meter.quote("course_build_media",
                           duration_seconds=max(1.0, float(duration_seconds)))
    if q is not None:
        return {
            "operation_type": "course_build_media",
            "points": q["quoted_points"],
            "rate_version": q["rate_version"],
            "mode": credit_meter.mode(),
        }
    minutes = max(1, math.ceil(max(0.0, float(duration_seconds)) / 60))
    return {
        "operation_type": "course_build_media",
        "points": minutes * _MEDIA_UPLOAD_POINTS_PER_MINUTE,
        "rate_version": _MEDIA_UPLOAD_RATE_VERSION,
        "mode": "shadow",
    }


def _safe_upload_filename(filename: str) -> str:
    """剥离路径成分、白名单扩展名、清洗文件名字符；不合法抛 MediaUploadError。"""
    name = Path(str(filename or "").replace("\\", "/")).name
    stem, ext = os.path.splitext(name)
    ext = ext.lower()
    if ext not in _AUDIO_EXTS | _VIDEO_EXTS:
        raise MediaUploadError(f"不支持的文件类型：{ext or name or '(无文件名)'}")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "media"
    return stem[:80] + ext


def _probe_uploaded_media(path: Path) -> tuple[float, str]:
    """ffprobe 双重校验：可读、有音/视频流、有时长；返回 (时长秒, media_kind)。"""
    try:
        result = subprocess.run(
            [_find_ffprobe(), "-v", "error",
             "-show_entries", "format=duration:stream=codec_type",
             "-of", "json", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
        )
        info = json.loads(result.stdout or "{}")
        duration = float((info.get("format") or {}).get("duration") or 0)
        kinds = {s.get("codec_type") for s in info.get("streams") or []}
    except Exception as exc:
        raise MediaUploadError(f"无法识别的媒体文件：{exc}")
    if duration <= 0 or not (kinds & {"audio", "video"}):
        raise MediaUploadError("文件不是有效的音视频媒体")
    return duration, ("local_video" if "video" in kinds else "local_audio")


def save_media_upload(filename: str, stream) -> dict:
    """分块落盘 → 大小限制 → ffprobe 校验 → 入库；任何失败清除残片。"""
    safe_name = _safe_upload_filename(filename)
    upload_id = uuid.uuid4().hex
    folder = user_assets.current_uploads_root() / upload_id
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / safe_name
    limit = _media_upload_max_bytes()
    size = 0
    try:
        with open(target, "wb") as fh:
            while True:
                chunk = stream.read(_MEDIA_UPLOAD_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise MediaUploadTooLargeError(
                        f"文件超过大小限制 {limit // (1024 * 1024)} MB")
                fh.write(chunk)
        if size == 0:
            raise MediaUploadError("空文件")
        duration, media_kind = _probe_uploaded_media(target)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    record = db.create_v2_media_upload(
        upload_id, str(filename or safe_name), f"{upload_id}/{safe_name}",
        media_kind, size, duration,
    )
    record["upload_id"] = record["id"]
    record["quote"] = media_upload_quote(duration)
    return record


def get_media_upload(upload_id: str) -> dict:
    record = db.get_v2_media_upload(str(upload_id or ""))
    if not record:
        raise ValueError("Upload not found")
    return record


def delete_media_upload(upload_id: str) -> None:
    """仅本人未 consumed 的上传可删除；跨用户/不存在 → ValueError（路由 404）。"""
    record = get_media_upload(upload_id)
    if record["status"] == "consumed":
        raise MediaUploadError("该上传已用于建课，不能删除")
    if record["status"] == "deleted":
        raise ValueError("Upload not found")
    db.mark_v2_media_upload_deleted(record["id"])
    shutil.rmtree(user_assets.current_uploads_root() / record["id"], ignore_errors=True)


def start_uploaded_media_lesson(upload_id: str, *, whisper_model: str = "large-v3",
                                translate: bool = False) -> dict:
    """消费本用户 ready upload 建课；媒体复制进当前用户 output/v2_assets/<lesson_id>/。

    并发安全 claim + 失败回滚：先确认文件存在（缺失不消费，保持 ready 可重试），
    再原子 consume（并发只有一个获胜者）；建课/复制/enqueue 任一步失败时回滚
    upload 为 ready，并清除本轮创建的 lesson 与资产。

    多用户计费建课的归属排他由 start_uploaded_media_lesson_billed 经
    credit_build_claims 在调用本函数之前原子认领，本函数不承担账务。
    """
    upload = db.get_v2_media_upload(str(upload_id or ""))
    if not upload or upload["status"] == "deleted":
        raise FileNotFoundError("Upload not found")
    media_path = user_assets.current_uploads_root() / upload["stored_relpath"]
    if not media_path.is_file():
        # 文件缺失：不消费，保持 ready——补齐文件后可原样重试
        raise FileNotFoundError("Uploaded media file missing")
    if not db.consume_v2_media_upload(upload["id"]):
        raise MediaUploadConsumedError("上传不存在或已被使用")
    media_kind = upload["media_kind"] or _media_kind(media_path)
    title = Path(upload["original_filename"] or media_path.name).stem
    lesson_id: int | None = None
    try:
        lesson = db.create_v2_lesson(
            source_type="uploaded_media",
            source_url=f"upload:{upload['id']}",
            title=title,
            duration=upload["duration_seconds"],
            media_kind=media_kind,
        )
        lesson_id = int(lesson["id"])
        media_url = _copy_media_for_lesson(lesson_id, media_path)
        db.update_v2_lesson_metadata(
            lesson_id, media_url=media_url, media_kind=media_kind, title=title)
        db.configure_v2_lesson_translation(lesson_id, requested=translate)
        enqueue_local_import(
            lesson_id, str(media_path),
            transcript_path=None, whisper_model=whisper_model, translate=translate,
        )
    except Exception:
        # 回滚：删除本轮半成品 lesson/资产，upload 恢复 ready 可重试
        if lesson_id is not None:
            db.delete_v2_lesson(lesson_id)
            shutil.rmtree(
                user_assets.user_output_subdir(
                    "v2_assets", str(lesson_id),
                    fallback=OUTPUT_DIR / "v2_assets" / str(lesson_id),
                    create=False,
                ),
                ignore_errors=True,
            )
        db.restore_v2_media_upload_ready(upload["id"])
        raise
    lesson = db.get_v2_lesson(lesson_id) or lesson
    return {"lesson": lesson, "workspace_url": f"/workspace/{lesson_id}"}


class CreditOperationReleasedError(RuntimeError):
    """同一 Idempotency-Key 的上一次尝试已失败释放：客户端必须用新 key 重试。"""


class MissingIdempotencyKeyError(ValueError):
    """多用户可计费入口缺少合法 Idempotency-Key（路由转 400）。"""
def _op_billing(op: dict) -> dict:
    """响应中的计费摘要：不暴露内部流水，只给模式/积分/状态。"""
    return {
        "mode": credit_meter.mode(),
        "operation_id": op["id"],
        "operation_type": op["operation_type"],
        "points": int(op["quoted_points"]),
        "status": op["status"],
    }


def start_uploaded_media_lesson_billed(upload_id: str, *, whisper_model: str,
                                       translate: bool, username: str,
                                       idempotency_key: str) -> dict:
    """多用户建课计费编排：服务端按 upload 时长报价 → reserve → 消费/建课/后台管线。

    - reserve 发生在消费 upload 与创建 lesson 之前；enforce 余额不足由
      credits.InsufficientCredits 上抛（路由转 402），不发生任何业务变更。
    - Idempotency-Key 重放：已有 operation 且已绑定 lesson → 直接返回该 lesson，
      不重复建课、不重复扣费；operation 已 released → CreditOperationReleasedError（409）。
    - 崩溃恢复：operation 存在但未绑定 lesson 时，按 upload 来源找回已建课程补绑定。
    - 建课过程在 use_operation 上下文中 enqueue：后台线程继承父 operation，
      核心字幕成功 settle、核心失败 release（见 _import_local_media 等钩子）。
    """
    upload = db.get_v2_media_upload(str(upload_id or ""))
    if not upload or upload["status"] == "deleted":
        # 先 404：缺 key 不得提前泄露 upload 存在性
        raise FileNotFoundError("Upload not found")
    try:
        key = credit_meter.require_idempotency_key(idempotency_key)
    except ValueError as e:
        raise MissingIdempotencyKeyError(str(e)) from e

    op = credit_meter.get_operation_by_key(username, key)

    def _replayed_result(lesson: dict) -> dict:
        return {
            "lesson": lesson,
            "workspace_url": f"/workspace/{lesson['id']}",
            "credits": _op_billing(op),
            "replayed": True,
        }

    fresh_op = op is None
    if op is not None:
        # 语义身份校验（fail closed）：key 跨操作类型或跨 upload 复用 → 409，
        # 不消费、不建课、不动原 operation
        credit_meter.require_operation_identity(
            op, operation_type="course_build_media", reference_type="lesson")
        est = op.get("estimated_usage") or {}
        if est.get("upload_id") and est["upload_id"] != upload["id"]:
            raise credit_meter.OperationConflictError(
                "Idempotency-Key 已用于另一上传的建课，请换用新的 key")
        if op["status"] == "released":
            raise CreditOperationReleasedError(
                "该 Idempotency-Key 的上一次建课已失败释放，请用新的 key 重试")
        ref = op.get("reference_id")
        if ref:
            lesson = db.get_v2_lesson(int(ref))
            if lesson and str(lesson.get("source_url") or "") == f"upload:{upload['id']}":
                return _replayed_result(lesson)
            if lesson:
                raise credit_meter.OperationConflictError(
                    "Idempotency-Key 绑定的课程不属于该上传")
            raise MediaUploadError("该 Idempotency-Key 对应的课程已不存在")
    else:
        op = credit_meter.reserve(
            username, "course_build_media",
            idempotency_key=key,
            duration_seconds=max(1.0, float(upload["duration_seconds"] or 0)),
            reference_type="lesson",
            estimated_usage={"duration_seconds": float(upload["duration_seconds"] or 0),
                             "media_kind": upload["media_kind"],
                             "upload_id": upload["id"]},
        )
        if op is None:  # 防御：multiuser 判定与 reserve 不一致时不应走到这里
            raise RuntimeError("credit reserve unavailable")

    # 归属认领：在 consume/create 之前原子完成。并发不同 key 的败者在这里
    # 就被挡下（不可能进入 consume/attach，更不会触发胜者课程回滚）。
    try:
        outcome, claim = credit_meter.claim_build_upload(
            username, upload["id"], op["id"], key)
    except credit_meter.OperationConflictError:
        if fresh_op:
            # 败者：释放自己新建的占位（零扣费）；已存在的 op（同 key 重放）
            # 不属于本次创建，不动它
            credit_meter.release(
                op["id"], reason="duplicate concurrent build; upload claimed by another operation")
        raise

    if outcome == "promoted":
        # 课程在 promote 前已创建（崩溃恢复）：补绑定并直接返回
        lesson = db.get_v2_lesson(int(claim["lesson_id"]))
        if not lesson:
            raise MediaUploadError("该 Idempotency-Key 对应的课程已不存在")
        credit_meter.attach_reference(op["id"], "lesson", str(lesson["id"]))
        return _replayed_result(lesson)

    # claimed/resumed/taken_over：若课程在上次崩溃前已创建但未 promote，
    # 找回并 promote，不重复建课
    lesson = db.get_v2_lesson_by_source("uploaded_media", f"upload:{upload['id']}")
    if lesson:
        credit_meter.promote_build_claim(username, upload["id"], op["id"], int(lesson["id"]))
        credit_meter.attach_reference(op["id"], "lesson", str(lesson["id"]))
        return _replayed_result(lesson)

    try:
        with credit_meter.use_operation(op):
            result = start_uploaded_media_lesson(
                upload["id"], whisper_model=whisper_model, translate=translate)
    except Exception as exc:
        # promote 之前的永久失败：标记 claim failed（允许新 key 接管），释放 op
        credit_meter.fail_build_claim(username, upload["id"], op["id"],
                                      reason=f"course build failed before pipeline start: {exc}"[:500])
        credit_meter.release(op["id"],
                             reason=f"course build failed before pipeline start: {exc}"[:500])
        raise
    credit_meter.promote_build_claim(username, upload["id"], op["id"],
                                     int(result["lesson"]["id"]))
    credit_meter.attach_reference(op["id"], "lesson", str(result["lesson"]["id"]))
    result["credits"] = _op_billing(op)
    result["replayed"] = False
    return result


def retranscribe_operation(lesson: dict, username: str,
                           idempotency_key: str) -> tuple[dict, bool]:
    """用户主动重转录：独立幂等的新 operation（course_retranscribe，按课时长报价）。

    返回 (op, replayed)；replayed=True 表示同 key 重放，路由不得重复 enqueue。
    已 released 的同 key → CreditOperationReleasedError（409）。
    """
    key = credit_meter.require_idempotency_key(idempotency_key)
    op = credit_meter.get_operation_by_key(username, key)
    if op is not None:
        # 语义身份校验：key 跨类型或跨 lesson 复用 → 409 fail closed
        credit_meter.require_operation_identity(
            op, operation_type="course_retranscribe",
            reference_type="lesson", reference_id=str(lesson["id"]))
        if op["status"] == "released":
            raise CreditOperationReleasedError(
                "该 Idempotency-Key 的上一次重转录已失败释放，请用新的 key 重试")
        return op, True
    duration = float(lesson.get("duration") or 0)
    if duration <= 0:
        duration = 60.0  # 平台课程时长未知时按 1 分钟下限报价
    op = credit_meter.reserve(
        username, "course_retranscribe",
        idempotency_key=key,
        duration_seconds=duration,
        reference_type="lesson", reference_id=str(lesson["id"]),
        estimated_usage={"duration_seconds": duration},
    )
    if op is None:
        raise RuntimeError("credit reserve unavailable")
    return op, False


def start_youtube_lesson(url: str, *, translate: bool = False) -> dict:
    video_id = extract_video_id(url)
    fallback_title = f"YouTube Lesson {video_id}" if video_id else "YouTube Lesson"
    lesson = db.create_v2_lesson(
        source_type="youtube",
        source_url=url,
        video_id=video_id,
        title=fallback_title,
        duration=0,
    )
    if not str(lesson.get("title") or "").strip():
        db.update_v2_lesson_metadata(lesson["id"], title=fallback_title)
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


def _reading_tts_identity(text: str, source_kind: str) -> dict:
    """服务端派生的不可变内容身份：内容哈希 + 可信字数 + 来源类型。
    随 reserve 写入 estimated_usage，重放时逐项比对，不同内容同 key → 409。"""
    return {
        "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "char_count": len(text),
        "source_kind": source_kind,
    }


def _require_same_reading_content(existing: dict, identity: dict) -> None:
    stored = existing.get("estimated_usage") or {}
    for field in ("content_hash", "source_kind"):
        if stored.get(field) and stored[field] != identity[field]:
            raise credit_meter.OperationConflictError(
                "该 Idempotency-Key 已绑定不同的朗读内容，请换用新的 key",
                detail={"code": "content_mismatch"})
    if stored.get("char_count") is not None and stored["char_count"] != identity["char_count"]:
        raise credit_meter.OperationConflictError(
            "该 Idempotency-Key 已绑定不同的朗读内容，请换用新的 key",
            detail={"code": "content_mismatch"})


def _reading_tts_replay_lesson(op: dict) -> dict:
    """settled 重放：经 operation 引用在当前用户库解析原课程；缺失 → 409。"""
    lesson_id = (op or {}).get("reference_id")
    if (op or {}).get("reference_type") != "v2_lesson" or not lesson_id:
        raise credit_meter.OperationConflictError(
            "该操作缺少可重放的课程引用，请换用新的 Idempotency-Key 重新发起",
            detail={"code": "replay_unavailable"})
    lesson = db.get_v2_lesson(int(lesson_id))
    if not lesson:
        raise credit_meter.OperationConflictError(
            "原课程已不存在，请换用新的 Idempotency-Key 重新发起",
            detail={"code": "replay_unavailable"})
    return lesson


def _begin_reading_tts(*, username: str, idempotency_key: str,
                       identity: dict) -> tuple[dict | None, dict | None]:
    """tts=True 且计费激活时的 reading_tts 预授权（Task 8）。

    返回 (op, replay_op)：单用户/不计费返回 (None, None)；
    同 key 同内容已 settled → (None, existing)，调用方直接回放原课程结果，
    不得再建课/排队 TTS；in-flight/released/跨内容复用 → OperationConflictError（409）；
    缺 key → ValueError（400）；余额不足 → InsufficientCredits（402）。
    调用方必须在此之前完成缓存检查，且必须在任何课程/TTS job 落库变更之前调用。
    """
    if not credit_meter.billing_active():
        return None, None
    key = credit_meter.require_idempotency_key(idempotency_key)
    existing = credit_meter.get_operation_by_key(username, key)
    credit_meter.require_operation_identity(existing, operation_type="reading_tts")
    if existing is not None:
        _require_same_reading_content(existing, identity)
        status = existing.get("status")
        if status == "released":
            raise credit_meter.OperationConflictError(
                "该 Idempotency-Key 对应的朗读操作已失败释放，请换用新的 key 重试",
                detail={"code": "key_released"})
        if status in ("reserved", "shadow"):
            raise credit_meter.OperationConflictError(
                "相同 Idempotency-Key 的朗读任务正在进行中，请等待完成",
                detail={"code": "operation_in_flight"})
        return None, existing
    op = credit_meter.reserve(
        username, "reading_tts",
        idempotency_key=key, char_count=identity["char_count"],
        reference_type="v2_lesson", estimated_usage=identity,
    )
    return op, None


def start_reading_text_lesson(title: str, text: str, *, tts: bool = False,
                              username: str = "", idempotency_key: str = "",
                              source_kind: str = "reading_text") -> dict:
    op: dict | None = None
    if tts:
        # char_count/内容哈希均服务端可信（text 原文）；失败时课程/任务均未创建
        op, replay_op = _begin_reading_tts(
            username=username, idempotency_key=idempotency_key,
            identity=_reading_tts_identity(text, source_kind))
        if replay_op is not None:
            # settled 同 key 同内容：回放原课程，不建课、不排队 TTS、不重复扣费
            lesson = _reading_tts_replay_lesson(replay_op)
            return {"lesson": lesson,
                    "blocks": db.get_v2_reading_blocks(int(lesson["id"])),
                    "workspace_url": f"/workspace/{lesson['id']}",
                    "replayed": True}
    try:
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
    except Exception:
        if op is not None:
            credit_meter.release(op["id"], reason="reading lesson creation failed")
        raise
    if tts:
        if op is not None:
            # attach 先于 worker settle：重放永远能经引用解析到课程
            credit_meter.attach_reference(op["id"], "v2_lesson", str(lesson["id"]))
        # use_operation 把 reading_tts op 带进后台 TTS 线程（copy_context），
        # 后台成功 settle_current / 失败 release_current
        with credit_meter.use_operation(op):
            enqueue_reading_tts(lesson["id"])
    lesson = db.get_v2_lesson(lesson["id"]) or lesson
    return {"lesson": lesson, "blocks": imported["blocks"], "workspace_url": f"/workspace/{lesson['id']}"}


def start_reading_pdf_lesson(local_path: str, *, title: str = "", tts: bool = False,
                             username: str = "", idempotency_key: str = "") -> dict:
    pdf_path = _resolve_local_path(local_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"Reading PDF not found: {pdf_path}")
    text = extract_text_from_pdf(pdf_path)
    return start_reading_text_lesson(title=title or pdf_path.stem, text=text, tts=tts,
                                     username=username, idempotency_key=idempotency_key,
                                     source_kind="reading_pdf")


def start_reading_file_lesson(local_path: str, *, tts: bool = False,
                              username: str = "", idempotency_key: str = "") -> dict:
    """服务器本地路径的 txt/md/docx/pdf（如网盘导入产物）：读 bytes 后复用上传解析管线。"""
    file_path = _resolve_local_path(local_path)
    if not file_path.exists():
        raise FileNotFoundError(f"Reading file not found: {file_path}")
    return start_reading_upload_lesson(
        file_path.name, file_path.read_bytes(), tts=tts,
        username=username, idempotency_key=idempotency_key,
    )


def start_reading_upload_lesson(filename: str, content: bytes, *, tts: bool = False,
                                username: str = "", idempotency_key: str = "") -> dict:
    if not content:
        raise ValueError("Uploaded reading file is empty")
    digest = hashlib.sha1(content).hexdigest()[:12]
    source_url = f"upload:{digest}"
    scope = user_assets.current_scope_key()
    cached_lesson = db.get_cached_v2_reading_lesson(source_url)
    if cached_lesson:
        if tts:
            op: dict | None = None
            if not reading_tts_is_cached(cached_lesson["id"]):
                # 缓存命中课程但朗读缺失：身份必须与首次上传一致——
                # 同样从原始文件同步抽取（同一管道，结果确定性相同）
                extracted = extract_text_from_upload(filename, content)
                op, replay_op = _begin_reading_tts(
                    username=username, idempotency_key=idempotency_key,
                    identity=_reading_tts_identity(extracted, "reading_upload"))
                if replay_op is not None:
                    lesson = _reading_tts_replay_lesson(replay_op)
                    return {"cached": True, "replayed": True,
                            "workspace_url": f"/workspace/{lesson['id']}"}
                if op is not None:
                    credit_meter.attach_reference(op["id"], "v2_lesson",
                                                  str(cached_lesson["id"]))
            with credit_meter.use_operation(op):
                enqueue_reading_tts(cached_lesson["id"])
        return {"cached": True, "workspace_url": f"/workspace/{cached_lesson['id']}"}

    preextracted_text: str | None = None
    op = None
    if tts and credit_meter.billing_active():
        # 服务端可信内容身份：同步抽取文本（后台 job 复用，不重复解析）；
        # 预授权/重放判定失败（402/409/400）发生在任何 job 变更之前
        preextracted_text = extract_text_from_upload(filename, content)
        op, replay_op = _begin_reading_tts(
            username=username, idempotency_key=idempotency_key,
            identity=_reading_tts_identity(preextracted_text, "reading_upload"))
        if replay_op is not None:
            # settled 同 key 同内容：回放原课程，不产生第二个 job/课程
            lesson = _reading_tts_replay_lesson(replay_op)
            return {"cached": True, "replayed": True,
                    "workspace_url": f"/workspace/{lesson['id']}"}

    with _READING_UPLOAD_JOBS_LOCK:
        inflight_job_id = _READING_UPLOAD_INFLIGHT.get((scope, source_url))
        if inflight_job_id:
            if op is not None:
                credit_meter.release(op["id"], reason="reading upload already in flight")
            return {"cached": False, "job_id": inflight_job_id, "status": "queued"}
    if not _READING_UPLOAD_SLOTS.acquire(blocking=False):
        if op is not None:
            credit_meter.release(op["id"], reason="reading import service busy")
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
                "scope": scope,
                "stage": "queued",
                "percent": 0,
                "message": "Reading file queued",
                "error": "",
                "workspace_url": "",
            }
            _READING_UPLOAD_INFLIGHT[(scope, source_url)] = job_id
        with credit_meter.use_operation(op):
            thread = db.spawn_with_db_context(
                _run_reading_upload,
                job_id, filename, content, source_url, scope, tts, preextracted_text,
                name=f"reading-import-{job_id[:8]}",
            )
    except Exception:
        if op is not None:
            credit_meter.release(op["id"], reason="reading upload spawn failed")
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
        # 只允许查询当前用户 scope 的 job；其他用户 job_id 一律 404
        if not status or status.get("scope", "") != user_assets.current_scope_key():
            raise ValueError("Reading upload job not found")
        # scope 是内部所有权标记，不外泄到响应 payload
        return {key: value for key, value in status.items() if key != "scope"}


def _process_reading_upload(job_id: str, filename: str, content: bytes, source_url: str,
                            scope: str = "", tts: bool = False,
                            preextracted_text: str | None = None) -> None:
    try:
        _set_reading_upload_job(job_id, stage="parsing", percent=10, message="Extracting text")
        text = (preextracted_text if preextracted_text is not None
                else extract_text_from_upload(filename, content))
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
        op = credit_meter.current_operation()
        if op is not None:
            credit_meter.attach_reference(op["id"], "v2_lesson", str(lesson["id"]))
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
        # 解析/建块/落库失败（TTS 尚未开始）：释放 reading_tts 预授权
        credit_meter.release_current(reason=f"reading import failed: {exc}"[:500])
        _set_reading_upload_job(
            job_id,
            stage="failed",
            message="Reading import failed",
            error=str(exc),
        )
    finally:
        with _READING_UPLOAD_JOBS_LOCK:
            if _READING_UPLOAD_INFLIGHT.get((scope, source_url)) == job_id:
                _READING_UPLOAD_INFLIGHT.pop((scope, source_url), None)


def _run_reading_upload(job_id: str, filename: str, content: bytes, source_url: str,
                        scope: str = "", tts: bool = False,
                        preextracted_text: str | None = None) -> None:
    try:
        _process_reading_upload(job_id, filename, content, source_url, scope, tts,
                                preextracted_text)
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
    segments = _transcribe_with_optional_whisper(
        Path(audio_path), model, output_dir=user_assets.current_output_root(OUTPUT_DIR)
    )
    return [
        {
            "index": i + 1, "start": float(s.start), "end": float(s.end), "text": s.text,
            **({"words": s.words} if getattr(s, "words", None) else {}),
        }
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
        title = (bundle.title or "").strip()
        if title:
            db.update_v2_lesson_metadata(lesson_id, title=title)
        db.set_v2_lesson_status(lesson_id, subtitle_status="ready")
        # 课程 bundle（Task 7）：核心标准=字幕 ready（YouTube 媒体走嵌入播放器）；
        # 此时 settle 父 operation，之后翻译/对齐等附属失败不影响核心成功
        credit_meter.settle_current(actual_usage={"lesson_id": lesson_id,
                                                  "subtitle_segments": len(segments)})
        _enqueue_media_alignment(lesson_id)
        if translate:
            _translate_ancillary(lesson_id)
    except Exception as exc:
        db.set_v2_lesson_status(lesson_id, subtitle_status="failed", subtitle_error=str(exc) or repr(exc))
        credit_meter.release_current(
            reason=f"core subtitle pipeline failed: {exc}"[:500])
        if translate:
            db.update_v2_translation_status(
                lesson_id, status="failed", ready=False,
                error=f"Subtitle generation failed: {exc}",
            )


def _translate_ancillary(lesson_id: int) -> None:
    """课程 bundle 附属翻译：失败只记录状态，绝不让核心字幕成功被改判失败。"""
    try:
        translate_lesson_subtitles(lesson_id)
    except Exception as exc:
        print(f"[v2] lesson {lesson_id}: 附属翻译失败（核心字幕不受影响）：{exc}", flush=True)
        try:
            db.update_v2_translation_status(
                lesson_id, status="failed", ready=False, error=str(exc))
        except Exception:
            pass


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
            output_dir=user_assets.current_output_root(OUTPUT_DIR),
        )
        segments = source_bundle_to_segment_dicts(bundle)
        _store_media_segments(lesson_id, segments)
        db.update_v2_lesson_metadata(
            lesson_id,
            title=bundle.title or media.stem,
            media_url=media_url,
            media_kind=_media_kind(media),
        )
        db.set_v2_lesson_status(lesson_id, subtitle_status="ready")
        # 课程 bundle（Task 7）：核心标准=媒体可播放（media_url）且字幕 ready → settle
        credit_meter.settle_current(actual_usage={"lesson_id": lesson_id,
                                                  "subtitle_segments": len(segments)})
        _enqueue_media_alignment(lesson_id)
        if translate:
            _translate_ancillary(lesson_id)
    except Exception as exc:
        db.set_v2_lesson_status(lesson_id, subtitle_status="failed", subtitle_error=str(exc) or repr(exc))
        credit_meter.release_current(
            reason=f"core subtitle pipeline failed: {exc}"[:500])
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
        segments = source_bundle_to_segment_dicts(bundle)
        _store_media_segments(lesson_id, segments)
        db.update_v2_lesson_metadata(
            lesson_id,
            title=bundle.title or "Bilibili lesson",
            media_url=media_url,
            media_kind=media_kind,
        )
        db.set_v2_lesson_status(lesson_id, subtitle_status="ready")
        # 课程 bundle（Task 7）：核心成功标准同 local → settle；附属失败不影响核心
        credit_meter.settle_current(actual_usage={"lesson_id": lesson_id,
                                                  "subtitle_segments": len(segments)})
        _enqueue_media_alignment(lesson_id)
        if translate:
            _translate_ancillary(lesson_id)
    except Exception as exc:
        db.set_v2_lesson_status(lesson_id, subtitle_status="failed", subtitle_error=str(exc) or repr(exc))
        credit_meter.release_current(
            reason=f"core subtitle pipeline failed: {exc}"[:500])
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
    target_dir = user_assets.user_output_subdir(
        "v2_assets", str(lesson_id), fallback=OUTPUT_DIR / "v2_assets" / str(lesson_id)
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    if not target.exists() or target.stat().st_size != source.stat().st_size:
        shutil.copy2(source, target)
    return f"/output/v2_assets/{lesson_id}/{target.name}"


def get_lesson_capabilities(lesson: dict) -> dict:
    can_listen = bool(
        str(lesson.get("video_id") or "").strip()
        or str(lesson.get("media_url") or "").strip()
        # generated_audio 在 TTS 排队时即写入：生成中也开放精听（加载态）；
        # 失败会清空 media_kind，回到不可精听
        or str(lesson.get("media_kind") or "").strip() == "generated_audio"
    )
    if "reading_block_count" in lesson:
        can_read = int(lesson.get("reading_block_count") or 0) > 0
    else:
        can_read = bool(db.get_v2_reading_blocks(int(lesson["id"])))
    return {"can_listen": can_listen, "can_read": can_read}


def get_available_modes(lesson: dict) -> list[str]:
    caps = get_lesson_capabilities(lesson)
    modes: list[str] = []
    if caps["can_listen"]:
        modes.append("listening")
    if caps["can_read"]:
        modes.append("reading")
    return modes


def get_course_library(include_archived: bool = False) -> list[dict]:
    courses = []
    for lesson in db.list_v2_lessons(include_archived=include_archived):
        lesson_id = int(lesson["id"])
        duration = float(lesson.get("duration") or 0)
        position = float(lesson.get("last_position_seconds") or 0)
        export_name = f"v2-intensive-{lesson_id}.html"
        export_path = user_assets.current_output_root(OUTPUT_DIR) / export_name
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
            "capabilities": get_lesson_capabilities(lesson),
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
    return {"lesson": lesson, "available_modes": get_available_modes(lesson), "capabilities": get_lesson_capabilities(lesson), "progress": {
        "last_position_seconds": progress.get("last_position_seconds", 0),
        "last_segment_index": progress.get("last_segment_index", 0),
    }}
