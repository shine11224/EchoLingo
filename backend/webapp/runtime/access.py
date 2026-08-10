"""多用户模式管理员权限 guard。

只信任 AuthMiddleware 写入 request.scope 的 ``elt_is_admin``，不信任任何
请求参数、Cookie 自报字段或前端隐藏状态。单用户/公开库模式
（ELT_AUTH_ENABLED != "1" 或无认证中间件）静默放行，保持原有能力。
"""
from __future__ import annotations

import os

from fastapi import HTTPException, Request

# 管理员专属 API 路径：多用户模式下未登录访客也不应感知其存在。
# AuthMiddleware 对这些路径不短路 401，放行给路由 guard 统一返回 404。
_ADMIN_ONLY_API_PREFIXES = (
    "/api/jobs",
    "/api/logs",
    "/api/browse-file",
    "/api/download-audio",
    "/api/generate",  # 含 /api/generate/status/{id} 与 /api/generate/cancel/{id}
    "/api/credits/admin",  # 积分管理员接口：未登录/非管理员统一 404
)


def is_admin_only_path(path: str) -> bool:
    return any(
        path == p or path.startswith(p + "/")
        for p in _ADMIN_ONLY_API_PREFIXES
    )


def multiuser_enabled() -> bool:
    """运行模式唯一权威判断：多用户认证中间件实际生效才为 True。

    公开库没有 webapp.auth 模块时，即使误设 ELT_AUTH_ENABLED=1 也算单用户，
    /health 与 require_admin() 都必须以本函数为准，不得各自实现。
    """
    if os.environ.get("ELT_AUTH_ENABLED") != "1":
        return False
    try:
        import webapp.auth.middleware  # noqa: F401
    except ImportError:
        return False
    return True


def is_admin_request(request: Request) -> bool:
    return bool(request.scope.get("elt_is_admin"))


def require_admin(request: Request) -> None:
    """多用户模式下非管理员/未登录一律 404；单用户模式放行。

    404 而非 403：普通用户不应感知私有入口的存在。
    """
    if not multiuser_enabled():
        return
    if not is_admin_request(request):
        raise HTTPException(status_code=404, detail="Not found")
