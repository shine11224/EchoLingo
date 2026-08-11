"""V2 chat routes."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

import db
from prompts import LESSON_RAG_SYSTEM_PROMPT
from webapp.runtime import ai_config
from webapp.runtime import credit_meter
from webapp.services import lesson_retrieval
from webapp.services.chat_context import build_chat_context

router = APIRouter(prefix="/api/v2", tags=["v2-chat"])


class ChatBody(BaseModel):
    lesson_id: int
    session_id: int
    message: str
    timestamp_seconds: float = 0
    context_mode: str = "auto"
    selected_start_seconds: float | None = None
    selected_end_seconds: float | None = None
    selected_segment_ids: list[int] | None = None
    selected_text: str | None = None
    allow_external_knowledge: bool = False


class CreateChatSessionBody(BaseModel):
    title: str = ""


@router.get("/chat/sessions/{lesson_id}")
def chat_sessions(lesson_id: int):
    if not db.get_v2_lesson(lesson_id):
        raise HTTPException(status_code=404, detail="Lesson not found")
    return {"sessions": db.list_v2_chat_sessions(lesson_id)}


@router.post("/chat/sessions/{lesson_id}")
def create_chat_session(lesson_id: int, body: CreateChatSessionBody | None = None):
    if not db.get_v2_lesson(lesson_id):
        raise HTTPException(status_code=404, detail="Lesson not found")
    title = body.title.strip() if body else ""
    return {"session": db.create_v2_chat_session(lesson_id, title=title)}


@router.get("/chat/sessions/{lesson_id}/{session_id}/export")
def export_chat_session(lesson_id: int, session_id: int):
    lesson = db.get_v2_lesson(lesson_id)
    session = db.get_v2_chat_session(session_id)
    if not lesson or not session or session["lesson_id"] != lesson_id:
        raise HTTPException(status_code=404, detail="Chat session not found")

    lesson_title = lesson.get("title") or "Untitled lesson"
    session_title = session.get("title") or f"Conversation {session_id}"
    lines = [f"# {lesson_title}", "", f"Conversation: {session_title}", ""]
    for message in db.get_v2_chat_history(lesson_id, session_id=session_id):
        lines.extend(
            [
                "## You",
                "",
                message["user_message"],
                "",
                "## AI",
                "",
                message["ai_response"],
                "",
            ]
        )
    content = "\n".join(lines).rstrip() + "\n"
    return Response(
        content=content,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="chat-session-{session_id}.md"'
        },
    )


@router.post("/chat")
def chat(body: ChatBody, request: Request):
    lesson = db.get_v2_lesson(body.lesson_id)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    session = db.get_v2_chat_session(body.session_id)
    if not session or session["lesson_id"] != body.lesson_id:
        raise HTTPException(status_code=404, detail="Chat session not found")

    selected_text = body.selected_text
    if body.context_mode == "reading_selection":
        selected_text = " ".join((selected_text or "").split())
        if not selected_text:
            raise HTTPException(status_code=400, detail="Reading selection cannot be empty")
        if len(selected_text) > 4000:
            raise HTTPException(
                status_code=400,
                detail="Reading selection cannot exceed 4000 characters",
            )

    # 每次用户问题一笔 lesson_chat（Task 8）；AI 失败/格式失败释放
    try:
        op, replay = credit_meter.begin_sync_operation(
            request, "lesson_chat",
            reference_type="v2_lesson", reference_id=str(body.lesson_id))
    except (credit_meter.InsufficientCredits,
            credit_meter.OperationConflictError, ValueError) as exc:
        status, detail = credit_meter.billing_error(exc)
        raise HTTPException(status_code=status, detail=detail) from exc
    if replay is not None:
        return replay

    if not ai_config.AI_API_KEY:
        credit_meter.release_sync(op, reason="lesson_chat: AI key not configured")
        raise HTTPException(status_code=503, detail="AI API key not configured")

    if body.context_mode in ("auto", "selected_range", "reading_selection"):
        # Task 9/10：单课 RAG（含 reading_selection 选区限定）。章节路由/回答/none 复核
        # 全部 bundle 进同一 operation；选区只在服务端 blocks/sentences 上解析；
        # 格式修复后仍失败或 provider 失败 → release。
        usage_acc = {"model": ai_config.AI_MODEL, "input_tokens": 0,
                     "output_tokens": 0, "ai_calls": 0}

        def call_ai(kind: str, content: str) -> str:
            resp = ai_config.client.chat.completions.create(
                model=ai_config.AI_MODEL,
                timeout=45,
                messages=[
                    {"role": "system", "content": LESSON_RAG_SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
            )
            usage = credit_meter.usage_from_response(resp, model=ai_config.AI_MODEL)
            usage_acc["input_tokens"] += usage.get("input_tokens", 0)
            usage_acc["output_tokens"] += usage.get("output_tokens", 0)
            usage_acc["ai_calls"] += 1
            return resp.choices[0].message.content or ""

        try:
            with credit_meter.use_operation(op):
                result = lesson_retrieval.answer_lesson_question(
                    call_ai,
                    lesson=lesson,
                    question=body.message,
                    history=db.get_v2_chat_history(
                        body.lesson_id, session_id=body.session_id, limit=5),
                    timestamp_seconds=body.timestamp_seconds,
                    selected_segment_ids=(
                        body.selected_segment_ids
                        if body.context_mode == "selected_range" else None),
                    selected_text=(
                        selected_text
                        if body.context_mode == "reading_selection" else None),
                    allow_external=body.allow_external_knowledge,
                )
        except lesson_retrieval.RetrievalFormatError as e:
            credit_meter.release_sync(op, reason=f"lesson_chat format invalid: {e}"[:500])
            raise HTTPException(status_code=502, detail=f"AI output invalid: {e}")
        except Exception as e:
            credit_meter.release_sync(op, reason=f"lesson_chat failed: {e}"[:500])
            raise HTTPException(status_code=502, detail=f"AI request failed: {e}")

        saved = db.save_v2_chat_message(
            lesson_id=body.lesson_id,
            session_id=body.session_id,
            timestamp_seconds=body.timestamp_seconds,
            selected_start_seconds=body.selected_start_seconds,
            selected_end_seconds=body.selected_end_seconds,
            selected_segment_ids=body.selected_segment_ids or [],
            user_message=body.message,
            ai_response=result["answer"],
            context_mode=body.context_mode,
            coverage_status=result["coverage"],
            external_knowledge_used=result["external_knowledge_used"],
            citations=result["citations"],
            unsupported=result["unsupported"],
        )
        payload = {
            "message": saved,
            "context": {
                "context_mode": body.context_mode,
                "unsupported": result["unsupported"],
            },
        }
        usage_acc.update({"lesson_id": body.lesson_id, "session_id": body.session_id,
                          "context_mode": body.context_mode,
                          "coverage": result["coverage"]})
        credit_meter.settle_sync(op, actual_usage=usage_acc, response=payload)
        return payload

    ctx = build_chat_context(
        lesson_id=body.lesson_id,
        session_id=body.session_id,
        message=body.message,
        timestamp_seconds=body.timestamp_seconds,
        context_mode=body.context_mode,
        selected_start_seconds=body.selected_start_seconds,
        selected_end_seconds=body.selected_end_seconds,
        selected_segment_ids=body.selected_segment_ids,
        selected_text=selected_text,
    )

    if not ai_config.AI_API_KEY:
        credit_meter.release_sync(op, reason="lesson_chat: AI key not configured")
        raise HTTPException(status_code=503, detail="AI API key not configured")

    try:
        resp = ai_config.client.chat.completions.create(
            model=ai_config.AI_MODEL,
            timeout=45,
            messages=[
                {"role": "system", "content": "You are an English learning assistant. Help the user understand video or reading lesson content. Reply in Chinese. By default answer only from the current lesson content provided; never fabricate citations, timestamps, or lesson references."},
                {"role": "user", "content": ctx["prompt"]},
            ],
        )
        ai_response = resp.choices[0].message.content or ""
    except Exception as e:
        credit_meter.release_sync(op, reason=f"lesson_chat failed: {e}"[:500])
        raise HTTPException(status_code=502, detail=f"AI request failed: {e}")

    saved = db.save_v2_chat_message(
        lesson_id=body.lesson_id,
        session_id=body.session_id,
        timestamp_seconds=body.timestamp_seconds,
        selected_start_seconds=body.selected_start_seconds,
        selected_end_seconds=body.selected_end_seconds,
        selected_segment_ids=body.selected_segment_ids or [],
        user_message=body.message,
        ai_response=ai_response,
        context_mode=body.context_mode,
    )
    payload = {"message": saved, "context": ctx}
    credit_meter.settle_sync(op, actual_usage=credit_meter.usage_from_response(
        resp, model=ai_config.AI_MODEL,
        extra={"lesson_id": body.lesson_id, "session_id": body.session_id,
               "context_mode": body.context_mode}), response=payload)

    return payload


@router.get("/chat/history/{lesson_id}")
def chat_history(lesson_id: int, session_id: int, limit: int = 50):
    session = db.get_v2_chat_session(session_id)
    if not session or session["lesson_id"] != lesson_id:
        raise HTTPException(status_code=404, detail="Chat session not found")
    history = db.get_v2_chat_history(lesson_id, session_id=session_id, limit=limit)
    return {"lesson_id": lesson_id, "session_id": session_id, "messages": history}
