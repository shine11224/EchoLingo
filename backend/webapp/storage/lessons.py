import datetime
import json
import re
from pathlib import Path

from db import get_lessons, upsert_lesson


BASE_DIR = Path(__file__).resolve().parents[3]
OUTPUT_DIR = BASE_DIR / "output"

_lessons_cache: dict = {"mtime": 0.0, "data": []}


def extract_js_var(raw: str, varname: str):
    m = re.search(rf'const {varname}\s*=\s*', raw)
    if not m:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(raw, m.end())
        return value
    except Exception:
        sm = re.search(rf'const {varname}\s*=\s*["\']([^"\']*)["\']', raw)
        return sm.group(1) if sm else None


def scan_lessons() -> list:
    html_files = sorted(
        (path for path in OUTPUT_DIR.glob("*.html") if not path.name.startswith("v2-intensive-")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not html_files:
        return []
    latest_mtime = max(p.stat().st_mtime for p in html_files)
    if latest_mtime <= _lessons_cache["mtime"]:
        return _lessons_cache["data"]

    lessons = []
    for html_path in html_files:
        try:
            raw = html_path.read_text(encoding="utf-8")
            title = extract_js_var(raw, "lessonTitle") or html_path.stem
            source_type = extract_js_var(raw, "sourceType") or "local_video"
            segments = extract_js_var(raw, "segments") or []
            duration = int(segments[-1]["end"]) if segments else 0
            lessons.append({
                "filename": html_path.name,
                "title": title,
                "source_type": source_type,
                "sentence_count": len(segments),
                "duration": duration,
                "created_at": datetime.datetime.fromtimestamp(
                    html_path.stat().st_mtime
                ).strftime("%Y-%m-%d"),
            })
        except Exception:
            lessons.append({
                "filename": html_path.name,
                "title": html_path.stem,
                "source_type": "local_video",
                "sentence_count": 0,
                "duration": 0,
                "created_at": "-",
            })

    _lessons_cache["mtime"] = latest_mtime
    _lessons_cache["data"] = lessons
    return lessons


def write_lesson_meta(filename: str, job: dict) -> None:
    try:
        html_path = OUTPUT_DIR / filename
        raw = html_path.read_text(encoding="utf-8")
        title = extract_js_var(raw, "lessonTitle") or filename
        source_type = extract_js_var(raw, "sourceType") or "local_video"
        segs = extract_js_var(raw, "segments") or []
        sentence_count = len(segs)
        duration = int(segs[-1]["end"]) if segs else 0
        upsert_lesson(
            filename=filename,
            title=title,
            source_type=source_type,
            source_url=job.get("url", ""),
            sentence_count=sentence_count,
            duration=duration,
            created_at=datetime.date.today().isoformat(),
        )
    except Exception as e:
        print(f"[meta] warn: could not write lesson meta for {filename}: {e}")


def migrate_lessons_to_db() -> None:
    """One-time: import any existing HTML files not yet in DB."""
    try:
        existing = {r["filename"] for r in get_lessons()}
        for html_path in OUTPUT_DIR.glob("*.html"):
            if html_path.name.startswith("v2-intensive-"):
                continue
            if html_path.name not in existing:
                write_lesson_meta(html_path.name, {"url": ""})
    except Exception as e:
        print(f"[meta] migration warn: {e}")


def resolve_output_html(filename: str) -> Path | None:
    safe_name = Path(filename or "").name
    if not safe_name or safe_name != filename or Path(safe_name).suffix.lower() != ".html":
        return None
    candidate = (OUTPUT_DIR / safe_name).resolve()
    output_root = OUTPUT_DIR.resolve()
    if output_root not in candidate.parents or not candidate.is_file():
        return None
    return candidate


def clear_lessons_cache() -> None:
    _lessons_cache["mtime"] = 0.0
