import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from fastapi.testclient import TestClient


def test_v2_review_export_writes_html_with_sentence_audio(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    import webapp.services.v2_review_export as review_export

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(review_export, "OUTPUT_DIR", tmp_path / "output")

    def fake_tts(text, output_path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake wav")

    monkeypatch.setattr(review_export, "synthesize_sentence_audio", fake_tts)

    app = create_app()
    client = TestClient(app)
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:export",
        title="Reading Export",
        lesson_mode="reading",
    )
    db.upsert_word("urban", "2026-07-07", level="v2", analysis={"basic_meaning": "城市的"})
    db.add_context("urban", "Reading Export", "Urban parks reduce stress.")
    db.save_v2_lesson_word(lesson["id"], "urban", "Urban parks reduce stress.")
    saved = db.save_v2_phase_b_sentence(
        lesson_id=lesson["id"],
        segment_index=1,
        start_seconds=0,
        end_seconds=0,
        text="Urban parks reduce stress.",
    )
    db.replace_v2_sentence_tags(saved["sentence_id"], [{"category": "practice", "name": "跟读"}])

    resp = client.get(f"/api/v2/lessons/{lesson['id']}/review-export")

    assert resp.status_code == 200
    data = resp.json()
    assert data["export_url"].endswith("/review.html")
    html_path = tmp_path / "output" / "v2_exports" / str(lesson["id"]) / "review.html"
    audio_path = tmp_path / "output" / "v2_exports" / str(lesson["id"]) / "audio" / "sentence-1.wav"
    assert html_path.exists()
    assert audio_path.exists()
    html = html_path.read_text(encoding="utf-8")
    assert "Reading Export" in html
    assert "urban" in html
    assert "城市的" in html
    assert "Urban parks reduce stress." in html
    assert "跟读" in html
    assert "audio/sentence-1.wav" in html


def test_v2_review_export_does_not_include_global_words_without_lesson_relation(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    import webapp.services.v2_review_export as review_export

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(review_export, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(review_export, "synthesize_sentence_audio", lambda text, output_path: output_path.write_bytes(b"fake wav"))

    app = create_app()
    client = TestClient(app)
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:no-context",
        title="No Context Lesson",
        lesson_mode="reading",
    )
    db.upsert_word("globalword", "2026-07-07", level="v2", analysis={"basic_meaning": "全局词"})
    db.save_v2_phase_b_sentence(
        lesson_id=lesson["id"],
        segment_index=1,
        start_seconds=0,
        end_seconds=0,
        text="A saved sentence.",
    )

    resp = client.get(f"/api/v2/lessons/{lesson['id']}/review-export")

    assert resp.status_code == 200
    assert resp.json()["word_count"] == 0
    html = (tmp_path / "output" / "v2_exports" / str(lesson["id"]) / "review.html").read_text(encoding="utf-8")
    assert "globalword" not in html
