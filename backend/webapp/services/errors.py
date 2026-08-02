"""Shared error payload helpers independent of Flask."""
from __future__ import annotations

from webapp.constants import ERROR_CATALOG


def error_payload(code: str, message: str | None = None, *, step: str | None = None, detail: str | None = None) -> dict:
    title, action = ERROR_CATALOG.get(code, ERROR_CATALOG["UNKNOWN_ERROR"])
    return {
        "code": code,
        "title": title,
        "message": message or title,
        "action": action,
        "step": step,
        "detail": detail,
    }

