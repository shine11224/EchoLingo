import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


def _fake_whisper_module(words_per_segment):
    """Install a fake faster_whisper module returning fixed word timestamps."""
    import types

    class FakeWord:
        def __init__(self, word, start, end):
            self.word = word
            self.start = start
            self.end = end

    class FakeSegment:
        def __init__(self, words):
            self.words = words

    class FakeModel:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, path, **kwargs):
            segments = [
                FakeSegment([FakeWord(w, s, e) for w, s, e in group])
                for group in words_per_segment
            ]
            return segments, None

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = FakeModel
    sys.modules["faster_whisper"] = module
    # 重置 transcribe_words 的进程级模型缓存，避免跨测试泄漏
    from webapp.services import whisper_alignment

    whisper_alignment._MODEL = None
    whisper_alignment._MODEL_NAME = ""
    return module


def _patch_lesson_env(monkeypatch, tmp_path, segments):
    """Point alignment output/audio at tmp_path and stub db lesson access."""
    import db
    from webapp.services import mfa_alignment, whisper_alignment

    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"fake")
    monkeypatch.setattr(
        whisper_alignment, "OUTPUT_DIR", tmp_path, raising=False
    )
    monkeypatch.setattr(
        mfa_alignment, "OUTPUT_DIR", tmp_path, raising=False
    )
    monkeypatch.setattr(
        db, "get_v2_lesson",
        lambda lesson_id: {
            "id": lesson_id,
            "source_type": "bilibili",
            "media_url": "",
            "source_url": "",
        },
    )
    monkeypatch.setattr(
        db, "get_v2_subtitle_segments", lambda lesson_id: segments
    )
    monkeypatch.setattr(
        mfa_alignment, "_resolve_lesson_audio", lambda lesson: audio
    )
    monkeypatch.setattr(
        whisper_alignment, "_resolve_lesson_audio", lambda lesson: audio
    )
    return audio


def test_run_whisper_alignment_writes_real_word_times(monkeypatch, tmp_path):
    _fake_whisper_module([
        [("Hello", 0.10, 0.42), ("world", 0.50, 0.96)],
        [("Second", 1.30, 1.71), ("sentence", 1.80, 2.40)],
    ])
    segments = [
        {"index": 1, "start": 0.0, "end": 1.0, "text": "Hello world."},
        {"index": 2, "start": 1.0, "end": 2.5, "text": "Second sentence."},
    ]
    _patch_lesson_env(monkeypatch, tmp_path, segments)

    from webapp.services import whisper_alignment

    result = whisper_alignment.run_whisper_alignment(7, force=True)
    assert result["status"] == "ready"
    assert result["model"].startswith("faster-whisper:")
    assert result["word_count"] == 4

    sentences = result["sentences"]
    assert len(sentences) == 2
    first = sentences[0]
    # times come from whisper words, not the interpolated subtitle span
    # (mapped start - 0.12s padding, clamped at 0)
    assert first["start_seconds"] == 0.0
    assert first["boundary_confidence"] == "high"
    assert [w["text"] for w in first["words"]] == ["Hello", "world"]
    assert first["words"][0]["start"] == 0.10

    written = json.loads(
        (tmp_path / "v2_alignments" / "7" / "alignment.json").read_text(encoding="utf-8")
    )
    assert written["status"] == "ready"


def test_mfa_fallback_to_whisper_when_mfa_missing(monkeypatch, tmp_path):
    _fake_whisper_module([[("Only", 0.05, 0.30), ("words", 0.35, 0.70)]])
    segments = [{"index": 1, "start": 0.0, "end": 1.0, "text": "Only words."}]
    _patch_lesson_env(monkeypatch, tmp_path, segments)

    from webapp.services import mfa_alignment

    monkeypatch.setattr(mfa_alignment, "_mfa_command", lambda: [])
    result = mfa_alignment.run_lesson_alignment(9, force=True)
    assert result["status"] == "ready"
    assert result["model"].startswith("faster-whisper:")
    assert result["sentences"][0]["words"][0]["text"] == "Only"


def test_mfa_unavailable_and_no_whisper_still_raises(monkeypatch, tmp_path):
    segments = [{"index": 1, "start": 0.0, "end": 1.0, "text": "Hello."}]
    _patch_lesson_env(monkeypatch, tmp_path, segments)

    from webapp.services import mfa_alignment, whisper_alignment

    monkeypatch.setattr(mfa_alignment, "_mfa_command", lambda: [])
    monkeypatch.setattr(whisper_alignment, "whisper_available", lambda: False)
    import pytest

    with pytest.raises(RuntimeError, match="MFA is unavailable"):
        mfa_alignment.run_lesson_alignment(11, force=True)


def _fake_groq_module(words, fail=False):
    """Install a fake groq module; words = list of dicts with word/start/end."""
    import types

    class FakeTranscriptions:
        def create(self, **kwargs):
            if fail:
                raise RuntimeError("groq boom")
            resp = types.SimpleNamespace()
            resp.words = [dict(w) for w in words]
            return resp

    class FakeGroq:
        def __init__(self, *args, **kwargs):
            self.audio = types.SimpleNamespace(transcriptions=FakeTranscriptions())

    module = types.ModuleType("groq")
    module.Groq = FakeGroq
    sys.modules["groq"] = module
    return module


def test_groq_alignment_words(monkeypatch, tmp_path):
    _fake_groq_module([
        {"word": "So,", "start": 0.24, "end": 0.40},
        {"word": "here", "start": 0.40, "end": 1.02},
    ])
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    tiny = tmp_path / "tiny.mp3"
    tiny.write_bytes(b"fake-audio")

    from webapp.services import whisper_alignment

    words = whisper_alignment.transcribe_words_groq(tiny)
    assert words == [
        {"label": "So,", "start": 0.24, "end": 0.40},
        {"label": "here", "start": 0.40, "end": 1.02},
    ]


def test_mfa_fallback_prefers_groq(monkeypatch, tmp_path):
    _fake_groq_module([
        {"word": "Only", "start": 0.10, "end": 0.30},
        {"word": "words", "start": 0.35, "end": 0.70},
    ])
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    segments = [{"index": 1, "start": 0.0, "end": 1.0, "text": "Only words."}]
    audio = _patch_lesson_env(monkeypatch, tmp_path, segments)

    from webapp.services import mfa_alignment

    monkeypatch.setattr(mfa_alignment, "_mfa_command", lambda: [])
    result = mfa_alignment.run_lesson_alignment(13, force=True)
    assert result["status"] == "ready"
    assert result["model"].startswith("groq:")
    assert [w["text"] for w in result["sentences"][0]["words"]] == ["Only", "words"]
    assert result["sentences"][0]["words"][0]["start"] == 0.10


def test_mfa_fallback_groq_failure_uses_whisper(monkeypatch, tmp_path):
    _fake_groq_module([], fail=True)
    _fake_whisper_module([[("Real", 0.05, 0.30), ("fallback", 0.35, 0.80)]])
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    segments = [{"index": 1, "start": 0.0, "end": 1.0, "text": "Real fallback."}]
    _patch_lesson_env(monkeypatch, tmp_path, segments)

    from webapp.services import mfa_alignment

    monkeypatch.setattr(mfa_alignment, "_mfa_command", lambda: [])
    result = mfa_alignment.run_lesson_alignment(15, force=True)
    assert result["model"].startswith("faster-whisper:")
    assert [w["text"] for w in result["sentences"][0]["words"]] == ["Real", "fallback"]


def test_apply_aligned_unit_times(monkeypatch, tmp_path):
    from webapp.services import mfa_alignment

    payload = {
        "status": "ready",
        "sentences": [
            {
                "key": 0,
                "text": "Hello world.",
                "start_seconds": 0.12,
                "end_seconds": 0.98,
                "boundary_confidence": "high",
            },
            {
                "key": 1,
                "text": "Different text entirely.",
                "start_seconds": 1.30,
                "end_seconds": 2.00,
                "boundary_confidence": "high",
            },
            {
                "key": 2,
                "text": "Low confidence.",
                "start_seconds": 2.30,
                "end_seconds": 3.00,
                "boundary_confidence": "fallback",
            },
        ],
    }
    align_dir = tmp_path / "v2_alignments" / "5"
    align_dir.mkdir(parents=True)
    (align_dir / "alignment.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    monkeypatch.setattr(mfa_alignment, "OUTPUT_DIR", tmp_path, raising=False)

    units = [
        {"index": 0, "text": "Hello world.", "start": 0.0, "end": 1.0},
        {"index": 1, "text": "Text mismatch stays.", "start": 1.0, "end": 2.0},
        {"index": 2, "text": "Low confidence.", "start": 2.0, "end": 3.0},
    ]
    out = mfa_alignment.apply_aligned_unit_times(5, units)
    assert out[0]["start"] == 0.12 and out[0]["end"] == 0.98
    assert out[0]["timing_source"] == "alignment"
    # text mismatch → untouched
    assert out[1]["start"] == 1.0 and "timing_source" not in out[1]
    # fallback confidence → untouched
    assert out[2]["start"] == 2.0 and "timing_source" not in out[2]
