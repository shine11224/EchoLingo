from pathlib import Path
import os
import sys

from fastapi.testclient import TestClient


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


def test_neural_tts_generates_versioned_wav_and_reuses_cache(tmp_path, monkeypatch):
    from webapp.services import natural_tts

    calls = []

    def fake_edge(text, mp3_path, *, voice, rate, pitch):
        calls.append((text, voice, rate, pitch))
        mp3_path.write_bytes(b"neural-mp3")

    def fake_transcode(mp3_path, wav_path):
        assert mp3_path.read_bytes() == b"neural-mp3"
        wav_path.write_bytes(b"RIFF-natural-wav")

    monkeypatch.setattr(natural_tts, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(natural_tts, "_synthesize_edge_mp3", fake_edge)
    monkeypatch.setattr(natural_tts, "_transcode_to_wav", fake_transcode)
    first = tmp_path / "one.wav"
    second = tmp_path / "two.wav"

    natural_tts.synthesize_natural_speech("A natural sentence.", first)
    natural_tts.synthesize_natural_speech("A natural sentence.", second)

    assert first.read_bytes() == b"RIFF-natural-wav"
    assert second.read_bytes() == b"RIFF-natural-wav"
    assert len(calls) == 1
    assert natural_tts.is_current_tts_audio(first, "A natural sentence.")
    assert natural_tts.is_current_tts_audio(second, "A natural sentence.")


def test_old_unversioned_tts_is_not_treated_as_current(tmp_path):
    from webapp.services.natural_tts import is_current_tts_audio

    old = tmp_path / "old.wav"
    old.write_bytes(b"RIFF-old-sapi")

    assert not is_current_tts_audio(old, "Old sentence.")


def test_natural_tts_preview_route_returns_neural_audio(tmp_path, monkeypatch):
    from fastapi_server import create_app
    from webapp.fastapi_routes import misc

    calls = []

    def fake_synthesize(text, output_path):
        calls.append(text)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"RIFF-neural-preview")

    monkeypatch.setattr(misc, "NATURAL_TTS_PREVIEW_DIR", tmp_path / "preview")
    monkeypatch.setattr(misc, "synthesize_natural_speech", fake_synthesize)
    client = TestClient(create_app())

    response = client.get(
        "/api/tts/natural",
        params={"text": "This example should sound natural."},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/wav")
    assert response.content == b"RIFF-neural-preview"
    assert calls == ["This example should sound natural."]
    assert client.get("/api/tts/natural", params={"text": " "}).status_code == 400
    assert client.get("/api/tts/natural", params={"text": "x" * 501}).status_code == 400


def test_all_tts_surfaces_use_unified_natural_voice_layer():
    root = Path(__file__).parents[1]
    natural_js = (root / "frontend" / "static" / "natural-tts.js").read_text(encoding="utf-8")
    templates = [
        root / "frontend" / "templates" / name
        for name in ("index.html", "intensive.html", "vocab.html", "lesson.html", "workspace.html")
    ]
    for template in templates:
        html = template.read_text(encoding="utf-8")
        assert '/static/natural-tts.js' in html
        assert "new SpeechSynthesisUtterance" not in html
        assert "NaturalTTS.speak" in html
    assert "online" in natural_js.lower()
    assert "natural" in natural_js.lower()
    assert "speechSynthesis.getVoices()" in natural_js
    assert "/api/tts/natural" in natural_js
    assert "speakNeural" in natural_js
    assert "new global.Audio" in natural_js


def test_server_no_longer_uses_windows_sapi_and_pins_neural_dependency():
    root = Path(__file__).parents[1]
    review_export = (root / "backend" / "webapp" / "services" / "v2_review_export.py").read_text(encoding="utf-8")
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")

    assert "System.Speech" not in review_export
    assert "SpeechSynthesizer" not in review_export
    assert "edge-tts==7.2.8" in requirements
