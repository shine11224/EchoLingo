from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path

from schemas import Segment


BASE_DIR = Path(__file__).resolve().parent.parent
TRANSCRIPT_CACHE_DIR = BASE_DIR / ".cache" / "transcripts"


def _cache_key(media_path: Path, model_name: str) -> str:
    resolved = media_path.expanduser().resolve()
    stat = resolved.stat()
    raw = "|".join([
        str(resolved),
        str(stat.st_size),
        str(stat.st_mtime_ns),
        model_name,
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def load_transcript_cache(media_path: Path, model_name: str) -> list[Segment] | None:
    path = TRANSCRIPT_CACHE_DIR / f"{_cache_key(media_path, model_name)}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        items = payload.get("segments", [])
        if not isinstance(items, list):
            return None
        segments: list[Segment] = []
        for item in items:
            if not isinstance(item, dict) or not item.get("text"):
                continue
            segments.append(Segment(
                index=int(item.get("index") or len(segments) + 1),
                text=str(item.get("text") or ""),
                start=item.get("start"),
                end=item.get("end"),
                translation=str(item.get("translation") or ""),
                # 词级时间戳必须随缓存还原，否则命中缓存的课程退化为插值断句（2026-08-07 云端吞句 bug）
                words=item.get("words") or None,
            ))
        return segments or None
    except Exception:
        return None


def extract_segments_from_lesson_html(bvid: str, output_dir: Path) -> list[Segment] | None:
    """在 output/ 里找匹配 bvid 的课程 HTML，提取 segments 列表。"""
    pattern = re.compile(r'const\s+segments\s*=\s*(\[.*?\]);', re.DOTALL)
    for html_file in output_dir.glob("*.html"):
        if bvid.lower() not in html_file.name.lower():
            continue
        try:
            text = html_file.read_text(encoding="utf-8", errors="ignore")
            m = pattern.search(text)
            if not m:
                continue
            items = json.loads(m.group(1))
            segments: list[Segment] = []
            for item in items:
                if not isinstance(item, dict) or not item.get("text"):
                    continue
                segments.append(Segment(
                    index=int(item.get("index") or len(segments) + 1),
                    text=str(item["text"]),
                    start=item.get("start"),
                    end=item.get("end"),
                    translation=str(item.get("translation") or ""),
                    words=item.get("words") or None,
                ))
            if segments:
                return segments
        except Exception:
            continue
    return None


def save_transcript_cache(media_path: Path, model_name: str, segments: list[Segment]) -> Path:
    TRANSCRIPT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_CACHE_DIR / f"{_cache_key(media_path, model_name)}.json"
    tmp_path = path.with_suffix(".json.tmp")
    payload = {
        "media": str(media_path.expanduser().resolve()),
        "model": model_name,
        "segments": [asdict(segment) for segment in segments],
    }
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)
    return path
