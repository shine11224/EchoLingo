"""faster-whisper word-level alignment — the cloud fallback for MFA.

paraformer-realtime-v2 word timestamps are uniformly interpolated (verified
against the live DashScope API on 2026-08-07: every word in a sentence gets an
equal share of the sentence duration), so they cannot drive per-word playback
sync.  faster-whisper with word_timestamps=True produces real word boundaries
on CPU within the 3.6 GiB cloud budget.  Results reuse the MFA alignment.json
format via project_words_to_sentences(), so the intensive page and subtitle
endpoint consume them transparently.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import db
from webapp.services.mfa_alignment import (
    _resolve_lesson_audio,
    _result_path,
    _status_path,
    _write_json,
    load_lesson_alignment,
    project_words_to_sentences,
    _now,
)
from webapp.services.v2_translation import build_translation_units
from webapp.storage.lessons import OUTPUT_DIR

_WHISPER_LOCK = threading.Lock()
_MODEL = None
_MODEL_NAME = ""


def whisper_align_model_name() -> str:
    return os.environ.get("WHISPER_ALIGN_MODEL", "small.en").strip() or "small.en"


def whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


def groq_available() -> bool:
    """Groq whisper API（large-v3-turbo）——速度快几百倍且词边界更准，优先于本地 small.en。"""
    return bool(os.environ.get("GROQ_API_KEY"))


def groq_align_model_name() -> str:
    return os.environ.get("GROQ_ALIGN_MODEL", "whisper-large-v3-turbo").strip() or "whisper-large-v3-turbo"


def _word_attr(word, key):
    if isinstance(word, dict):
        return word.get(key)
    return getattr(word, key, None)


def transcribe_words_groq(audio_path: Path) -> list[dict]:
    """Groq whisper-large-v3-turbo 词级时间戳；复用 baidu.py 的压缩/切片（24MB 上限）。"""
    import tempfile
    import time

    from groq import Groq
    from sources.baidu import (
        _GROQ_MAX_BYTES,
        _compress_audio_for_groq,
        _split_audio_for_groq,
    )

    client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

    def call_chunk(chunk_path: Path):
        for attempt in range(3):
            try:
                with open(chunk_path, "rb") as handle:
                    return client.audio.transcriptions.create(
                        file=(chunk_path.name, handle, "audio/mpeg"),
                        model=groq_align_model_name(),
                        response_format="verbose_json",
                        timestamp_granularities=["word"],
                        language="en",
                    )
            except Exception as exc:
                # 免费额度 429：短等待后重试，其余错误直接抛出
                if "429" in str(exc) and attempt < 2:
                    time.sleep(20 * (attempt + 1))
                    continue
                raise

    def collect(resp, offset: float, out: list[dict]) -> None:
        for word in getattr(resp, "words", None) or []:
            label = str(_word_attr(word, "word") or "").strip()
            start = _word_attr(word, "start")
            end = _word_attr(word, "end")
            if not label or start is None or end is None:
                continue
            out.append(
                {"label": label, "start": float(start) + offset, "end": float(end) + offset}
            )

    words: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="english-groq-align-") as tmp:
        tmpdir = Path(tmp)
        transcribe_path = audio_path
        if audio_path.stat().st_size > _GROQ_MAX_BYTES:
            transcribe_path = tmpdir / "groq-compressed.mp3"
            _compress_audio_for_groq(audio_path, transcribe_path)
        if transcribe_path.stat().st_size <= _GROQ_MAX_BYTES:
            collect(call_chunk(transcribe_path), 0.0, words)
            return words
        for chunk_path, offset in _split_audio_for_groq(transcribe_path, tmpdir):
            collect(call_chunk(chunk_path), offset, words)
    return words


def _model_cache_dir() -> Path:
    # /app/output is a persisted docker volume on the cloud; the container
    # filesystem itself is ephemeral, so models must not download elsewhere.
    path = OUTPUT_DIR / ".cache" / "whisper-models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _get_model():
    global _MODEL, _MODEL_NAME
    name = whisper_align_model_name()
    if _MODEL is not None and _MODEL_NAME == name:
        return _MODEL
    from faster_whisper import WhisperModel

    _MODEL = WhisperModel(
        name,
        device="cpu",
        compute_type="int8",
        cpu_threads=max(1, min(4, (os.cpu_count() or 2))),
        num_workers=1,
        download_root=str(_model_cache_dir()),
    )
    _MODEL_NAME = name
    return _MODEL


def transcribe_words(audio_path: Path) -> list[dict]:
    """Transcribe with word timestamps; returns [{label, start, end}] seconds."""
    model = _get_model()
    segments, _info = model.transcribe(
        str(audio_path),
        language="en",
        vad_filter=True,
        word_timestamps=True,
    )
    words: list[dict] = []
    for segment in segments:
        for word in segment.words or []:
            label = str(word.word or "").strip()
            if not label or word.start is None or word.end is None:
                continue
            words.append(
                {"label": label, "start": float(word.start), "end": float(word.end)}
            )
    return words


def run_whisper_alignment(lesson_id: int, *, force: bool = False, engine: str = "whisper") -> dict:
    lesson_id = int(lesson_id)
    if not force:
        cached = load_lesson_alignment(lesson_id)
        if cached:
            return cached
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise ValueError("Lesson not found")
    if engine == "groq":
        transcribe = transcribe_words_groq
        model_label = f"groq:{groq_align_model_name()}"
    else:
        transcribe = transcribe_words
        model_label = f"faster-whisper:{whisper_align_model_name()}"
    with _WHISPER_LOCK:
        _write_json(
            _status_path(lesson_id),
            {
                "lesson_id": lesson_id,
                "status": "running",
                "updated_at": _now(),
                "error": "",
            },
        )
        try:
            audio_path = _resolve_lesson_audio(lesson)
            units = build_translation_units(db.get_v2_subtitle_segments(lesson_id))
            units = [unit for unit in units if str(unit.get("text") or "").strip()]
            if not units:
                raise RuntimeError("Lesson has no complete subtitle sentences to align")
            print(
                f"[word-align:{engine}] lesson {lesson_id}: {model_label} 转写词级时间戳…",
                flush=True,
            )
            words = transcribe(audio_path)
            if not words:
                raise RuntimeError(f"{model_label} returned no word timestamps")
            sentences = project_words_to_sentences(units, words)
            result = {
                "lesson_id": lesson_id,
                "status": "ready",
                "model": model_label,
                "updated_at": _now(),
                "audio_path": str(audio_path),
                "sentence_count": len(sentences),
                "word_count": len(words),
                "sentences": sentences,
            }
            _write_json(_result_path(lesson_id), result)
            _write_json(
                _status_path(lesson_id),
                {
                    "lesson_id": lesson_id,
                    "status": "ready",
                    "updated_at": result["updated_at"],
                    "error": "",
                },
            )
            print(
                f"[word-align:{engine}] lesson {lesson_id}: {len(sentences)} 句 / {len(words)} 词完成",
                flush=True,
            )
            return result
        except Exception as exc:
            _write_json(
                _status_path(lesson_id),
                {
                    "lesson_id": lesson_id,
                    "status": "failed",
                    "updated_at": _now(),
                    "error": str(exc),
                },
            )
            raise
