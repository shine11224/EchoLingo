"""FastAPI entrypoint for EchoLingo."""

from __future__ import annotations

import os
import sys
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
    """服务重启后继续被中断的句子翻译（状态停留在 translating 的课程）。"""
    try:
        import threading

        from webapp.services.v2_translation import translate_lesson_subtitles

        for lesson in db.list_v2_lessons():
            if str(lesson.get("translation_status") or "") != "translating":
                continue
            if not int(lesson.get("translation_requested") or 0):
                continue
            threading.Thread(
                target=translate_lesson_subtitles,
                args=(int(lesson["id"]),),
                daemon=True,
                name=f"resume-translate-{lesson['id']}",
            ).start()
    except Exception:
        pass


def create_app() -> FastAPI:
    db.init_db()
    migrate_lessons_to_db()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _resume_interrupted_translations()
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
    app.mount(
        "/static",
        StaticFiles(directory=str(ai_config.BASE_DIR / "frontend" / "static")),
        name="static",
    )

    # Phase 2–5: native FastAPI routes registered before Flask fallback
    app.include_router(basics_router)
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
