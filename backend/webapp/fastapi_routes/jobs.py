"""Phase 5A/5B/5D 鈥?job status, log, cancel and generate endpoints migrated from Flask."""
from __future__ import annotations

import datetime
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from webapp.constants import PIPELINE_SCHEMA
from webapp.runtime import jobs as job_runtime
from webapp.storage.job_logs import append_job_log, make_job_log_path, resolve_log_file, tail_log

router = APIRouter()


def _json_error(code: str, status: int, message: str | None = None, **kwargs) -> JSONResponse:
    payload = job_runtime._error_payload(code, message, **kwargs)
    return JSONResponse({"error": payload["message"], "error_info": payload}, status_code=status)


def _err(message: str, status: int) -> JSONResponse:
    return _json_error("UNKNOWN_ERROR", status, message)


def _err404(message: str) -> JSONResponse:
    return _err(message, 404)


# 鈹€鈹€ Phase 5A read-only endpoints 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

@router.get("/api/jobs")
def api_jobs():
    job_runtime._cleanup_old_jobs()
    return {
        jid: {
            "status":                        j["status"],
            "current_step":                  j.get("current_step"),
            "step_detail":                   j.get("step_detail"),
            "transcription_backend":         j.get("transcription_backend", ""),
            "transcription_message":         j.get("transcription_message", ""),
            "transcription_fallback_reason": j.get("transcription_fallback_reason", ""),
            "transcription_progress":        j.get("transcription_progress"),
            "transcription_chunk_progress":  j.get("transcription_chunk_progress", ""),
            "error":                         j.get("error"),
            "error_info":                    j.get("error_info"),
            "log_file":                      j.get("log_file", ""),
            "url":                           j.get("url", "")[:80],
            "elapsed_s":                     round(time.time() - j.get("started_at", time.time())),
        }
        for jid, j in job_runtime._jobs.items()
    }


@router.get("/api/generate/status/{job_id}")
def api_generate_status(job_id: str):
    job = job_runtime._jobs.get(job_id)
    if not job:
        return _err404("job not found")
    return {
        "status":                        job["status"],
        "log":                           job["log"][-60:],
        "output_file":                   job["output_file"],
        "error":                         job["error"],
        "error_info":                    job.get("error_info"),
        "log_file":                      job.get("log_file", ""),
        "log_tail":                      tail_log(job.get("log_path"), 60),
        "current_step":                  job.get("current_step", "init"),
        "completed_steps":               job.get("completed_steps", []),
        "step_detail":                   job.get("step_detail", ""),
        "transcription_backend":         job.get("transcription_backend", ""),
        "transcription_message":         job.get("transcription_message", ""),
        "transcription_fallback_reason": job.get("transcription_fallback_reason", ""),
        "transcription_progress":        job.get("transcription_progress"),
        "transcription_chunk_progress":  job.get("transcription_chunk_progress", ""),
        "pipeline_schema":               PIPELINE_SCHEMA,
    }


@router.get("/api/logs/{filename:path}")
def api_read_log(filename: str):
    log_path = resolve_log_file(filename)
    if not log_path:
        return _err404("log not found")
    return Response(
        log_path.read_text(encoding="utf-8", errors="replace"),
        media_type="text/plain; charset=utf-8",
    )


# 鈹€鈹€ Phase 5B cancel 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

@router.post("/api/generate/cancel/{job_id}")
def api_generate_cancel(job_id: str):
    job = job_runtime._jobs.get(job_id)
    if not job:
        return _err404("job not found")
    if job["status"] != "running":
        return _err("job is not running", 400)
    proc = job_runtime._active_procs.get(job_id)
    if proc:
        proc.kill()
    job_runtime._set_job_error(job, "USER_CANCELLED", "鐢ㄦ埛鍙栨秷")
    append_job_log(job.get("log_path"), "[job] cancelled by user")
    job_runtime._active_procs.pop(job_id, None)
    return {"ok": True}


# 鈹€鈹€ Phase 5D generate 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

@router.post("/api/generate")
async def api_generate(request: Request):
    try:
        data = await request.json()
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}

    url         = (data.get("url") or "").strip()
    source_type = (data.get("source_type") or "").strip()
    dl_mode     = data.get("download_mode", "audio")
    bili_page   = data.get("bilibili_page", "")
    analysis    = data.get("analysis_mode", "auto")
    whisper     = data.get("whisper_model", "large-v3")
    ai_segmentation = data.get("ai_segmentation", True)
    ai_ipa          = data.get("ai_ipa", True)

    job_runtime._cleanup_old_jobs()

    cmd = [sys.executable, str(job_runtime.BASE_DIR / "backend" / "main.py")]
    job_key = url

    if not url and source_type not in ("local", "text"):
        return _json_error("URL_REQUIRED", 400, "url required", step="init")

    if source_type == "bilibili":
        cmd += ["--bilibili", url, "--bilibili-download", dl_mode]
        if bili_page:
            cmd += ["--bilibili-page", str(bili_page)]
        job_key = f"bilibili:{url}:p{bili_page or ''}:{dl_mode}"

    elif source_type == "youtube":
        yt_download = data.get("youtube_download", "subtitle-only")
        cmd += ["--youtube", url, "--youtube-download", yt_download]
        job_key = f"youtube:{url}:{yt_download}"

    elif source_type == "article":
        cmd += ["--article", url]
        job_key = f"article:{url}"

    elif source_type == "local":
        local_path = (data.get("local_path") or data.get("video_file") or url).strip()
        transcript_path = (data.get("transcript_path") or data.get("transcript_file") or "").strip()
        if not local_path:
            return _json_error("LOCAL_PATH_REQUIRED", 400, "local file path required", step="init")
        local_path = job_runtime._local_file_url_to_path(local_path)
        if not Path(local_path).is_file():
            return _json_error(
                "LOCAL_FILE_MISSING", 400,
                f"local file not found: {local_path}",
                step="init", detail=local_path,
            )
        if transcript_path:
            transcript_path = job_runtime._local_file_url_to_path(transcript_path)
            if not Path(transcript_path).is_file():
                return _json_error(
                    "LOCAL_FILE_MISSING", 400,
                    f"transcript file not found: {transcript_path}",
                    step="init", detail=transcript_path,
                )
        cmd += ["--video-file", local_path]
        if transcript_path:
            cmd += ["--transcript-file", transcript_path]
        job_key = f"local:{local_path}:{transcript_path}"

    elif source_type == "text":
        text_path = (data.get("local_path") or data.get("video_file") or url).strip()
        if not text_path:
            return _json_error("TEXT_PATH_REQUIRED", 400, "鏂囨湰鏂囦欢璺緞涓嶈兘涓虹┖", step="init")
        text_path = job_runtime._local_file_url_to_path(text_path)
        if not Path(text_path).is_file():
            return _json_error(
                "TEXT_FILE_NOT_FOUND", 400,
                f"鎵句笉鍒版枃鏈枃浠? {text_path}",
                step="init", detail=text_path,
            )
        job_key = f"text:{text_path}"
        for existing in job_runtime._jobs.values():
            if existing.get("status") == "running" and existing.get("job_key") == job_key:
                payload = job_runtime._error_payload("DUPLICATE_JOB", "璇ヤ换鍔℃鍦ㄧ敓鎴愪腑锛岃绛夊緟瀹屾垚鍚庡啀鎻愪氦", step="init")
                return JSONResponse({"error": payload["message"], "error_info": payload, "duplicate": True}, status_code=409)

        job_id = uuid.uuid4().hex[:8]
        log_path = make_job_log_path(job_id)
        job_runtime._jobs[job_id] = job_runtime._make_job_dict(text_path, job_key, log_path)
        append_job_log(log_path, f"[job] id={job_id} source_type=text started_at={datetime.datetime.now().isoformat(timespec='seconds')}")
        append_job_log(log_path, f"[text] file={text_path}")
        title = data.get("title", "")

        def _run_text(jid=job_id, fpath=text_path, amode=analysis, lp=log_path, ttl=title):
            try:
                from sources.text import build_text_lesson  # noqa: PLC0415
                from analyzer import SentenceAnalyzer        # noqa: PLC0415
                from renderer import LessonRenderer          # noqa: PLC0415

                job = job_runtime._jobs[jid]
                job_runtime._advance_step(job, "resegment_translate")
                append_job_log(lp, "[STEP:resegment_translate]")
                bundle = build_text_lesson(fpath, title=ttl)
                append_job_log(lp, f"[text] parsed {len(bundle.segments)} segments from {fpath}")

                job_runtime._advance_step(job, "analyze")
                append_job_log(lp, "[STEP:analyze]")
                analyzer = SentenceAnalyzer(mode=amode)
                analyses = analyzer.analyze(bundle.segments, bundle.title)

                job_runtime._advance_step(job, "render")
                append_job_log(lp, "[STEP:render]")
                renderer = LessonRenderer(job_runtime.BASE_DIR / "frontend" / "templates")
                output_path = renderer.render(bundle, analyses, job_runtime.OUTPUT_DIR)
                output_file = output_path.name

                job_runtime._complete_current_step(job)
                job["status"] = "done"
                job["output_file"] = output_file
                append_job_log(lp, f"Generated lesson: {output_path}")
                append_job_log(lp, f"[job] done output_file={output_file}")
                job_runtime.clear_lessons_cache()
                if output_file:
                    job_runtime.write_lesson_meta(output_file, job)
            except Exception as exc:
                code, message = job_runtime._classify_failure(str(exc))
                job_runtime._set_job_error(job_runtime._jobs.get(jid, {}), code, message, detail=str(exc))
                append_job_log(lp, f"[job] exception code={code} detail={exc}")

        threading.Thread(target=_run_text, daemon=True).start()
        return {"job_id": job_id}

    else:
        return _json_error("UNSUPPORTED_SOURCE", 400, f"unsupported source_type: {source_type}", step="init")

    # Duplicate-job check for subprocess-based sources
    for existing in job_runtime._jobs.values():
        if existing.get("status") == "running" and existing.get("job_key") == job_key:
            payload = job_runtime._error_payload("DUPLICATE_JOB", "璇ヤ换鍔℃鍦ㄧ敓鎴愪腑锛岃绛夊緟瀹屾垚鍚庡啀鎻愪氦", step="init")
            return JSONResponse({"error": payload["message"], "error_info": payload, "duplicate": True}, status_code=409)

    if whisper == "groq" and not os.environ.get("GROQ_API_KEY"):
        return _json_error(
            "MISSING_KEY", 400,
            "已选择 Groq 转录，但未配置 GROQ_API_KEY。请先在资源管理 API 设置中填写 Groq Key。",
            step="init",
        )
    if whisper not in {"groq", "base", "medium", "large-v3"}:
        return _json_error("CONFIG_ERROR", 400, f"unsupported whisper_model: {whisper}", step="init", detail=whisper)

    cmd += ["--output-dir", str(job_runtime.OUTPUT_DIR), "--analysis-mode", analysis, "--whisper-model", whisper]
    if not ai_segmentation:
        cmd += ["--disable-ai-segmentation"]
    if not ai_ipa:
        cmd += ["--disable-ai-ipa"]

    _TIMEOUT_SECS = {"groq": 45 * 60, "base": 90 * 60, "medium": 150 * 60, "large-v3": 240 * 60}
    timeout_secs = _TIMEOUT_SECS.get(whisper, 150 * 60)

    job_id = uuid.uuid4().hex[:8]
    log_path = make_job_log_path(job_id)
    job_runtime._jobs[job_id] = job_runtime._make_job_dict(url, job_key, log_path)
    append_job_log(log_path, f"[job] id={job_id} source_type={source_type} started_at={datetime.datetime.now().isoformat(timespec='seconds')}")
    append_job_log(log_path, "[cmd] " + " ".join(str(part) for part in cmd))

    def _run(jid=job_id, log_p=str(log_path), tmo=timeout_secs, command=list(cmd)):
        proc = None
        try:
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            proc = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                cwd=str(job_runtime.BASE_DIR), env=env,
            )
            job_runtime._active_procs[jid] = proc

            output_file = None
            deadline = time.time() + tmo

            for line in proc.stdout:
                if time.time() > deadline:
                    proc.kill()
                    job_runtime._set_job_error(
                        job_runtime._jobs[jid], "TIMEOUT",
                        f"超时（{tmo // 60} 分钟），进程已终止",
                        detail=f"timeout_secs={tmo}",
                    )
                    return
                line = line.rstrip()
                job_runtime._jobs[jid]["log"].append(line)
                append_job_log(job_runtime._jobs[jid].get("log_path"), line)
                job_runtime._update_transcription_state_from_stdout(job_runtime._jobs[jid], line)

                m_rp = re.search(r"\[resegment\] 鎵规 (\d+)/(\d+)", line)
                if m_rp:
                    job_runtime._jobs[jid]["step_detail"] = f"鎵规 {m_rp.group(1)}/{m_rp.group(2)}"
                    continue
                m_ap = re.search(r"\[analyzer\] (\d+)/(\d+) batches done", line)
                if m_ap:
                    job_runtime._jobs[jid]["step_detail"] = f"分析 {m_ap.group(1)}/{m_ap.group(2)} 批"
                    continue
                m_sp = re.search(r"\[smart-translate\] (\d+)/(\d+) batches done", line)
                if m_sp:
                    job_runtime._jobs[jid]["step_detail"] = f"智能断句翻译 {m_sp.group(1)}/{m_sp.group(2)} 批"
                    continue
                m_merge = re.search(r"\[analyzer\] 鍚堝苟鎵规 (\d+)/(\d+) 瀹屾垚", line)
                if m_merge:
                    job_runtime._jobs[jid]["step_detail"] = f"断句合并 {m_merge.group(1)}/{m_merge.group(2)} 批"
                    continue
                m_ipa = re.search(r"\[analyzer\] IPA鎵规 (\d+)/(\d+) 瀹屾垚", line)
                if m_ipa:
                    job_runtime._jobs[jid]["step_detail"] = f"IPA标注 {m_ipa.group(1)}/{m_ipa.group(2)} 批"
                    continue
                for pattern, step in job_runtime._STEP_SIGNALS:
                    if re.search(pattern, line):
                        job_runtime._advance_step(job_runtime._jobs[jid], step)
                        job_runtime._jobs[jid]["step_detail"] = ""
                        if step == "analyze":
                            deadline = max(deadline, time.time() + 25 * 60)
                        elif step == "ipa_annotate":
                            deadline = max(deadline, time.time() + 15 * 60)
                        break
                m = re.search(r"Generated lesson:\s*(.+\.html)", line)
                if m:
                    output_file = Path(m.group(1).strip()).name

            proc.wait()
            if (job_runtime._jobs[jid].get("error_info") or {}).get("code") == "USER_CANCELLED":
                append_job_log(job_runtime._jobs[jid].get("log_path"), "[job] cancelled")
                return
            if proc.returncode == 0:
                job_runtime._advance_step(job_runtime._jobs[jid], "render")
                job_runtime._complete_current_step(job_runtime._jobs[jid])
                job_runtime._jobs[jid]["status"]      = "done"
                job_runtime._jobs[jid]["output_file"] = output_file
                append_job_log(job_runtime._jobs[jid].get("log_path"), f"[job] done output_file={output_file or ''}")
                job_runtime.clear_lessons_cache()
                if output_file:
                    job_runtime.write_lesson_meta(output_file, job_runtime._jobs[jid])
            else:
                recent_log = "\n".join(job_runtime._jobs[jid]["log"][-30:])
                code, message = job_runtime._classify_failure(recent_log, proc.returncode)
                job_runtime._set_job_error(job_runtime._jobs[jid], code, message, detail=f"Exit {proc.returncode}")
                append_job_log(job_runtime._jobs[jid].get("log_path"), f"[job] error code={code} message={message} exit={proc.returncode}")
        except Exception as exc:
            code, message = job_runtime._classify_failure(str(exc))
            job_runtime._set_job_error(job_runtime._jobs[jid], code, message, detail=str(exc))
            append_job_log(
                job_runtime._jobs[jid].get("log_path") if jid in job_runtime._jobs else None,
                f"[job] exception code={code} detail={exc}",
            )
        finally:
            job_runtime._active_procs.pop(jid, None)

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job_id}
