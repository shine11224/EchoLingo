import datetime
import json
import re
import threading
from pathlib import Path

from db import get_lessons, upsert_lesson
from webapp.storage import user_assets


BASE_DIR = Path(__file__).resolve().parents[3]
OUTPUT_DIR = BASE_DIR / "output"

# 按用户 output root 隔离的扫描缓存，加锁保证并发请求不串数据
_lessons_cache: dict[str, dict] = {}
_lessons_cache_lock = threading.Lock()


def _output_root() -> Path:
    """多用户解析当前用户 output，单用户回退全局 OUTPUT_DIR。"""
    return user_assets.current_output_root(OUTPUT_DIR)


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
    output_root = _output_root()
    root_key = str(output_root)
    html_files = sorted(
        (path for path in output_root.glob("*.html") if not path.name.startswith("v2-intensive-")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not html_files:
        return []
    latest_mtime = max(p.stat().st_mtime for p in html_files)
    with _lessons_cache_lock:
        entry = _lessons_cache.get(root_key)
        if entry and latest_mtime <= entry["mtime"]:
            return entry["data"]

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

    with _lessons_cache_lock:
        _lessons_cache[root_key] = {"mtime": latest_mtime, "data": lessons}
    return lessons


def write_lesson_meta(filename: str, job: dict) -> None:
    try:
        html_path = _output_root() / filename
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
        for html_path in _output_root().glob("*.html"):
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
    return user_assets.resolve_output_file(safe_name, fallback=OUTPUT_DIR)


def clear_lessons_cache() -> None:
    with _lessons_cache_lock:
        _lessons_cache.clear()
