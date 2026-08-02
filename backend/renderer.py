from __future__ import annotations

import hashlib
import shutil
from dataclasses import asdict
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from schemas import SentenceAnalysis, SourceBundle


class LessonRenderer:
    def __init__(self, template_dir: Path) -> None:
        self.env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            autoescape=select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render(
        self,
        source: SourceBundle,
        analyses: list[SentenceAnalysis],
        output_dir: Path,
        loop_count: int = 3,
    ) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        slug = self._slugify(source.title)
        html_path = output_dir / f"{slug}.html"
        asset_video = None
        if source.local_video:
            asset_dir = output_dir / f"{slug}_assets"
            asset_dir.mkdir(parents=True, exist_ok=True)
            copied = asset_dir / source.local_video.name
            if copied.resolve() != source.local_video.resolve():
                shutil.copy2(source.local_video, copied)
            asset_video = f"{asset_dir.name}/{copied.name}"

        template = self.env.get_template("lesson.html")
        html = template.render(
            lesson_title=source.title,
            source_type=source.source_type,
            youtube_id=source.youtube_id,
            local_video_filename=asset_video,
            article_url=source.article_url,
            loop_count=loop_count,
            analyses_json=[asdict(item) for item in analyses],
            segments_json=[asdict(segment) for segment in source.segments],
        )
        html_path.write_text(html, encoding="utf-8")
        return html_path

    @staticmethod
    def _slugify(value: str) -> str:
        safe = "".join(ch.lower() if ch.isascii() and ch.isalnum() else "-" for ch in value).strip("-")
        safe = "-".join(part for part in safe.split("-") if part)
        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
        base = safe[:48] if safe else "lesson"
        return f"{base}-{digest}"
