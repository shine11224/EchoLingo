"""FastAPI entrypoint for EchoLingo."""

from __future__ import annotations

import os
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

# Ensure backend/ is on sys.path so bare imports (db, webapp, prompts, …)
# resolve whether this module is run directly or imported as backend.fastapi_server.
# This is a transitional shim; remove once all modules use full package imports.
_backend_dir = Path(__file__).parent
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))

import db
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from webapp.fastapi_routes.basics import router as basics_router
from webapp.fastapi_routes.ai import router as ai_router

# 多用户认证仅存在于私有库/云端：公开库不含 webapp.auth 与 auth 路由，导入失败时静默降级为单用户模式
try:
    from webapp.fastapi_routes.auth import router as auth_router
    from webapp.auth.middleware import AuthMiddleware
except ImportError:  # pragma: no cover - 公开库路径
    auth_router = None
    AuthMiddleware = None

# 管理员私有导入页仅私有库/云端提供；公开库缺少模板/路由时静默降级
try:
    from webapp.fastapi_routes.admin_private import router as admin_private_router
except ImportError:  # pragma: no cover - 公开库路径
    admin_private_router = None

# 积分 API 仅私有库/云端提供；公开库缺少 webapp.auth.credits 时静默降级
try:
    from webapp.fastapi_routes.credits import router as credits_router
except ImportError:  # pragma: no cover - 公开库路径
    credits_router = None

from webapp.fastapi_routes.jobs import router as jobs_router
from webapp.fastapi_routes.lessons import router as lessons_router
from webapp.fastapi_routes.misc import router as misc_router
from webapp.fastapi_routes.output import router as output_router
from webapp.fastapi_routes.pages import router as pages_router
from webapp.fastapi_routes.settings import router as settings_router
from webapp.fastapi_routes.study import router as study_router
from webapp.fastapi_routes.vocab import router as vocab_router
from webapp.fastapi_routes.v2_lessons import router as v2_lessons_router
from webapp.fastapi_routes.v2_chat import router as v2_chat_router
from webapp.runtime import ai_config
from webapp.storage.lessons import migrate_lessons_to_db


def _resume_interrupted_translations() -> None:
    """服务重启后继续被中断或因瞬时上游错误失败的句子翻译。

    多用户模式下遍历 resources/users/*/vocab.db 逐一恢复。"""
    import os as _os
    if _os.environ.get("ELT_AUTH_ENABLED") == "1":
        users_root = Path(
            _os.environ.get("ELT_USERS_ROOT", Path(__file__).resolve().parents[1] / "resources" / "users")
        )
        db_paths = list(users_root.glob("*/vocab.db"))
    else:
        db_paths = [db.DB_PATH]
    for path in db_paths:
        token = db.set_current_db_path(path)
        try:
            from webapp.services.hy_translate import is_retryable_translation_error
            from webapp.services.v2_translation import translate_lesson_subtitles, translate_reading_blocks
            for lesson in db.list_v2_lessons():
                status = str(lesson.get("translation_status") or "")
                if not int(lesson.get("translation_requested") or 0):
                    continue
                is_reading = str(lesson.get("source_type") or "").startswith("reading")
                # Reading：翻译与 TTS 解耦并行，pending（未启动）与 translating（被中断）都要恢复；
                # 429/5xx/超时等瞬时错误可安全续跑，逐句缓存会跳过已完成部分；
                # 401/配置错误等永久故障不自动重试，避免每次启动重复失败。
                retryable_failure = (
                    status == "failed"
                    and is_retryable_translation_error(
                        str(lesson.get("translation_error") or "")
                    )
                )
                if (
                    status != "translating"
                    and not (is_reading and status == "pending")
                    and not retryable_failure
                ):
                    continue
                fn = translate_reading_blocks if is_reading else translate_lesson_subtitles
                db.spawn_with_db_context(
                    fn, int(lesson["id"]),
                    name=f"resume-translate-{lesson['id']}")
        except Exception:
            pass
        finally:
            db.reset_current_db_path(token)


def _ensure_builtin_wordlists_async() -> None:
    """启动自愈：内置词表缺失且 ECDICT 可用时后台重建，不阻塞服务启动。"""

    def _run() -> None:
        try:
            import build_ecdict

            result = build_ecdict.ensure_builtin_wordlists()
            if result not in ("present", "no-db"):
                print(f"[startup] builtin wordlists: {result}")
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


def _recover_stuck_reading_tts() -> None:
    """启动自愈：重启杀掉进行中的 Reading TTS 后台线程后，重新合成卡 pending 的课程。"""
    try:
        from webapp.services.v2_tts import recover_stuck_reading_tts

        recovered = recover_stuck_reading_tts()
        if recovered:
            print(f"[startup] recovered {recovered} stuck reading TTS job(s)")
    except Exception:
        pass


def create_app() -> FastAPI:
    db.init_db()
    migrate_lessons_to_db()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _resume_interrupted_translations()
        _ensure_builtin_wordlists_async()
        _recover_stuck_reading_tts()
        yield
        try:
            from webapp.services.hy_translate import stop_local_server

            stop_local_server()
        except ImportError:
            pass

    app = FastAPI(
        title="EchoLingo",
        version=os.environ.get("ELT_VERSION", "0.1.0"),
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    if os.environ.get("ELT_AUTH_ENABLED") == "1" and AuthMiddleware is not None:
        app.add_middleware(AuthMiddleware)
    app.mount(
        "/static",
        StaticFiles(directory=str(ai_config.BASE_DIR / "frontend" / "static")),
        name="static",
    )

    # Phase 2–5: native FastAPI routes registered before Flask fallback
    app.include_router(basics_router)
    if auth_router is not None:
        app.include_router(auth_router)
    app.include_router(lessons_router)
    app.include_router(study_router)
    app.include_router(vocab_router)
    app.include_router(jobs_router)
    app.include_router(output_router)
    app.include_router(ai_router)
    app.include_router(settings_router)
    app.include_router(pages_router)
    app.include_router(misc_router)
    app.include_router(v2_lessons_router)
    app.include_router(v2_chat_router)
    if admin_private_router is not None:
        app.include_router(admin_private_router)
    if credits_router is not None:
        app.include_router(credits_router)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    host = os.environ.get("ELT_HOST", "0.0.0.0")
    port = int(os.environ.get("ELT_PORT", "5173"))
    print(f"EchoLingo FastAPI server starting on http://localhost:{port}")
    if host == "0.0.0.0":
        print(f"LAN/mobile access enabled on http://<this-computer-ip>:{port}")
    key_status = "configured" if ai_config.AI_API_KEY else "NOT SET - set AI_API_KEY env var"
    print(f"AI API Key: {key_status}")
    uvicorn.run(app, host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
