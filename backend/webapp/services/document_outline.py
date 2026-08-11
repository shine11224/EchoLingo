"""Generate and cache an AI-assisted document outline with stable anchors."""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from datetime import datetime, timezone

import db
from prompts import DOCUMENT_OUTLINE_PROMPT
from webapp.runtime import ai_config
from webapp.runtime import credit_meter

_OUTLINE_JOBS: dict[int, dict] = {}
_OUTLINE_JOBS_LOCK = threading.Lock()


def _clip(text: str, limit: int = 520) -> str:
    normalized = " ".join((text or "").split())
    return normalized if len(normalized) <= limit else normalized[: limit - 1].rstrip() + "…"


def _reading_candidates(lesson_id: int) -> list[dict]:
    blocks = [
        block
        for block in db.get_v2_reading_blocks(lesson_id)
        if str(block.get("text", "")).strip()
    ]
    group_size = max(1, math.ceil(len(blocks) / 36))
    candidates = []
    for offset in range(0, len(blocks), group_size):
        group = blocks[offset : offset + group_size]
        candidates.append(
            {
                "anchor_id": int(group[0]["index"]),
                "anchor_type": "block",
                "text": _clip(" ".join(str(block.get("text", "")) for block in group)),
            }
        )
    return candidates


def _media_candidates(lesson_id: int) -> list[dict]:
    segments = db.get_v2_subtitle_segments(lesson_id)
    group_size = max(1, math.ceil(len(segments) / 36))
    candidates = []
    for offset in range(0, len(segments), group_size):
        group = segments[offset : offset + group_size]
        if not group:
            continue
        start = float(group[0].get("start_seconds", group[0].get("start", 0)) or 0)
        text = " ".join(str(item.get("text", "")) for item in group)
        candidates.append(
            {"anchor_id": round(start, 3), "anchor_type": "time", "text": _clip(text)}
        )
    return candidates


def _candidates(lesson: dict) -> list[dict]:
    media_source = str(lesson.get("source_type") or "") in {
        "youtube",
        "bilibili",
        "local_audio",
        "local_video",
    }
    return _media_candidates(int(lesson["id"])) if media_source else _reading_candidates(int(lesson["id"]))


def _parse_json(content: str) -> dict:
    text = (content or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    return json.loads(text)


def _validated_outline(raw: dict, lesson: dict, candidates: list[dict]) -> dict:
    candidate_map = {str(item["anchor_id"]): item for item in candidates}
    sections = []
    for item in raw.get("sections", []) if isinstance(raw, dict) else []:
        anchor = candidate_map.get(str(item.get("anchor_id")))
        if not anchor:
            continue
        sections.append(
            {
                "anchor_id": anchor["anchor_id"],
                "anchor_type": anchor["anchor_type"],
                "title": _clip(str(item.get("title") or "未命名部分"), 32),
                "description": _clip(str(item.get("description") or anchor["text"]), 150),
            }
        )
    if not sections:
        sections = [
            {
                "anchor_id": item["anchor_id"],
                "anchor_type": item["anchor_type"],
                "title": f"第 {index + 1} 部分",
                "description": _clip(item["text"], 150),
            }
            for index, item in enumerate(candidates[:12])
        ]
    return {
        "summary": _clip(str(raw.get("summary") or f"{lesson.get('title') or '本课程'}的文档结构摘要。"), 600),
        "document_type": _clip(str(raw.get("document_type") or "document"), 40),
        "sections": sections,
    }


def _outline_context(lesson_id: int) -> tuple[dict, list[dict], str, str]:
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise ValueError("Lesson not found")
    candidates = _candidates(lesson)
    if not candidates:
        raise ValueError("No document content is available for outlining")
    source_json = json.dumps(
        {
            "duration_seconds": round(float(lesson.get("duration") or 0), 3),
            "candidates": candidates,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    content_hash = hashlib.sha1(
        f"{DOCUMENT_OUTLINE_PROMPT}\n{source_json}".encode("utf-8")
    ).hexdigest()
    return lesson, candidates, source_json, content_hash


def generate_document_outline(lesson_id: int, *, force: bool = False) -> dict:
    lesson, candidates, source_json, content_hash = _outline_context(lesson_id)
    if not force:
        cached = db.get_v2_document_outline(lesson_id, content_hash)
        if cached:
            return {"outline": cached["outline"], "cached": True, "updated_at": cached["updated_at"]}
    if not ai_config.AI_API_KEY:
        raise RuntimeError("AI API key not configured")

    response = ai_config.client.with_options(
        timeout=180,
        max_retries=0,
    ).chat.completions.create(
        model=ai_config.AI_MODEL,
        temperature=0.2,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "user",
                "content": DOCUMENT_OUTLINE_PROMPT.format(document_json=source_json),
            }
        ],
    )
    try:
        raw = _parse_json(response.choices[0].message.content or "")
    except (AttributeError, IndexError, TypeError, json.JSONDecodeError):
        raw = {}
    outline = _validated_outline(raw, lesson, candidates)
    saved = db.save_v2_document_outline(lesson_id, content_hash, outline)
    return {"outline": saved["outline"], "cached": False, "updated_at": saved["updated_at"]}


def get_document_outline_status(lesson_id: int) -> dict:
    _lesson, _candidates, _source_json, content_hash = _outline_context(lesson_id)
    with _OUTLINE_JOBS_LOCK:
        job = dict(_OUTLINE_JOBS.get(lesson_id) or {})
    if (
        job.get("content_hash") == content_hash
        and job.get("status") in {"pending", "ready", "error"}
    ):
        return job
    cached = db.get_v2_document_outline(lesson_id, content_hash)
    if cached:
        return {
            "status": "ready",
            "outline": cached["outline"],
            "cached": True,
            "updated_at": cached["updated_at"],
            "content_hash": content_hash,
        }
    return {"status": "idle"}


def _run_document_outline_job(lesson_id: int, force: bool, content_hash: str) -> None:
    try:
        result = generate_document_outline(lesson_id, force=force)
        state = {"status": "ready", "content_hash": content_hash, **result}
        # Task 7：force 重生成是独立计费 operation，成功 settle；无上下文时 no-op
        credit_meter.settle_current(actual_usage={"lesson_id": int(lesson_id)})
    except Exception as exc:
        credit_meter.release_current(
            reason=f"outline regenerate failed: {exc}"[:500])
        state = {
            "status": "error",
            "content_hash": content_hash,
            "error": str(exc) or exc.__class__.__name__,
        }
    with _OUTLINE_JOBS_LOCK:
        current = _OUTLINE_JOBS.get(lesson_id) or {}
        if current.get("content_hash") == content_hash:
            _OUTLINE_JOBS[lesson_id] = state


def start_document_outline_generation(lesson_id: int, *, force: bool = False) -> dict:
    _lesson, _candidates, _source_json, content_hash = _outline_context(lesson_id)
    with _OUTLINE_JOBS_LOCK:
        current = _OUTLINE_JOBS.get(lesson_id) or {}
        if (
            current.get("content_hash") == content_hash
            and current.get("status") == "pending"
        ):
            return dict(current)
        if not force:
            cached = db.get_v2_document_outline(lesson_id, content_hash)
            if cached:
                ready = {
                    "status": "ready",
                    "outline": cached["outline"],
                    "cached": True,
                    "updated_at": cached["updated_at"],
                    "content_hash": content_hash,
                }
                _OUTLINE_JOBS[lesson_id] = ready
                return ready
        pending = {
            "status": "pending",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "content_hash": content_hash,
        }
        _OUTLINE_JOBS[lesson_id] = pending
    db.spawn_with_db_context(
        _run_document_outline_job, lesson_id, force, content_hash,
        name=f"outline-{lesson_id}",
    )
    return pending
