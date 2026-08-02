"""V2 chat routes."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

import db
from webapp.runtime import ai_config
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
def chat(body: ChatBody):
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
        raise HTTPException(status_code=503, detail="AI API key not configured")

    try:
        resp = ai_config.client.chat.completions.create(
            model=ai_config.AI_MODEL,
            timeout=45,
            messages=[
                {"role": "system", "content": "You are an English learning assistant. Help the user understand video or reading lesson content. Reply in Chinese."},
                {"role": "user", "content": ctx["prompt"]},
            ],
        )
        ai_response = resp.choices[0].message.content or ""
    except Exception as e:
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

    return {"message": saved, "context": ctx}


@router.get("/chat/history/{lesson_id}")
def chat_history(lesson_id: int, session_id: int, limit: int = 50):
    session = db.get_v2_chat_session(session_id)
    if not session or session["lesson_id"] != lesson_id:
        raise HTTPException(status_code=404, detail="Chat session not found")
    history = db.get_v2_chat_history(lesson_id, session_id=session_id, limit=limit)
    return {"lesson_id": lesson_id, "session_id": session_id, "messages": history}
