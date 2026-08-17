"""Phase 2 native FastAPI routes — read-only endpoints migrated from Flask."""

from __future__ import annotations

import json
import re

import db
from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
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


def _virtual_wordlist_configs() -> list[dict]:
    """Dynamic wordlists backed by the vocab book: active words and mastered words."""
    try:
        active = db.get_review_word_set()
        mastered = db.get_mastered_review_targets() | db.get_known_words()
    except Exception:
        return []
    return [
        {
            "id": "my_vocab",
            "name": "我的生词本",
            "type": "domain",
            "key": "my_vocab",
            "color": "domain-user",
            "tag": "生词本",
            "builtin": True,
            "virtual": True,
            "count": len(active),
        },
        {
            "id": "my_mastered",
            "name": "已掌握词",
            "type": "exclude",
            "key": "my_mastered",
            "tag": "已掌握",
            "builtin": True,
            "virtual": True,
            "count": len(mastered),
        },
    ]


@router.get("/api/wordlists/config")
def api_wordlists_config():
    return wl_storage.scan_wordlists() + _virtual_wordlist_configs()


class WordlistMembershipRequest(BaseModel):
    words: list[str] = Field(default_factory=list, max_length=1000)


@router.post("/api/wordlists/membership")
def api_wordlists_membership(payload: WordlistMembershipRequest):
    """Return list membership for the small set of words visible in the vocab book."""
    requested = list(dict.fromkeys(
        word.strip().lower()
        for word in payload.words
        if isinstance(word, str) and word.strip()
    ))
    memberships: dict[str, list[str]] = {word: [] for word in requested}
    requested_set = set(requested)

    for config in wl_storage.scan_wordlists():
        stem = str(config.get("id") or config.get("key") or "")
        key = str(config.get("key") or stem)
        if not stem or not key:
            continue
        path = wl_storage.resolve_compiled_path(stem)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        member_words = {
            str(word).strip().lower()
            for word in data.get("words", [])
            if str(word).strip()
        }
        for word in requested_set & member_words:
            memberships[word].append(key)

    try:
        active = {str(word).strip().lower() for word in db.get_review_word_set()}
        mastered = {
            str(word).strip().lower()
            for word in (db.get_mastered_review_targets() | db.get_known_words())
        }
    except Exception:
        active, mastered = set(), set()
    for word in requested:
        if word in active:
            memberships[word].append("my_vocab")
        if word in mastered:
            memberships[word].append("my_mastered")

    return {"memberships": memberships}


@router.get("/api/resources")
def api_resources():
    return wl_storage.list_uploaded_resources()


@router.get("/wordlists/{name}")
def serve_wordlist(name: str):
    if name == "sentence_patterns":
        return wl_storage.get_combined_patterns()
    if name == "my_vocab":
        return {"words": sorted(db.get_review_word_set())}
    if name == "my_mastered":
        return {"words": sorted(db.get_mastered_review_targets() | db.get_known_words())}
    safe = re.sub(r"[^a-z0-9_]", "", name)
    path = wl_storage.resolve_compiled_path(safe)
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(path), media_type="application/json")
