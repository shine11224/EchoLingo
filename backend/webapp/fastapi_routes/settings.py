"""Phase 7A native FastAPI settings endpoints migrated from Flask."""
from __future__ import annotations

import asyncio
import ipaddress
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from webapp.runtime import ai_config
from webapp.runtime.access import multiuser_enabled
from webapp.services import baidu_pan, local_translation_setup

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
        # 混元翻译在调用处实时读 os.environ，这里同源返回
        "hy_translate_api_key": os.environ.get("HY_TRANSLATE_API_KEY", ""),
        "hy_translate_model": os.environ.get("HY_TRANSLATE_MODEL", "") or "hy-mt2-plus",
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

    ai_config.save_settings(
        new_key,
        new_url,
        new_model,
        new_groq_key,
        hy_translate_api_key=new_hy_key,
        hy_translate_model=new_hy_model,
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
        "model": "deepseek-v4-flash",
        "groq_api_key": "",
        "hy_translate_api_key": "",
        "hy_translate_model": "",
    }
    if field not in defaults:
        return JSONResponse({"ok": False, "message": f"未知字段: {field}"}, status_code=400)

    ai_config.delete_setting(field)
    return {"ok": True, "message": f"{field} 已清空"}


def _baidu_auth_check(request: Request):
    """多用户模式限管理员；公开单用户模式只允许本机回环地址管理授权。"""
    if multiuser_enabled():
        return _admin_check(request)
    host = str(request.client.host if request.client else "")
    if host == "testclient":  # Starlette TestClient 的不可伪造客户端地址
        return None
    try:
        if ipaddress.ip_address(host).is_loopback:
            return None
    except ValueError:
        pass
    return JSONResponse({"detail": "Baidu Drive authorization is local-only"}, status_code=403)


def _loopback_check(request: Request):
    """安装本机可执行文件的接口永远只允许从本机访问。"""
    host = str(request.client.host if request.client else "")
    if host == "testclient":
        return None
    try:
        if ipaddress.ip_address(host).is_loopback:
            return None
    except ValueError:
        pass
    return JSONResponse({"detail": "local installation is loopback-only"}, status_code=403)


@router.get("/api/settings/local-translation")
def get_local_translation_settings(request: Request):
    if (deny := _loopback_check(request)) is not None:
        return deny
    if (deny := _admin_check(request)) is not None:
        return deny
    return local_translation_setup.installer_info()


@router.post("/api/settings/local-translation/install")
async def install_local_translation(request: Request):
    if (deny := _loopback_check(request)) is not None:
        return deny
    if (deny := _admin_check(request)) is not None:
        return deny
    data = await _parse_body(request)
    try:
        return await asyncio.to_thread(
            local_translation_setup.install,
            expected_version=str(data.get("version") or ""),
            accepted_license=data.get("accepted_license") is True,
        )
    except local_translation_setup.LocalTranslationSetupBusyError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=409)
    except (ValueError, local_translation_setup.LocalTranslationSetupError) as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)


# 公开单用户模式提供本机网页授权；多用户模式仍仅管理员可管理。
@router.get("/api/settings/baidu-pan")
def get_baidu_pan_settings(request: Request, refresh: bool = False):
    if (deny := _baidu_auth_check(request)) is not None:
        return deny
    data = baidu_pan.capability(refresh=refresh)
    data["can_manage_auth"] = True
    data["installer"] = baidu_pan.installer_info()
    return data


@router.post("/api/settings/baidu-pan/install")
async def install_baidu_pan(request: Request):
    if (deny := _loopback_check(request)) is not None:
        return deny
    if (deny := _admin_check(request)) is not None:
        return deny
    data = await _parse_body(request)
    try:
        return baidu_pan.install_cli(
            expected_version=str(data.get("version") or ""),
            confirmed=data.get("confirmed") is True,
        )
    except baidu_pan.BaiduPanBusyError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=409)
    except (ValueError, baidu_pan.BaiduPanError) as exc:
        return JSONResponse({"detail": baidu_pan.friendly_message(exc)}, status_code=400)


@router.post("/api/settings/baidu-pan/auth-url")
def begin_baidu_pan_auth(request: Request):
    if (deny := _baidu_auth_check(request)) is not None:
        return deny
    try:
        return baidu_pan.begin_web_auth()
    except (ValueError, baidu_pan.BaiduPanError) as exc:
        return JSONResponse({"detail": baidu_pan.friendly_message(exc)}, status_code=400)


@router.post("/api/settings/baidu-pan/complete")
async def complete_baidu_pan_auth(request: Request):
    if (deny := _baidu_auth_check(request)) is not None:
        return deny
    data = await _parse_body(request)
    try:
        return baidu_pan.complete_web_auth(str(data.get("code") or ""))
    except (ValueError, baidu_pan.BaiduPanError) as exc:
        return JSONResponse({"detail": baidu_pan.friendly_message(exc)}, status_code=400)


@router.delete("/api/settings/baidu-pan")
def delete_baidu_pan_auth(request: Request):
    if (deny := _baidu_auth_check(request)) is not None:
        return deny
    try:
        baidu_pan.logout_web_auth()
        return {"ok": True}
    except (ValueError, baidu_pan.BaiduPanError) as exc:
        return JSONResponse({"detail": baidu_pan.friendly_message(exc)}, status_code=409)
