"""Persist a v2 intensive workspace as an explicitly downloaded offline snapshot."""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

import db
from webapp.services.v2_intensive import build_intensive_document
from webapp.storage.lessons import BASE_DIR, OUTPUT_DIR


TEMPLATE_DIR = BASE_DIR / "frontend" / "templates"


def export_intensive_html(lesson_id: int) -> dict:
    document = build_intensive_document(lesson_id)
    lesson = document["lesson"]
    title = str(lesson.get("title") or f"Course {lesson_id}").strip()
    export_title = title if title.endswith("· 精读") else f"{title} · 精读"
    filename = f"v2-intensive-{lesson_id}.html"

    environment = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(("html", "xml")),
    )
    html = environment.get_template("intensive.html").render(lesson_id=lesson_id)

    output_dir = Path(OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / filename
    output_path.write_text(html, encoding="utf-8")

    db.delete_lesson_meta(filename)
    return {
        "ok": True,
        "lesson_id": lesson_id,
        "filename": filename,
        "workspace_url": f"/workspace/{lesson_id}/intensive",
        "export_url": f"/output/{filename}?download=1",
        "download_url": f"/output/{filename}?download=1",
        "sentence_count": len(document["sentences"]),
        "title": export_title,
    }
