"""Phase 3A — lesson metadata and study-session endpoints migrated from Flask."""

from __future__ import annotations

import datetime
import shutil
from pathlib import Path
from typing import Any, Optional

import db
from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from webapp.constants import ERROR_CATALOG
from webapp.storage.lessons import clear_lessons_cache, resolve_output_html

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


class PatchLessonBody(BaseModel):
    title: Optional[str] = None
    archived: Optional[bool] = None


class StudySessionBody(BaseModel):
    lesson_filename: str = ""
    current_sentence_idx: int = 0
    total_sentences: int = 0


@router.patch("/api/lessons/{filename:path}")
def api_patch_lesson(filename: str, body: PatchLessonBody = Body(default=PatchLessonBody())):
    safe = Path(filename).name
    if not safe or Path(safe).suffix.lower() != ".html":
        return _error("UNKNOWN_ERROR", 400, "invalid filename")
    if body.title is not None:
        db.rename_lesson(safe, str(body.title)[:200])
    if body.archived is not None:
        db.set_lesson_archived(safe, bool(body.archived))
    return {"ok": True}


@router.delete("/api/lessons/{filename:path}")
def api_delete_lesson(filename: str):
    html_path = resolve_output_html(filename)
    if not html_path:
        return _error("UNKNOWN_ERROR", 404, "lesson not found")

    deleted = [html_path.name]
    asset_dir = html_path.with_name(f"{html_path.stem}_assets")
    html_path.unlink()
    if asset_dir.is_dir():
        shutil.rmtree(asset_dir)
        deleted.append(asset_dir.name)

    clear_lessons_cache()
    db.delete_lesson_meta(filename)
    return {"ok": True, "deleted": deleted}


@router.get("/api/study-session/{filename:path}")
def api_get_study_session(filename: str):
    session = db.get_study_session(Path(filename).name)
    return session or {}


@router.post("/api/study-session")
def api_upsert_study_session(body: StudySessionBody = Body(default=StudySessionBody())):
    fn = Path(body.lesson_filename).name
    if not fn:
        return _error("URL_REQUIRED", 400, "lesson_filename required")
    db.upsert_study_session(
        fn,
        body.current_sentence_idx,
        body.total_sentences,
        datetime.date.today().isoformat(),
    )
    return {"ok": True}
