from __future__ import annotations

import importlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from schemas import Segment, SourceBundle
from sources.subtitle_parser import parse_subtitle_file
from sources.transcript_cache import (
    extract_segments_from_lesson_html,
    load_transcript_cache,
    save_transcript_cache,
)

# _GROQ_MAX_BYTES
_GROQ_MAX_BYTES = 24 * 1024 * 1024   # 24 MB 留 1 MB 余量
_GROQ_TARGET_BYTES = int(_GROQ_MAX_BYTES * 0.85)
_GROQ_COMPRESS_BITRATE = "64k"


def build_local_video_lesson(
    video_path: str,
    transcript_path: str | None = None,
    whisper_model: str = "large-v3",
    bvid: str | None = None,
    output_dir: Path | None = None,
) -> SourceBundle:
    video = Path(video_path).expanduser().resolve()
    if not video.exists():
        raise FileNotFoundError(f"本地视频不存在: {video}")

    if transcript_path:
        transcript = Path(transcript_path).expanduser().resolve()
        if not transcript.exists():
            raise FileNotFoundError(f"字幕文件不存在: {transcript}")
        segments = parse_subtitle_file(transcript)
        print("[STEP:subtitle]", flush=True)
        print(f"  已获取本地字幕（{len(segments)} 句）", flush=True)
    else:
        segments = _transcribe_with_optional_whisper(
            video, whisper_model, bvid=bvid, output_dir=output_dir
        )

    return SourceBundle(
        source_type="local_video",
        title=video.stem,
        source_value=str(video),
        segments=segments,
        local_video=video,
    )


_MODEL_INFO = {
    "base":     {"size": "~150 MB", "speed": "快（约3-5分钟/小时音频）",   "accuracy": "基础"},
    "medium":   {"size": "~1.5 GB", "speed": "中等（约10-15分钟/小时音频）", "accuracy": "较好"},
    "large-v3": {"size": "~3 GB",   "speed": "慢（约25-35分钟/小时音频）",  "accuracy": "最佳"},
}


def _run_ffmpeg(command: list[str]) -> None:
    subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


def _find_ffprobe() -> str:
    from sources.media_bins import find_ffprobe
    return find_ffprobe()


def _probe_audio_duration(audio_path: Path) -> float:
    try:
        result = subprocess.run(
            [
                _find_ffprobe(), "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        return max(0.0, float(result.stdout.strip() or 0.0))
    except Exception as exc:
        print(f"[GROQ_WARN] ffprobe_duration_failed reason={exc}", flush=True)
        return 0.0


def _compress_audio_for_groq(input_path: Path, output_path: Path) -> None:
    _run_ffmpeg([
        _find_ffmpeg(), "-y", "-i", str(input_path),
        "-vn", "-ac", "1", "-b:a", _GROQ_COMPRESS_BITRATE, str(output_path),
    ])


def _split_audio_for_groq(input_path: Path, tmpdir: Path) -> list[tuple[Path, float]]:
    duration = _probe_audio_duration(input_path)
    size = input_path.stat().st_size
    if duration > 0:
        segment_seconds = max(10, int(duration * _GROQ_TARGET_BYTES / max(size, 1)))
    else:
        segment_seconds = 15 * 60
    segment_seconds = min(segment_seconds, 15 * 60)

    print(
        f"[GROQ_SPLIT_START] bytes={size} max_bytes={_GROQ_MAX_BYTES} "
        f"segment_seconds={segment_seconds}",
        flush=True,
    )
    pattern = tmpdir / "groq-part-%04d.mp3"
    while True:
        _run_ffmpeg([
            _find_ffmpeg(), "-y", "-i", str(input_path),
            "-f", "segment", "-segment_time", str(segment_seconds),
            "-reset_timestamps", "1", "-c", "copy", str(pattern),
        ])
        parts = sorted(tmpdir.glob("groq-part-*.mp3"))
        if not parts:
            raise RuntimeError("Groq split produced no chunks")

        oversize = [part for part in parts if part.stat().st_size > _GROQ_MAX_BYTES]
        if not oversize:
            break
        if segment_seconds <= 1:
            largest = max(part.stat().st_size for part in oversize)
            raise RuntimeError(
                f"Groq split still has oversized chunks: count={len(oversize)} "
                f"largest={largest} max={_GROQ_MAX_BYTES}"
            )

        next_segment_seconds = max(1, segment_seconds // 2)
        print(
            f"[GROQ_SPLIT_RETRY] oversized_parts={len(oversize)} "
            f"segment_seconds={segment_seconds} next_segment_seconds={next_segment_seconds}",
            flush=True,
        )
        for part in parts:
            part.unlink(missing_ok=True)
        segment_seconds = next_segment_seconds

    offset = 0.0
    result: list[tuple[Path, float]] = []
    for part in parts:
        result.append((part, offset))
        offset += _probe_audio_duration(part) or segment_seconds
    print(f"[GROQ_SPLIT_DONE] chunks={len(result)}", flush=True)
    return result


def _groq_seg_value(seg, key: str, default):
    if isinstance(seg, dict):
        return seg.get(key, default)
    return getattr(seg, key, default)


def _groq_segments_to_items(raw_segs, offset: float, next_index: int) -> list[Segment]:
    items: list[Segment] = []
    for seg in raw_segs:
        text = re.sub(r"\s+", " ", str(_groq_seg_value(seg, "text", ""))).strip()
        if not text:
            continue
        start = float(_groq_seg_value(seg, "start", 0.0)) + offset
        end = float(_groq_seg_value(seg, "end", 0.0)) + offset
        items.append(Segment(index=next_index, text=text, start=start, end=end))
        next_index += 1
    return items


def _call_api_whisper(client, model: str, audio_path: Path):
    with open(audio_path, "rb") as f:
        return client.audio.transcriptions.create(
            model=model,
            file=f,
            response_format="verbose_json",
            timestamp_granularities=["segment"],
            language="en",
        )


def _transcribe_via_api(client, model: str, audio_path: Path) -> list[Segment]:
    """通过 OpenAI 兼容的 Whisper API 转录（Groq / OpenAI 等），必要时压缩并切片长音频。"""
    print("[STEP:whisper_transcribe]", flush=True)
    with tempfile.TemporaryDirectory(prefix="english-groq-") as tmp:
        tmpdir = Path(tmp)
        transcribe_path = audio_path
        original_size = audio_path.stat().st_size

        if original_size > _GROQ_MAX_BYTES:
            transcribe_path = tmpdir / "groq-compressed.mp3"
            print(
                f"[GROQ_COMPRESS_START] bytes={original_size} max_bytes={_GROQ_MAX_BYTES}",
                flush=True,
            )
            _compress_audio_for_groq(audio_path, transcribe_path)
            print(
                f"[GROQ_COMPRESS_DONE] bytes={transcribe_path.stat().st_size}",
                flush=True,
            )

        if transcribe_path.stat().st_size <= _GROQ_MAX_BYTES:
            try:
                print(f"[GROQ_CHUNK] current=1 total=1 bytes={transcribe_path.stat().st_size}", flush=True)
                resp = _call_api_whisper(client, model, transcribe_path)
            except Exception as exc:
                print(f"[GROQ_ERROR] chunk=1 total=1 reason={exc}", flush=True)
                raise
            raw_segs = getattr(resp, "segments", []) or []
            print("[WHISPER_PROGRESS] 100%", flush=True)
            return _groq_segments_to_items(raw_segs, 0.0, 1)

        chunks = _split_audio_for_groq(transcribe_path, tmpdir)
        items: list[Segment] = []
        total = len(chunks)
        for chunk_index, (chunk_path, offset) in enumerate(chunks, 1):
            try:
                print(
                    f"[GROQ_CHUNK] current={chunk_index} total={total} "
                    f"bytes={chunk_path.stat().st_size} offset={offset:.3f}",
                    flush=True,
                )
                resp = _call_api_whisper(client, model, chunk_path)
            except Exception as exc:
                print(f"[GROQ_ERROR] chunk={chunk_index} total={total} reason={exc}", flush=True)
                raise
            raw_segs = getattr(resp, "segments", []) or []
            items.extend(_groq_segments_to_items(raw_segs, offset, len(items) + 1))
            print(f"[WHISPER_PROGRESS] {min(99, int(chunk_index / total * 100))}%", flush=True)
        print("[WHISPER_PROGRESS] 100%", flush=True)
        return items


def _transcribe_with_groq(audio_path: Path) -> list[Segment]:
    """使用 Groq Whisper API 转录。"""
    from groq import Groq
    client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
    return _transcribe_via_api(client, "whisper-large-v3-turbo", audio_path)


def _transcribe_with_openai(audio_path: Path) -> list[Segment]:
    """使用 OpenAI Whisper API 转录（云端部署的转录兜底，Groq 屏蔽机房 IP 时可用）。"""
    from openai import OpenAI
    client = OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"),
        base_url=os.environ.get("OPENAI_BASE_URL") or None,
    )
    model = os.environ.get("OPENAI_WHISPER_MODEL", "whisper-1")
    return _transcribe_via_api(client, model, audio_path)


def _paraformer_call_with_timeout(wav_path: Path) -> "object":
    """Recognition.call 无超时机制，websocket 卡死会永久挂住管线（2026-08-06 云端实测）。

    用线程池加超时兜底：超时后放弃本次调用换新 Recognition 重试，
    卡死线程随进程存活泄漏（偶发可接受）。超时按文件大小估算，
    实测 18MB wav 约 111s，给 30s/MB 且下限 300s。
    """
    import concurrent.futures
    from http import HTTPStatus

    from dashscope.audio.asr import Recognition

    timeout_s = max(
        300.0,
        wav_path.stat().st_size / 1024 / 1024 * 30.0,
    )
    timeout_s = float(os.environ.get("PARAFORMER_TIMEOUT_S", timeout_s))
    max_attempts = int(os.environ.get("PARAFORMER_MAX_ATTEMPTS", "2"))

    for attempt in range(1, max_attempts + 1):
        recognition = Recognition(
            model=os.environ.get("PARAFORMER_MODEL", "paraformer-realtime-v2"),
            format="wav",
            sample_rate=16000,
            language_hints=["en"],  # 英语学习素材固定按英文识别，避免语种误判
            callback=None,
        )
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(recognition.call, str(wav_path))
        try:
            result = future.result(timeout=timeout_s)
            pool.shutdown(wait=False)
        except concurrent.futures.TimeoutError:
            pool.shutdown(wait=False)  # 卡死线程无法强杀，放弃并换新连接重试
            print(
                f"[PARAFORMER_TIMEOUT] 第 {attempt}/{max_attempts} 次超过 "
                f"{timeout_s:.0f}s 未返回，重试",
                flush=True,
            )
            if attempt == max_attempts:
                raise RuntimeError(
                    f"Paraformer 转录超时：{max_attempts} 次尝试均超过 "
                    f"{timeout_s:.0f}s 未返回"
                )
            continue
        if result.status_code != HTTPStatus.OK:
            raise RuntimeError(
                f"Paraformer 转录失败：{result.message}"
                f"（request_id={result.get_request_id()}）"
            )
        return result
    raise RuntimeError("Paraformer 转录失败：未知错误")  # pragma: no cover


def _transcribe_with_paraformer(audio_path: Path) -> list[Segment]:
    """使用阿里云百炼 Paraformer 实时语音识别转录（Recognition.call 本地文件直传）。

    云端部署的转录兜底：Groq 屏蔽机房 IP、本地大模型跑不动时使用。
    非流式调用直接读本地文件，无需公网 URL；结果带句级 begin_time/end_time（毫秒）。
    """
    import dashscope

    dashscope.api_key = os.environ.get("DASHSCOPE_API_KEY")
    # 默认连北京 endpoint；用国际站（新加坡）Key 时设
    # DASHSCOPE_WS_URL=wss://dashscope-intl.aliyuncs.com/api/v1
    ws_url = os.environ.get("DASHSCOPE_WS_URL")
    if ws_url:
        dashscope.base_websocket_api_url = ws_url

    print("[STEP:whisper_transcribe]", flush=True)
    with tempfile.TemporaryDirectory(prefix="english-paraformer-") as tmp:
        # Paraformer 的 wav 必须是 PCM 编码，统一转成 16kHz 单声道 PCM wav 再识别
        wav_path = Path(tmp) / "paraformer-16k.wav"
        print(f"[PARAFORMER_CONVERT] {audio_path.name} -> 16kHz mono pcm wav", flush=True)
        _run_ffmpeg([
            _find_ffmpeg(), "-y", "-i", str(audio_path),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav_path),
        ])
        result = _paraformer_call_with_timeout(wav_path)

    sentences = result.get_sentence() or []
    if isinstance(sentences, dict):  # 兜底：个别版本单句结果返回 dict
        sentences = [sentences]
    items: list[Segment] = []
    for sentence in sentences:
        text = re.sub(r"\s+", " ", str(sentence.get("text", ""))).strip()
        if not text:
            continue
        items.append(
            Segment(
                index=len(items) + 1,
                text=text,
                start=float(sentence.get("begin_time") or 0) / 1000.0,
                end=float(sentence.get("end_time") or 0) / 1000.0,
                words=[
                    {
                        "text": str(w.get("text") or ""),
                        "start": float(w.get("begin_time") or 0) / 1000.0,
                        "end": float(w.get("end_time") or 0) / 1000.0,
                        "punctuation": str(w.get("punctuation") or ""),
                    }
                    for w in (sentence.get("words") or [])
                ],
            )
        )
    print("[WHISPER_PROGRESS] 100%", flush=True)
    return items


def _transcribe_with_optional_whisper(
    video: Path,
    whisper_model: str = "large-v3",
    bvid: str | None = None,
    output_dir: Path | None = None,
) -> list[Segment]:
    audio_path = _extract_audio_for_whisper(video)

    # 1. 优先查转录缓存
    cached = load_transcript_cache(audio_path, whisper_model)
    if cached:
        print("[STEP:whisper_transcribe]", flush=True)
        print(f"  复用已缓存转录：{len(cached)} 句", flush=True)
        print("[WHISPER_PROGRESS] 100%", flush=True)
        return cached

    # 2. 缓存未命中 → 尝试从已生成课程 HTML 提取（跳过重新 Whisper）
    if bvid and output_dir:
        from_html = extract_segments_from_lesson_html(bvid, output_dir)
        if from_html:
            save_transcript_cache(audio_path, whisper_model, from_html)
            print("[STEP:whisper_transcribe]", flush=True)
            print(f"  从已有课程提取转录并写入缓存：{len(from_html)} 句", flush=True)
            print("[WHISPER_PROGRESS] 100%", flush=True)
            return from_html

    if whisper_model == "paraformer":
        if not os.environ.get("DASHSCOPE_API_KEY"):
            raise RuntimeError(
                "已选择 Paraformer 转录，但未配置 DASHSCOPE_API_KEY。"
                "请在资源管理 → API 设置中填写阿里云百炼 API-KEY。"
            )
        print("  使用阿里云 Paraformer 语音识别转录…", flush=True)
        segments = _transcribe_with_paraformer(audio_path)
        save_transcript_cache(audio_path, whisper_model, segments)
        return segments

    if whisper_model == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError(
                "已选择 OpenAI 转录，但未配置 OPENAI_API_KEY。"
                "请在资源管理 → API 设置中填写 OpenAI Key。"
            )
        print("  使用 OpenAI Whisper API 转录…", flush=True)
        segments = _transcribe_with_openai(audio_path)
        save_transcript_cache(audio_path, whisper_model, segments)
        return segments

    if whisper_model == "groq":
        if not os.environ.get("GROQ_API_KEY"):
            raise RuntimeError(
                "已选择 Groq 转录，但未配置 GROQ_API_KEY。"
                "请在资源管理 → API 设置中填写 Groq Key。"
            )
        print("  使用 Groq Whisper API 转录…", flush=True)
        segments = _transcribe_with_groq(audio_path)
        save_transcript_cache(audio_path, whisper_model, segments)
        return segments

    # 云端部署无本地模型且已配 OpenAI key 时，自动走 OpenAI Whisper API，
    # 避免在受限服务器上下载/运行大模型。
    if os.environ.get("ELT_DEPLOYMENT") == "cloud" and os.environ.get("OPENAI_API_KEY"):
        print("  云端环境：改用 OpenAI Whisper API 转录…", flush=True)
        segments = _transcribe_with_openai(audio_path)
        save_transcript_cache(audio_path, whisper_model, segments)
        return segments

    # 云端已配百炼 key 时兜底 paraformer——3.6GB 小服务器跑 large-v3 会直接把机器拖死
    if os.environ.get("ELT_DEPLOYMENT") == "cloud" and os.environ.get("DASHSCOPE_API_KEY"):
        print("  云端环境：改用阿里云 Paraformer 语音识别转录…", flush=True)
        segments = _transcribe_with_paraformer(audio_path)
        save_transcript_cache(audio_path, "paraformer", segments)
        return segments

    try:
        faster_whisper = importlib.import_module("faster_whisper")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "未提供 transcript，且当前环境未安装 faster-whisper。"
            "请先 `pip install faster-whisper`，或传入 --transcript-file。"
        ) from exc
    info = _MODEL_INFO.get(whisper_model, {})
    print(f"  加载 Whisper {whisper_model} 模型（{info.get('size','')}，{info.get('speed','')}）…")
    model = faster_whisper.WhisperModel(
        whisper_model,
        device="cpu",
        compute_type="int8_float32",  # AVX-512 optimized for Intel Core Ultra
        cpu_threads=12,
        num_workers=1,
    )
    print("[STEP:whisper_transcribe]")
    segments_gen, info = model.transcribe(str(audio_path), language="en", vad_filter=True)
    total_dur = getattr(info, "duration", 0.0)
    items: list[Segment] = []
    sentence_index = 1
    last_pct = -1
    for segment in segments_gen:
        if total_dur > 0:
            pct = min(99, int(segment.end / total_dur * 100))
            if pct >= last_pct + 5:
                print(f"[WHISPER_PROGRESS] {pct}%", flush=True)
                last_pct = pct
        text = re.sub(r"\s+", " ", segment.text).strip()
        if not text:
            continue
        items.append(
            Segment(
                index=sentence_index,
                text=text,
                start=float(segment.start),
                end=float(segment.end),
            )
        )
        sentence_index += 1
    save_transcript_cache(audio_path, whisper_model, items)
    return items


_AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".ogg", ".flac", ".aac"}


def _find_ffmpeg() -> str:
    """找 ffmpeg 可执行文件：FFMPEG_PATH → Release 包内 → PATH → conda。"""
    from sources.media_bins import find_ffmpeg
    return find_ffmpeg()


def _extract_audio_for_whisper(video: Path) -> Path:
    if video.suffix.lower() in _AUDIO_EXTS:
        return video  # already audio, faster-whisper + av can read directly
    tmpdir = Path(tempfile.mkdtemp(prefix="english-audio-"))
    audio_path = tmpdir / f"{video.stem}.wav"
    command = [
        _find_ffmpeg(), "-y", "-i", str(video),
        "-vn", "-ac", "1", "-ar", "16000", str(audio_path),
    ]
    subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return audio_path
