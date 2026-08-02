"""Phase 2 native FastAPI routes — read-only endpoints migrated from Flask."""

from __future__ import annotations

import re

import db
from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse
from webapp.constants import ERROR_CATALOG, PIPELINE_SCHEMA
from webapp.services.today import get_today_tasks
from webapp.storage.lessons import scan_lessons
import webapp.storage.wordlists as wl_storage

router = APIRouter()


@router.get("/api/lessons")
def api_lessons(include_archived: str = "0"):
    db_lessons = db.get_lessons(include_archived=include_archived == "1")
    lessons = db_lessons if db_lessons else scan_lessons()
    return [
        lesson for lesson in lessons
        if lesson.get("source_type") != "intensive"
        and not str(lesson.get("filename") or "").startswith("v2-intensive-")
    ]


@router.get("/api/today-tasks")
def api_today_tasks():
    return get_today_tasks()


@router.get("/api/pipeline/schema")
def api_pipeline_schema():
    return {"steps": PIPELINE_SCHEMA, "errors": ERROR_CATALOG}


@router.get("/api/wordlists/config")
def api_wordlists_config():
    return wl_storage.scan_wordlists()


@router.get("/api/resources")
def api_resources():
    return wl_storage.list_uploaded_resources()


@router.get("/wordlists/{name}")
def serve_wordlist(name: str):
    if name == "sentence_patterns":
        return wl_storage.get_combined_patterns()
    safe = re.sub(r"[^a-z0-9_]", "", name)
    path = wl_storage.COMPILED_DIR / (safe + ".json")
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(path), media_type="application/json")
