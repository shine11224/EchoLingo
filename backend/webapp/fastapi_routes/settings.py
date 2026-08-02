"""Phase 7A native FastAPI settings endpoints migrated from Flask."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from webapp.runtime import ai_config

router = APIRouter()


async def _parse_body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@router.get("/api/settings/ai")
def get_ai_settings():
    return {
        "api_key": ai_config.AI_API_KEY,
        "base_url": ai_config.AI_BASE_URL,
        "model": ai_config.AI_MODEL,
        "groq_api_key": ai_config.GROQ_API_KEY,
    }


@router.post("/api/settings/ai")
async def save_ai_settings(request: Request):
    data = await _parse_body(request)
    new_key = data.get("api_key", "").strip()
    new_url = data.get("base_url", "").strip()
    new_model = data.get("model", "").strip()
    new_groq_key = data.get("groq_api_key", "").strip()

    ai_config.save_settings(new_key, new_url, new_model, new_groq_key)

    return {"ok": True, "message": "设置已保存"}


@router.delete("/api/settings/ai")
async def delete_ai_settings(request: Request):
    data = await _parse_body(request)
    field = data.get("field", "")

    defaults = {
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "groq_api_key": "",
    }
    if field not in defaults:
        return JSONResponse({"ok": False, "message": f"未知字段: {field}"}, status_code=400)

    ai_config.delete_setting(field)
    return {"ok": True, "message": f"{field} 已清空"}
