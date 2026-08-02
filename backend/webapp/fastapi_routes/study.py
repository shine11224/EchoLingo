"""Phase 3B — sentence marks, reflections, and practice attempts."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional

import db
from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from webapp.constants import ERROR_CATALOG

router = APIRouter()


def _error(code: str, status: int, message: str | None = None) -> JSONResponse:
    title, action = ERROR_CATALOG.get(code, ERROR_CATALOG["UNKNOWN_ERROR"])
    payload = {
        "code": code,
        "title": title,
        "message": message or title,
        "action": action,
        "step": None,
        "detail": None,
    }
    return JSONResponse({"error": payload["message"], "error_info": payload}, status_code=status)


class SentenceMarkBody(BaseModel):
    lesson_filename: str = ""
    sentence_idx: Optional[int] = None
    marked: Optional[bool] = None


class ReflectionBody(BaseModel):
    filename: str = ""
    reflection: str = ""


class PracticeAttemptBody(BaseModel):
    filename: str = ""
    sentence_idx: Optional[int] = None
    user_input: str = ""
    ai_feedback: str = ""


@router.get("/api/sentence-marks/{filename:path}")
def api_get_sentence_marks(filename: str):
    return db.get_sentence_marks(Path(filename).name)


@router.post("/api/sentence-mark")
def api_toggle_sentence_mark(body: SentenceMarkBody = Body(default=SentenceMarkBody())):
    fn = Path(body.lesson_filename).name
    idx = body.sentence_idx
    if not fn or idx is None:
        return _error("URL_REQUIRED", 400, "lesson_filename and sentence_idx required")
    now = datetime.date.today().isoformat()
    if body.marked is not None:
        marked = db.set_sentence_mark(fn, int(idx), bool(body.marked), now)
    else:
        marked = db.toggle_sentence_mark(fn, int(idx), now)
    return {"ok": True, "marked": marked}


@router.get("/api/lesson-reflection/{filename:path}")
def api_get_reflection(filename: str):
    text = db.get_latest_reflection(Path(filename).name)
    return {"reflection": text or ""}


@router.post("/api/lesson-reflection")
def api_save_reflection(body: ReflectionBody = Body(default=ReflectionBody())):
    fn = Path(body.filename).name
    if not fn:
        return _error("URL_REQUIRED", 400, "filename required")
    db.save_reflection(fn, body.reflection.strip(), datetime.date.today().isoformat())
    return {"ok": True}


@router.get("/api/practice-attempts/{rest:path}")
def api_get_practice_attempts(rest: str):
    # FastAPI path type is greedy, so we capture "filename/sentence_idx" as one string
    # and split on the last "/" to recover both parts — same behaviour as Flask's
    # <path:filename>/<int:sentence_idx>.
    parts = rest.rsplit("/", 1)
    if len(parts) != 2:
        return JSONResponse({"error": "invalid path"}, status_code=404)
    filename, idx_str = parts
    try:
        sentence_idx = int(idx_str)
    except ValueError:
        return JSONResponse({"error": "sentence_idx must be an integer"}, status_code=422)
    return db.get_practice_attempts(Path(filename).name, sentence_idx)


@router.post("/api/practice-attempt")
def api_save_practice_attempt(body: PracticeAttemptBody = Body(default=PracticeAttemptBody())):
    fn = Path(body.filename).name
    idx = body.sentence_idx
    if not fn or idx is None:
        return _error("URL_REQUIRED", 400, "filename and sentence_idx required")
    db.save_practice_attempt(
        fn, int(idx),
        str(body.user_input),
        str(body.ai_feedback),
        datetime.date.today().isoformat(),
    )
    return {"ok": True}
