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

from webapp.storage.lessons import OUTPUT_DIR

ENGINE_VERSION = "edge-neural-v1"
DEFAULT_VOICE = os.environ.get("TTS_VOICE", "en-US-AvaMultilingualNeural")
DEFAULT_RATE = os.environ.get("TTS_RATE", "-5%")
DEFAULT_PITCH = os.environ.get("TTS_PITCH", "+0Hz")
CACHE_DIR = OUTPUT_DIR / "tts_cache" / ENGINE_VERSION
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


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
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path = CACHE_DIR / f"{key}.wav"
        cache_meta = _metadata_path(cache_path)
        if not cache_path.exists() or not cache_meta.exists():
            with tempfile.NamedTemporaryFile(suffix=".mp3", dir=CACHE_DIR, delete=False) as handle:
                mp3_path = Path(handle.name)
            with tempfile.NamedTemporaryFile(suffix=".wav", dir=CACHE_DIR, delete=False) as handle:
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
