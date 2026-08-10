"""High-quality, cached English speech synthesis through Edge neural voices."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import certifi

from webapp.storage import user_assets
from webapp.storage.lessons import OUTPUT_DIR

ENGINE_VERSION = "edge-neural-v1"
DEFAULT_VOICE = os.environ.get("TTS_VOICE", "en-US-AvaMultilingualNeural")
DEFAULT_RATE = os.environ.get("TTS_RATE", "-5%")
DEFAULT_PITCH = os.environ.get("TTS_PITCH", "+0Hz")
CACHE_DIR = OUTPUT_DIR / "tts_cache" / ENGINE_VERSION
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _cache_dir() -> Path:
    """多用户按当前用户隔离 TTS 缓存；单用户回退模块级 CACHE_DIR。"""
    return user_assets.user_output_subdir("tts_cache", ENGINE_VERSION, fallback=CACHE_DIR)


def _audio_key(text: str, *, voice: str, rate: str, pitch: str) -> str:
    payload = json.dumps(
        {"engine": ENGINE_VERSION, "voice": voice, "rate": rate, "pitch": pitch, "text": text},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _metadata_path(audio_path: Path) -> Path:
    return audio_path.with_suffix(audio_path.suffix + ".tts.json")


def is_current_tts_audio(
    audio_path: Path,
    text: str,
    *,
    voice: str = DEFAULT_VOICE,
    rate: str = DEFAULT_RATE,
    pitch: str = DEFAULT_PITCH,
) -> bool:
    if not audio_path.exists() or not _metadata_path(audio_path).exists():
        return False
    try:
        metadata = json.loads(_metadata_path(audio_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return metadata.get("key") == _audio_key(text, voice=voice, rate=rate, pitch=pitch)


def _load_edge_tts():
    # Some Windows certificate stores contain malformed entries. Use certifi's
    # verified CA bundle only while aiohttp builds its module-level SSL context.
    original = ssl.create_default_context

    def safe_context(*args, **kwargs):
        if not args and not kwargs:
            return original(cafile=certifi.where())
        return original(*args, **kwargs)

    ssl.create_default_context = safe_context
    try:
        import edge_tts
    finally:
        ssl.create_default_context = original
    return edge_tts


def _synthesize_edge_mp3(
    text: str,
    mp3_path: Path,
    *,
    voice: str,
    rate: str,
    pitch: str,
) -> None:
    edge_tts = _load_edge_tts()
    communicate = edge_tts.Communicate(text, voice=voice, rate=rate, pitch=pitch)
    asyncio.run(communicate.save(str(mp3_path)))


def _synthesize_edge_mp3_with_timestamps(
    text: str,
    mp3_path: Path,
    *,
    voice: str,
    rate: str,
    pitch: str,
) -> list[dict]:
    """单次 edge_tts 调用同时收音频流与 WordBoundary 事件（offset/duration 单位为 100ns）。"""
    edge_tts = _load_edge_tts()

    async def _run() -> list[dict]:
        # 块级文本长、并发下服务端可能限流：接收窗口放宽到 180s（默认 60s 易误杀）
        communicate = edge_tts.Communicate(
            text, voice=voice, rate=rate, pitch=pitch, boundary="WordBoundary",
            connect_timeout=15, receive_timeout=180,
        )
        boundaries: list[dict] = []
        with open(mp3_path, "wb") as handle:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    handle.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    boundaries.append({
                        "text": str(chunk.get("text") or ""),
                        "offset": float(chunk["offset"]) / 10_000_000,
                        "duration": float(chunk["duration"]) / 10_000_000,
                    })
        return boundaries

    return asyncio.run(_run())


def _ffmpeg_command() -> str:
    conda_ffmpeg = Path(sys.executable).parent / "Library" / "bin" / "ffmpeg.exe"
    if conda_ffmpeg.exists():
        return str(conda_ffmpeg)
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise FileNotFoundError("ffmpeg is required to create cached neural TTS WAV files")


def _transcode_to_wav(mp3_path: Path, wav_path: Path) -> None:
    completed = subprocess.run(
        [
            _ffmpeg_command(), "-y", "-v", "error", "-i", str(mp3_path),
            "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(wav_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0 or not wav_path.exists() or wav_path.stat().st_size == 0:
        raise RuntimeError((completed.stderr or completed.stdout or "Neural TTS transcoding failed").strip())


def synthesize_natural_speech(
    text: str,
    output_path: Path,
    *,
    voice: str = DEFAULT_VOICE,
    rate: str = DEFAULT_RATE,
    pitch: str = DEFAULT_PITCH,
) -> None:
    normalized = " ".join(str(text or "").split())
    if not normalized:
        raise ValueError("TTS text is empty")
    output_path = Path(output_path)
    key = _audio_key(normalized, voice=voice, rate=rate, pitch=pitch)
    metadata = {"engine": ENGINE_VERSION, "voice": voice, "rate": rate, "pitch": pitch, "key": key}
    if is_current_tts_audio(output_path, normalized, voice=voice, rate=rate, pitch=pitch):
        return
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.Lock())
    with lock:
        cache_dir = _cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f"{key}.wav"
        cache_meta = _metadata_path(cache_path)
        if not cache_path.exists() or not cache_meta.exists():
            with tempfile.NamedTemporaryFile(suffix=".mp3", dir=cache_dir, delete=False) as handle:
                mp3_path = Path(handle.name)
            with tempfile.NamedTemporaryFile(suffix=".wav", dir=cache_dir, delete=False) as handle:
                wav_path = Path(handle.name)
            try:
                _synthesize_edge_mp3(normalized, mp3_path, voice=voice, rate=rate, pitch=pitch)
                _transcode_to_wav(mp3_path, wav_path)
                wav_path.replace(cache_path)
                cache_meta.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
            finally:
                mp3_path.unlink(missing_ok=True)
                wav_path.unlink(missing_ok=True)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cache_path, output_path)
        _metadata_path(output_path).write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")


def _boundaries_path(audio_path: Path) -> Path:
    return audio_path.with_suffix(audio_path.suffix + ".boundaries.json")


def synthesize_natural_speech_with_timestamps(
    text: str,
    output_path: Path,
    *,
    voice: str = DEFAULT_VOICE,
    rate: str = DEFAULT_RATE,
    pitch: str = DEFAULT_PITCH,
) -> list[dict]:
    """整段文本一次合成，返回词边界 [{text, offset, duration}]（秒），供句级时间轴对齐。

    与 synthesize_natural_speech 共用缓存键；词边界随缓存落盘，命中时直接读回。
    """
    normalized = " ".join(str(text or "").split())
    if not normalized:
        raise ValueError("TTS text is empty")
    output_path = Path(output_path)
    key = _audio_key(normalized, voice=voice, rate=rate, pitch=pitch)
    metadata = {"engine": ENGINE_VERSION, "voice": voice, "rate": rate, "pitch": pitch, "key": key}
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.Lock())
    with lock:
        cache_dir = _cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f"{key}.wav"
        cache_meta = _metadata_path(cache_path)
        cache_bounds = _boundaries_path(cache_path)
        if not cache_path.exists() or not cache_meta.exists() or not cache_bounds.exists():
            with tempfile.NamedTemporaryFile(suffix=".mp3", dir=cache_dir, delete=False) as handle:
                mp3_path = Path(handle.name)
            with tempfile.NamedTemporaryFile(suffix=".wav", dir=cache_dir, delete=False) as handle:
                wav_path = Path(handle.name)
            try:
                boundaries = _synthesize_edge_mp3_with_timestamps(
                    normalized, mp3_path, voice=voice, rate=rate, pitch=pitch
                )
                _transcode_to_wav(mp3_path, wav_path)
                wav_path.replace(cache_path)
                cache_meta.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
                cache_bounds.write_text(json.dumps(boundaries, ensure_ascii=False), encoding="utf-8")
            finally:
                mp3_path.unlink(missing_ok=True)
                wav_path.unlink(missing_ok=True)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cache_path, output_path)
        _metadata_path(output_path).write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        return json.loads(cache_bounds.read_text(encoding="utf-8"))
