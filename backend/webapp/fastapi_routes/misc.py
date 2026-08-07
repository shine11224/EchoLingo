"""Phase 7C native FastAPI miscellaneous endpoints migrated from Flask."""
from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

import db
import webapp.storage.wordlists as wl_storage
from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from webapp.runtime import ai_config
from webapp.runtime import heartbeat as heartbeat_state
from webapp.services import dicts as dict_service
from webapp.services.natural_tts import synthesize_natural_speech
from webapp.services.v2_vocab import clear_vocab_caches
from webapp.services.errors import error_payload
from webapp.storage.lessons import OUTPUT_DIR, extract_js_var

router = APIRouter()
NATURAL_TTS_PREVIEW_DIR = OUTPUT_DIR / "tts_preview"


async def _parse_json_or_none(request: Request) -> dict[str, Any] | None:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _json_error(code: str, status: int, message: str | None = None, **kwargs) -> JSONResponse:
    payload = error_payload(code, message, **kwargs)
    return JSONResponse({"error": payload["message"], "error_info": payload}, status_code=status)


@router.get("/health")
def health():
    def key_status(value: str) -> str:
        return "configured" if value else "missing"

    def check_ffmpeg() -> dict:
        conda_root = Path(sys.executable).parent
        candidates = [
            conda_root / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg"),
            conda_root / "Library" / "bin" / "ffmpeg.exe",
            conda_root / "Library" / "bin" / "ffmpeg",
        ]
        path = shutil.which("ffmpeg") or next((str(p) for p in candidates if p.exists()), "")
        return {"available": bool(path), "path": path or ""}

    def check_sqlite() -> dict:
        db_path = db.current_db_path()
        result = {
            "path": str(db_path),
            "exists": db_path.exists(),
            "accessible": False,
            "sqlite_version": sqlite3.sqlite_version,
            "status": "missing",
        }
        if not db_path.exists():
            return result
        try:
            with sqlite3.connect(db_path, timeout=2) as conn:
                conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
                quick_check = conn.execute("PRAGMA quick_check").fetchone()
            result.update({
                "accessible": True,
                "status": "ok" if quick_check and quick_check[0] == "ok" else "warning",
                "quick_check": quick_check[0] if quick_check else "",
            })
        except Exception as exc:
            result.update({"status": "error", "error": exc.__class__.__name__})
        return result

    def whisper_cache_roots() -> list[Path]:
        roots = []
        if os.environ.get("HUGGINGFACE_HUB_CACHE"):
            roots.append(Path(os.environ["HUGGINGFACE_HUB_CACHE"]))
        if os.environ.get("HF_HOME"):
            roots.append(Path(os.environ["HF_HOME"]) / "hub")
        roots.extend([
            Path.home() / ".cache" / "huggingface" / "hub",
            ai_config.BASE_DIR / ".cache" / "huggingface" / "hub",
        ])

        unique_roots = []
        seen = set()
        for root in roots:
            root_key = str(root)
            if root_key not in seen:
                seen.add(root_key)
                unique_roots.append(root)
        return unique_roots

    def check_whisper_model(model_name: str) -> dict:
        cache_dir = f"models--Systran--faster-whisper-{model_name}"
        critical_files = {
            "model": ["model.bin"],
            "config": ["config.json"],
            "tokenizer": ["tokenizer.json"],
            "vocabulary": ["vocabulary.txt", "vocabulary.json"],
        }
        model_root = None
        snapshot_dir = None

        for root in whisper_cache_roots():
            candidate = root / cache_dir
            if candidate.exists():
                model_root = candidate
                snapshots_root = candidate / "snapshots"
                snapshots = sorted(
                    [p for p in snapshots_root.glob("*") if p.is_dir()],
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                ) if snapshots_root.exists() else []
                snapshot_dir = snapshots[0] if snapshots else None
                break

        files = {}
        for label, names in critical_files.items():
            files[label] = any(
                snapshot_dir and (snapshot_dir / name).is_file() and (snapshot_dir / name).stat().st_size > 0
                for name in names
            )
        return {
            "cache_dir": str(model_root) if model_root else "",
            "snapshot_dir": str(snapshot_dir) if snapshot_dir else "",
            "exists": bool(model_root),
            "critical_files": files,
            "ready": bool(snapshot_dir) and all(files.values()),
        }

    python_info = {"version": sys.version, "executable": sys.executable}
    ffmpeg_info = check_ffmpeg()
    sqlite_info = check_sqlite()
    keys_info = {
        "AI_API_KEY": key_status(ai_config.AI_API_KEY),
        "GROQ_API_KEY": key_status(os.environ.get("GROQ_API_KEY", "")),
    }
    whisper_info = {
        "cache_roots": [str(root) for root in whisper_cache_roots()],
        "models": {
            model_name: check_whisper_model(model_name)
            for model_name in ("base", "medium", "large-v3")
        },
    }

    return {
        "status": "ok",
        "environment": {
            "python": python_info,
            "ffmpeg": ffmpeg_info,
            "sqlite": sqlite_info,
            "keys": keys_info,
            "whisper": whisper_info,
        },
        "python": python_info,
        "ffmpeg": ffmpeg_info,
        "sqlite": sqlite_info,
        "api_keys": keys_info,
        "whisper": whisper_info,
        "dicts": {"ecdict": dict_service.ECDICT_DB.exists()},
        "ai_key": key_status(ai_config.AI_API_KEY),
    }


@router.get("/api/tts/natural")
def natural_tts_preview(text: str = ""):
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return JSONResponse({"error": "text required"}, status_code=400)
    if len(normalized) > 500:
        return JSONResponse({"error": "text too long"}, status_code=400)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    audio_path = NATURAL_TTS_PREVIEW_DIR / f"{digest}.wav"
    try:
        synthesize_natural_speech(normalized, audio_path)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return FileResponse(
        audio_path,
        media_type="audio/wav",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get("/api/lesson/check")
def api_lesson_check(url: str = ""):
    from sources.transcript_cache import _cache_key, TRANSCRIPT_CACHE_DIR

    url = (url or "").strip()
    match = re.search(r"BV[a-zA-Z0-9]+", url)
    if not match:
        return {"has_lesson": False, "has_transcript_cache": False}
    bvid = match.group(0)
    output_dir = ai_config.BASE_DIR / "output"
    cache_dir = ai_config.BASE_DIR / ".cache" / "bilibili"
    audio_path = cache_dir / f"{bvid}.m4a"
    has_transcript_cache = False
    if audio_path.exists():
        cache_file = TRANSCRIPT_CACHE_DIR / f"{_cache_key(audio_path, 'large-v3')}.json"
        has_transcript_cache = cache_file.exists()
    has_lesson_html = any(output_dir.glob(f"*{bvid}*.html")) if output_dir.exists() else False
    return {
        "bvid": bvid,
        "has_lesson": has_lesson_html or has_transcript_cache,
        "has_transcript_cache": has_transcript_cache,
        "has_lesson_html": has_lesson_html,
    }


@router.get("/api/export/lesson/{filename:path}")
def api_export_lesson(filename: str, format: str = Query(default="markdown")):
    _ = format.lower()
    safe = Path(filename).name
    if not safe or Path(safe).suffix.lower() != ".html":
        return _json_error("UNKNOWN_ERROR", 400, "invalid filename")

    html_path = OUTPUT_DIR / safe
    if not html_path.exists():
        return _json_error("UNKNOWN_ERROR", 404, "lesson not found")

    raw = html_path.read_text(encoding="utf-8")
    title = extract_js_var(raw, "lessonTitle") or safe
    analyses = extract_js_var(raw, "analyses") or []
    segments = extract_js_var(raw, "segments") or []

    def sentence_score(item):
        diff = {"A2": 0, "B1": 1, "B2": 2, "C1": 3}
        return diff.get(item.get("difficulty", "B1"), 1) + len(item.get("vocabulary", []))

    sorted_analyses = sorted(enumerate(analyses), key=lambda x: sentence_score(x[1]), reverse=True)
    patterns_seen, top_patterns = set(), []
    for idx, analysis in sorted_analyses:
        pattern = analysis.get("pattern", {})
        template = pattern.get("template", "").strip()
        if template and template not in patterns_seen:
            patterns_seen.add(template)
            seg_text = segments[idx].get("text", "") if idx < len(segments) else ""
            top_patterns.append({
                "template": template,
                "usage": pattern.get("usage", ""),
                "example": analysis.get("text", seg_text),
            })
        if len(top_patterns) >= 3:
            break

    with db._db() as conn:
        vocab_rows = conn.execute(
            "SELECT w.word, w.level, w.cached_analysis, c.sentence"
            " FROM words w LEFT JOIN contexts c ON c.word=w.word AND c.lesson=?"
            " WHERE c.lesson=? ORDER BY w.last_studied DESC LIMIT 15",
            (safe, safe),
        ).fetchall()
        reflection_row = conn.execute(
            "SELECT reflection FROM lesson_reflections WHERE filename=? ORDER BY id DESC LIMIT 1",
            (safe,),
        ).fetchone()

    reflection = reflection_row["reflection"] if reflection_row else ""

    lines = [f"# {title}", ""]
    lesson_meta = next((lesson for lesson in db.get_lessons(include_archived=True) if lesson["filename"] == safe), {})
    meta_parts = [
        lesson_meta.get("source_type", ""),
        f"共 {lesson_meta.get('sentence_count', len(analyses))} 句",
        lesson_meta.get("created_at", ""),
    ]
    lines += [f"> {' · '.join(part for part in meta_parts if part)}", ""]

    if top_patterns:
        lines += ["## 可复用句式", ""]
        for idx, pattern in enumerate(top_patterns, 1):
            lines.append(f"{idx}. **{pattern['template']}**")
            if pattern["usage"]:
                lines.append(f"   → {pattern['usage']}")
            if pattern["example"]:
                lines.append(f"   > 例句：{pattern['example']}")
            lines.append("")

    if vocab_rows:
        lines += ["## 重点词汇", "", "| 词汇 | 等级 | 含义 | 例句 |", "|------|------|------|------|"]
        for row in vocab_rows:
            analysis = json.loads(row["cached_analysis"]) if row["cached_analysis"] else None
            vocab_items = analysis.get("vocabulary", []) if isinstance(analysis, dict) else (analysis or [])
            first = next((item for item in vocab_items if item.get("word")), None) if vocab_items else None
            meaning = first.get("meaning", "") if first else ""
            sentence = (row["sentence"] or "")[:60]
            lines.append(f"| {row['word']} | {row['level']} | {meaning} | {sentence} |")
        lines.append("")

    if reflection:
        lines += ["## 课后总结", "", reflection, ""]

    content = "\n".join(lines)
    safe_title = "".join(char for char in title if char.isalnum() or char in " _-")[:40]
    return Response(
        content,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=lesson_{safe_title}.md"},
    )


@router.post("/api/browse-file")
async def api_browse_file():
    def _browse() -> dict:
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.attributes("-topmost", True)
            root.withdraw()
            path = filedialog.askopenfilename(
                parent=root,
                title="选择音视频文件",
                filetypes=[
                    ("音视频文件", "*.mp4 *.m4a *.mp3 *.mkv *.webm *.mov *.avi *.flac *.ogg"),
                    ("全部文件", "*.*"),
                ],
            )
            root.destroy()
            return {"path": path or ""}
        except Exception as exc:
            return {"path": "", "error": str(exc)}

    return await run_in_threadpool(_browse)


@router.post("/api/heartbeat")
async def api_heartbeat(request: Request):
    data = await _parse_json_or_none(request) or {}
    heartbeat_state.heartbeat_ts[0] = time.time()
    heartbeat_state.heartbeat_seen[0] = True
    heartbeat_state.heartbeat_paused[0] = data.get("paused", False)
    return Response(status_code=204)


@router.post("/api/wordlists/upload")
async def api_upload_wordlist(
    file: UploadFile = File(default=None),
    name: str = Form(default=""),
    tag: str = Form(default=""),
):
    if file is None:
        return JSONResponse({"error": "no file"}, status_code=400)
    if not file.filename:
        return JSONResponse({"error": "empty filename"}, status_code=400)

    content = (await file.read()).decode("utf-8-sig", errors="replace")
    valid, message, words, invalid = wl_storage.validate_wordlist_upload(file.filename, content)
    if not valid:
        return JSONResponse({"ok": False, "error": message, "invalid_tokens": invalid}, status_code=400)

    safe_name = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", file.filename)
    save_path = wl_storage.user_source_dir() / safe_name
    save_path.write_text(content, encoding="utf-8")

    success = wl_storage.compile_user_wordlist(save_path, display_name=name, tag=tag)
    if not success:
        return JSONResponse({"ok": False, "error": "词表编译失败，请检查文件编码和格式。"}, status_code=400)
    clear_vocab_caches()
    return {
        "ok": True,
        "filename": safe_name,
        "key": wl_storage._user_compiled_path(save_path).stem,
        "count": len(words),
        "invalid_tokens": invalid,
    }


def _expand_wordlist_words(
    originals: list[str],
    source_total_count: int,
    invalid: list[str],
) -> dict:
    """Expand words locally via the built-in ECDICT inflection data; uncovered words pass through."""
    local_items = wl_storage.expand_with_local_word_families(originals)
    expanded = set(originals)
    normalized_by_source: dict[str, set[str]] = {}
    for source, forms in local_items.items():
        clean_forms = {form for form in forms if wl_storage._clean_word(form)} | {source}
        expanded.update(clean_forms)
        normalized_by_source[source] = clean_forms

    normalized_items = [
        {"source": source, "forms": sorted(forms)}
        for source, forms in sorted(normalized_by_source.items())
    ]
    return {
        "ok": True,
        "source_total_count": source_total_count,
        "truncated_count": max(0, source_total_count - len(originals)),
        "original_count": len(originals),
        "local_family_count": len(local_items),
        "count": len(expanded),
        "added_count": len(expanded) - len(originals),
        "words": sorted(expanded),
        "items": normalized_items,
        "invalid_tokens": invalid,
    }


async def _read_wordlist_expansion_upload(file: UploadFile, limit: int):
    content = (await file.read()).decode("utf-8-sig", errors="replace")
    valid, message, _words, invalid = wl_storage.validate_wordlist_upload(file.filename, content)
    if not valid:
        return None, JSONResponse(
            {"ok": False, "error": message, "invalid_tokens": invalid}, status_code=400
        )
    ordered_words = wl_storage.parse_wordlist_content_ordered(content)
    source_total_count = len(ordered_words)
    if limit > 0:
        ordered_words = ordered_words[:min(limit, 5000)]
    return (ordered_words, source_total_count, invalid), None


@router.post("/api/wordlists/expand")
async def api_expand_wordlist(file: UploadFile = File(default=None), limit: int = Form(default=0)):
    if file is None or not file.filename:
        return JSONResponse({"ok": False, "error": "请先选择词表文件。"}, status_code=400)
    prepared, error = await _read_wordlist_expansion_upload(file, limit)
    if error:
        return error
    return _expand_wordlist_words(*prepared)


@router.patch("/api/wordlists/upload/{filename:path}")
async def api_patch_wordlist(filename: str, request: Request):
    data = await _parse_json_or_none(request)
    if not isinstance(data, dict):
        return JSONResponse({"ok": False, "error": "JSON body required"}, status_code=400)
    if "name" not in data and "tag" not in data:
        return JSONResponse({"ok": False, "error": "name or tag required"}, status_code=400)

    ok, message, updated = wl_storage.update_uploaded_wordlist_metadata(
        filename,
        name=data.get("name"),
        tag=data.get("tag"),
    )
    if not ok:
        status = 404 if "not found" in message else 400
        return JSONResponse({"ok": False, "error": message}, status_code=status)
    clear_vocab_caches()
    return {"ok": True, **updated}


@router.delete("/api/wordlists/upload/{filename:path}")
def api_delete_wordlist(filename: str):
    ok, message = wl_storage.delete_uploaded_wordlist(filename)
    if ok:
        clear_vocab_caches()
    status = 200 if ok else 404
    return JSONResponse({"ok": ok, "error": message}, status_code=status)


@router.post("/api/patterns/upload")
async def api_upload_pattern(file: UploadFile = File(default=None)):
    if file is None:
        return JSONResponse({"error": "no file"}, status_code=400)
    if not file.filename:
        return JSONResponse({"error": "empty filename"}, status_code=400)

    raw = await file.read()
    valid, message, patterns = wl_storage.parse_patterns_upload(file.filename, raw)
    if not valid:
        return JSONResponse({"ok": False, "error": message}, status_code=400)

    original = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", file.filename)
    safe_name = re.sub(r"\.[^.]+$", ".json", original)
    (wl_storage.PATTERNS_DIR / safe_name).write_text(
        json.dumps(patterns, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"ok": True, "filename": safe_name, "count": len(patterns)}


@router.delete("/api/patterns/upload/{filename:path}")
def api_delete_pattern(filename: str):
    ok, message = wl_storage.delete_uploaded_pattern(filename)
    status = 200 if ok else 404
    return JSONResponse({"ok": ok, "error": message}, status_code=status)


@router.patch("/api/patterns/upload/{filename:path}")
async def api_patch_pattern(filename: str, request: Request):
    data = await _parse_json_or_none(request)
    if not isinstance(data, dict):
        return JSONResponse({"ok": False, "error": "JSON body required"}, status_code=400)
    if "name" not in data:
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)

    ok, message, updated = wl_storage.update_uploaded_pattern_metadata(filename, name=data.get("name"))
    if not ok:
        status = 404 if "not found" in message else 400
        return JSONResponse({"ok": False, "error": message}, status_code=status)
    return {"ok": True, **updated}


@router.post("/api/transcribe")
async def api_transcribe(audio: UploadFile = File(default=None)):
    if audio is None:
        return JSONResponse({"error": "未收到音频数据"}, status_code=400)
    audio_bytes = await audio.read()
    if not audio_bytes:
        return JSONResponse({"error": "音频为空"}, status_code=400)

    def _transcribe():
        filename = audio.filename or "recording.webm"
        if ai_config.GROQ_API_KEY:
            try:
                from groq import Groq as _Groq
                gc = _Groq(api_key=ai_config.GROQ_API_KEY)
                resp = gc.audio.transcriptions.create(
                    file=(filename, audio_bytes),
                    model="whisper-large-v3-turbo",
                )
                return {"text": resp.text.strip()}
            except Exception:
                pass
        import tempfile
        from faster_whisper import WhisperModel
        suffix = Path(filename).suffix or ".webm"
        tmp = Path(tempfile.mktemp(suffix=suffix))
        try:
            tmp.write_bytes(audio_bytes)
            model = WhisperModel("base", device="cpu", compute_type="int8")
            segs, _ = model.transcribe(str(tmp))
            return {"text": " ".join(s.text.strip() for s in segs)}
        finally:
            tmp.unlink(missing_ok=True)

    try:
        return await run_in_threadpool(_transcribe)
    except Exception as exc:
        return JSONResponse({"error": f"转写失败：{exc}"}, status_code=500)


@router.get("/api/download-audio/{youtube_id}")
def api_download_audio(youtube_id: str):
    import subprocess

    ytid = re.sub(r"[^a-zA-Z0-9_-]", "", youtube_id)
    if not ytid:
        return JSONResponse({"ok": False, "error": "invalid youtube_id"}, status_code=400)
    audio_path = OUTPUT_DIR / f"{ytid}.m4a"
    if audio_path.exists() and audio_path.stat().st_size > 0:
        return {"ok": True, "audio_file": f"{ytid}.m4a", "cached": True}
    tmp_path = audio_path.with_suffix(".m4a.tmp")
    try:
        ytdlp = shutil.which("yt-dlp") or "yt-dlp"
        result = subprocess.run(
            [ytdlp, "-f", "bestaudio[ext=m4a]/bestaudio", "--no-playlist",
             "-o", str(tmp_path), f"https://www.youtube.com/watch?v={ytid}"],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0 or not tmp_path.exists() or tmp_path.stat().st_size == 0:
            return JSONResponse(
                {"ok": False, "error": "yt-dlp failed", "stderr": result.stderr[-500:]},
                status_code=500,
            )
        tmp_path.replace(audio_path)
        return {"ok": True, "audio_file": f"{ytid}.m4a"}
    except subprocess.TimeoutExpired:
        return JSONResponse({"ok": False, "error": "download timed out (5 min)"}, status_code=504)
    except FileNotFoundError:
        return JSONResponse({"ok": False, "error": "yt-dlp not installed"}, status_code=500)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
