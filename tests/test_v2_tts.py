import os
import sys
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


def test_build_reading_tts_creates_course_audio_and_timed_subtitles(tmp_path, monkeypatch):
    import db
    from webapp.services import v2_intensive, v2_lessons, v2_tts

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(v2_tts, "OUTPUT_DIR", tmp_path / "output")
    db.init_db()
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:tts-course",
        title="TTS Course",
        lesson_mode="reading",
    )
    db.replace_v2_reading_blocks(lesson["id"], [
        {"index": 1, "text": "First sentence. Second sentence!"},
    ])

    def fake_synthesize(_text, output_path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output_path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8_000)
            audio.writeframes(b"\0\0" * 800)

    monkeypatch.setattr(v2_tts, "synthesize_sentence_audio", fake_synthesize)
    translated = []
    monkeypatch.setattr(
        v2_tts,
        "translate_lesson_subtitles",
        lambda lesson_id: translated.append(lesson_id) or {"status": "ready"},
    )

    result = v2_tts.build_reading_tts(lesson["id"])

    assert result["status"] == "ready"
    assert result["sentence_count"] == 2
    assert (tmp_path / "output" / "v2_assets" / str(lesson["id"]) / "reading.wav").exists()
    updated = db.get_v2_lesson(lesson["id"])
    assert updated["media_kind"] == "generated_audio"
    assert updated["media_url"].endswith(f"/{lesson['id']}/reading.wav")
    assert updated["duration"] == 0.2
    assert updated["translation_requested"] == 1
    assert translated == [lesson["id"]]
    assert v2_lessons.get_available_modes(updated) == ["listening", "reading"]
    segments = db.get_v2_subtitle_segments(lesson["id"])
    assert [segment["text"] for segment in segments] == ["First sentence.", "Second sentence!"]
    assert segments[0]["end"] == segments[1]["start"]
    assert segments[-1]["end"] == 0.2
    timed_block = db.get_v2_reading_blocks(lesson["id"])[0]
    assert timed_block["start_seconds"] == 0
    assert timed_block["end_seconds"] == 0.2
    assert [item["text"] for item in timed_block["sentences"]] == [
        "First sentence.",
        "Second sentence!",
    ]
    intensive = v2_intensive.build_intensive_document(lesson["id"])
    assert [item["start_seconds"] for item in intensive["sentences"]] == [0, 0.1]
    assert [item["end_seconds"] for item in intensive["sentences"]] == [0.1, 0.2]


def test_reading_endpoint_backfills_timing_for_existing_generated_audio(tmp_path, monkeypatch):
    import db
    from fastapi.testclient import TestClient
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:legacy-tts",
        title="Legacy TTS",
        lesson_mode="reading",
        media_url="/output/v2_assets/1/reading.wav",
        media_kind="generated_audio",
    )
    db.replace_v2_reading_blocks(
        lesson["id"],
        [{"index": 1, "text": "First sentence. Second sentence!"}],
    )
    db.replace_v2_subtitle_segments(
        lesson["id"],
        [
            {"index": 0, "start": 0.0, "end": 0.1, "text": "First sentence."},
            {"index": 1, "start": 0.1, "end": 0.2, "text": "Second sentence!"},
        ],
    )

    response = client.get(f"/api/v2/lessons/{lesson['id']}/reading")

    assert response.status_code == 200
    block = response.json()["blocks"][0]
    assert block["start_seconds"] == 0
    assert block["end_seconds"] == 0.2
    assert [item["start_seconds"] for item in block["sentences"]] == [0, 0.1]
