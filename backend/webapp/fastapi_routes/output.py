"""Phase 5C — output file serving and lesson rerender endpoints."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from webapp.storage.lessons import OUTPUT_DIR, extract_js_var

router = APIRouter()

_TEMPLATE_DIR = OUTPUT_DIR.parent / "frontend" / "templates"
_V2_INTENSIVE_EXPORT_RE = re.compile(r"^v2-intensive-(\d+)\.html$")


def _safe_output_path(filename: str) -> Path | None:
    """Resolve filename inside OUTPUT_DIR; return None on traversal or missing file."""
    if not filename or "\x00" in filename:
        return None
    candidate = (OUTPUT_DIR / filename).resolve()
    try:
        candidate.relative_to(OUTPUT_DIR.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


@router.get("/output/{filename:path}")
def output_file(filename: str, download: bool = False):
    intensive_match = _V2_INTENSIVE_EXPORT_RE.fullmatch(filename)
    if intensive_match and not download:
        return RedirectResponse(
            url=f"/workspace/{intensive_match.group(1)}/intensive",
            status_code=307,
            headers={"Cache-Control": "no-store"},
        )
    path = _safe_output_path(filename)
    if path is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if intensive_match:
        return FileResponse(
            str(path),
            media_type="text/html",
            filename=filename,
            headers={"Cache-Control": "no-store"},
        )
    return FileResponse(str(path))


@router.get("/rerender/{filename:path}")
def rerender_file(filename: str):
    html_path = _safe_output_path(filename)
    if html_path is None or html_path.suffix.lower() != ".html":
        return Response(f"File not found: {filename}", status_code=404, media_type="text/plain")

    raw = html_path.read_text(encoding="utf-8")
    segments_json = extract_js_var(raw, "segments")
    analyses_json = extract_js_var(raw, "analyses")
    source_type   = extract_js_var(raw, "sourceType")
    lesson_title  = extract_js_var(raw, "lessonTitle")

    if segments_json is None or analyses_json is None:
        return Response("Could not extract data from HTML file", status_code=500, media_type="text/plain")

    youtube_id_m = re.search(r'const youtubeId\s*=\s*["\']([^"\']+)["\']', raw)
    youtube_id   = youtube_id_m.group(1) if youtube_id_m else None

    src_m = re.search(r'<source\s+src="([^"]+)"\s+type="(?:audio|video)/', raw)
    local_video = src_m.group(1) if src_m else None
    if local_video and not local_video.startswith("/") and not local_video.startswith("http"):
        local_video = f"/output/{local_video}"

    loop_m     = re.search(r"const loopCount\s*=\s*(\d+)", raw)
    loop_count = int(loop_m.group(1)) if loop_m else 3

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    rendered = env.get_template("lesson.html").render(
        lesson_title=lesson_title or filename,
        source_type=source_type or "local_video",
        youtube_id=youtube_id,
        local_video_filename=local_video,
        article_url=None,
        loop_count=loop_count,
        analyses_json=analyses_json,
        segments_json=segments_json,
    )
    return HTMLResponse(content=rendered, status_code=200)
