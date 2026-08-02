"""Generation job runtime state and helpers independent of Flask."""
from __future__ import annotations

import atexit
import os
import re
import time
from urllib.parse import unquote, urlparse

from webapp.constants import PIPELINE_SCHEMA
from webapp.runtime.ai_config import BASE_DIR
from webapp.services.errors import error_payload as _error_payload
from webapp.storage.lessons import OUTPUT_DIR, clear_lessons_cache, write_lesson_meta

_jobs: dict = {}
_active_procs: dict = {}
_ORDERED_STEPS = [step["id"] for step in PIPELINE_SCHEMA]


def _cleanup_old_jobs() -> None:
    cutoff = time.time() - 7200
    to_del = [
        jid for jid, job in list(_jobs.items())
        if job.get("status") in ("done", "error", "cancelled")
        and job.get("started_at", 0) < cutoff
    ]
    for jid in to_del:
        _jobs.pop(jid, None)


def _kill_all_jobs() -> None:
    for proc in list(_active_procs.values()):
        try:
            proc.kill()
        except Exception:
            pass


atexit.register(_kill_all_jobs)


def _make_job_dict(url: str, job_key: str, log_path) -> dict:
    return {
        "status": "running", "log": [], "output_file": None, "error": None,
        "current_step": "init", "completed_steps": [], "url": url, "job_key": job_key,
        "started_at": time.time(), "step_detail": "", "error_info": None,
        "log_file": log_path.name,
        "log_path": str(log_path),
        "transcription_backend": "", "transcription_message": "",
        "transcription_fallback_reason": "", "transcription_progress": None,
        "transcription_chunk_progress": "",
        "pipeline_schema": PIPELINE_SCHEMA,
    }


def _set_job_error(job: dict, code: str, message: str | None = None, *, step: str | None = None, detail: str | None = None) -> None:
    info = _error_payload(code, message, step=step or job.get("current_step"), detail=detail)
    job["status"] = "error"
    job["error"] = info["message"]
    job["error_info"] = info


def _classify_failure(text: str, returncode: int | None = None) -> tuple[str, str]:
    lower = text.lower()
    if "invalid choice" in lower or "unrecognized arguments" in lower:
        return "CONFIG_ERROR", "生成参数配置不匹配"
    if "deepseek_api_key not set" in lower or "api key" in lower or "unauthorized" in lower or "401" in lower:
        return "MISSING_KEY", "缺少或无效的 API Key"
    if "quota" in lower or "429" in lower or "rate limit" in lower:
        return "QUOTA_EXCEEDED", "API 配额用尽或触发限流"
    if "ffmpeg" in lower or "no module named" in lower or "not installed" in lower:
        return "MISSING_DEPENDENCY", "缺少运行依赖"
    if "cookies" in lower or "login" in lower or "auth" in lower or "权限" in text or "登录" in text:
        return "AUTH_REQUIRED", "当前来源可能需要登录凭证"
    if "filenotfounderror" in lower or "file not found" in lower or "不存在" in text or "no such file" in lower:
        return "LOCAL_FILE_MISSING", "本地文件不存在或路径错误"
    if "json" in lower and ("decode" in lower or "parse" in lower or "解析" in text):
        return "AI_JSON_ERROR", "AI 返回内容无法解析"
    if "network" in lower or "connection" in lower or "request" in lower or "http" in lower:
        return "NETWORK_ERROR", "网络请求失败"
    if "timeout" in lower or "timed out" in lower or "超时" in text:
        return "TIMEOUT", "任务超时"
    if "subtitle" in lower or "字幕" in text:
        return "TRANSCRIPT_NOT_FOUND", "没有找到可用字幕"
    if returncode:
        return "UNKNOWN_ERROR", f"进程退出码 {returncode}"
    return "UNKNOWN_ERROR", "生成失败"


_STEP_SIGNALS = [
    (r"\[STEP:download\]", "download"),
    (r"\[STEP:subtitle\]", "subtitle"),
    (r"\[STEP:whisper_transcribe\]", "whisper_transcribe"),
    (r"\[STEP:resegment_translate\]", "resegment_translate"),
    (r"\[STEP:resegment_translate_analyze\]", "resegment_translate"),
    (r"\[STEP:analyze\]", "analyze"),
    (r"\[STEP:ipa_annotate\]", "ipa_annotate"),
    (r"Generated lesson", "render"),
]


def _first_match(text: str, patterns: list[str]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return (match.group(1) if match.groups() else match.group(0)).strip()
    return ""


def _marker_payload(line: str, marker: str) -> str:
    idx = line.upper().find(marker)
    if idx < 0:
        return ""
    return line[idx + len(marker):].strip(" :-\t")


def _set_transcription_state(
    job: dict,
    *,
    backend: str | None = None,
    message: str | None = None,
    fallback_reason: str | None = None,
    progress: int | None = None,
    chunk_progress: str | None = None,
) -> None:
    if backend is not None:
        job["transcription_backend"] = backend
    if message is not None:
        job["transcription_message"] = message
    if fallback_reason is not None:
        job["transcription_fallback_reason"] = fallback_reason
    if progress is not None:
        job["transcription_progress"] = max(0, min(100, int(progress)))
    if chunk_progress is not None:
        job["transcription_chunk_progress"] = chunk_progress


def _update_transcription_state_from_stdout(job: dict, line: str) -> None:
    if not line:
        return
    upper = line.upper()
    lower = line.lower()
    progress_text = _first_match(line, [
        r"\[WHISPER_PROGRESS\]\s*(\d{1,3})%",
        r"\[GROQ_PROGRESS\]\s*(\d{1,3})%",
        r"\bprogress[:= ]+(\d{1,3})%",
    ])
    if progress_text:
        progress = int(progress_text)
        backend = job.get("transcription_backend") or "local"
        backend_label = "Groq" if backend == "groq" else "本地 Whisper"
        _set_transcription_state(job, backend=backend, progress=progress, message=f"{backend_label} 转录 {progress}%")
        job["step_detail"] = f"已转录 {progress}%"
        return
    if "[GROQ_CHUNK]" in upper or "[GROQ_COMPRESS]" in upper:
        payload = _marker_payload(line, "[GROQ_CHUNK]") or _marker_payload(line, "[GROQ_COMPRESS]")
        chunk_progress = _first_match(line, [r"(\d+\s*/\s*\d+)"])
        _set_transcription_state(job, backend="groq", message="正在使用 Groq 转录", chunk_progress=chunk_progress or payload)
        job["step_detail"] = chunk_progress or "Groq 转录"
        return
    if "[GROQ_FALLBACK]" in upper:
        reason = _marker_payload(line, "[GROQ_FALLBACK]") or line
        _set_transcription_state(job, backend="local", fallback_reason=reason, message="Groq 转录失败，已切换到本地 Whisper")
        job["step_detail"] = "切换到本地 Whisper"
        return
    if "[GROQ]" in upper or "groq" in lower:
        _set_transcription_state(job, backend="groq", message="正在使用 Groq 转录")
        job["step_detail"] = "Groq 转录"
        return
    if "[LOCAL_WHISPER]" in upper or "loading whisper" in lower or "local whisper" in lower or "whisper" in lower:
        loading = "loading" in lower or "load" in lower
        _set_transcription_state(job, backend="local", message="正在加载本地 Whisper 模型" if loading else "正在使用本地 Whisper 转录")
        job["step_detail"] = "加载本地 Whisper" if loading else "本地 Whisper"


def _advance_step(job: dict, new_step: str) -> None:
    if new_step not in _ORDERED_STEPS:
        return
    current_step = job.get("current_step", "init")
    if current_step not in _ORDERED_STEPS:
        current_step = "init"
    current_idx = _ORDERED_STEPS.index(current_step)
    new_idx = _ORDERED_STEPS.index(new_step)
    if new_idx < current_idx:
        if new_step not in job["completed_steps"]:
            job["completed_steps"].append(new_step)
        return
    if new_idx > current_idx and current_step not in job["completed_steps"]:
        job["completed_steps"].append(current_step)
    job["current_step"] = new_step


def _complete_current_step(job: dict) -> None:
    current_step = job.get("current_step")
    if current_step in _ORDERED_STEPS and current_step not in job["completed_steps"]:
        job["completed_steps"].append(current_step)


def _local_file_url_to_path(value: str) -> str:
    if not value.lower().startswith("file://"):
        return value
    parsed = urlparse(value)
    path = unquote(parsed.path)
    if re.match(r"^/[a-zA-Z]:/", path):
        path = path[1:]
    return path.replace("/", os.sep)

