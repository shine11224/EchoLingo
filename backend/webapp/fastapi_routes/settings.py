"""Phase 7A native FastAPI settings endpoints migrated from Flask."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from webapp.runtime import ai_config

router = APIRouter()


def _admin_check(request: Request):
    """AI 设置仅管理员可读写。多用户模式（中间件注入用户名）下校验 is_admin；
    单用户/公开库（无注入或无 auth 模块）放行。未登录的 401 由中间件兜底。"""
    username = request.scope.get("elt_username")
    if not username:
        return None
    try:
        from webapp.auth import store
    except ImportError:
        return None
    if store.is_admin(username):
        return None
    return JSONResponse({"detail": "admin only"}, status_code=403)


async def _parse_body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@router.get("/api/settings/ai")
def get_ai_settings(request: Request):
    if (deny := _admin_check(request)) is not None:
        return deny
    import os

    return {
        "api_key": ai_config.AI_API_KEY,
        "base_url": ai_config.AI_BASE_URL,
        "model": ai_config.AI_MODEL,
        "groq_api_key": ai_config.GROQ_API_KEY,
        # 混元翻译 / 千问转录在调用处实时读 os.environ，这里同源返回
        "hy_translate_api_key": os.environ.get("HY_TRANSLATE_API_KEY", ""),
        "hy_translate_model": os.environ.get("HY_TRANSLATE_MODEL", "") or "hy-mt2-plus",
        "dashscope_api_key": os.environ.get("DASHSCOPE_API_KEY", ""),
    }


@router.post("/api/settings/ai")
async def save_ai_settings(request: Request):
    if (deny := _admin_check(request)) is not None:
        return deny
    data = await _parse_body(request)
    new_key = data.get("api_key", "").strip()
    new_url = data.get("base_url", "").strip()
    new_model = data.get("model", "").strip()
    new_groq_key = data.get("groq_api_key", "").strip()
    new_hy_key = data.get("hy_translate_api_key", "").strip()
    new_hy_model = data.get("hy_translate_model", "").strip()
    new_dashscope_key = data.get("dashscope_api_key", "").strip()

    ai_config.save_settings(
        new_key,
        new_url,
        new_model,
        new_groq_key,
        hy_translate_api_key=new_hy_key,
        hy_translate_model=new_hy_model,
        dashscope_api_key=new_dashscope_key,
    )

    return {"ok": True, "message": "设置已保存"}


@router.delete("/api/settings/ai")
async def delete_ai_settings(request: Request):
    if (deny := _admin_check(request)) is not None:
        return deny
    data = await _parse_body(request)
    field = data.get("field", "")

    defaults = {
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "groq_api_key": "",
        "hy_translate_api_key": "",
        "hy_translate_model": "",
        "dashscope_api_key": "",
    }
    if field not in defaults:
        return JSONResponse({"ok": False, "message": f"未知字段: {field}"}, status_code=400)

    ai_config.delete_setting(field)
    return {"ok": True, "message": f"{field} 已清空"}
