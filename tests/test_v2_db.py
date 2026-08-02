import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


def test_v2_lesson_lifecycle(tmp_path, monkeypatch):
    import db

    test_db = tmp_path / "vocab.db"
    monkeypatch.setattr(db, "DB_PATH", test_db)
    db.init_db()

    lesson = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=abc123def45",
        video_id="abc123def45",
        title="Test Video",
        duration=0,
        media_url="/output/v2_assets/1/sample.mp3",
        media_kind="local_audio",
    )

    assert lesson["id"] > 0
    assert lesson["subtitle_status"] == "pending"
    assert lesson["summary_status"] == "pending"
    assert lesson["media_url"] == "/output/v2_assets/1/sample.mp3"
    assert lesson["media_kind"] == "local_audio"

    db.update_v2_lesson_metadata(lesson["id"], media_url="/output/v2_assets/1/updated.mp4", media_kind="local_video")
    updated_lesson = db.get_v2_lesson(lesson["id"])
    assert updated_lesson["media_url"] == "/output/v2_assets/1/updated.mp4"
    assert updated_lesson["media_kind"] == "local_video"

    db.replace_v2_subtitle_segments(lesson["id"], [
        {"index": 1, "start": 0.0, "end": 2.0, "text": "Hello world."},
        {"index": 2, "start": 2.0, "end": 4.0, "text": "This is useful."},
    ])
    db.set_v2_lesson_status(lesson["id"], subtitle_status="ready")

    segments = db.get_v2_subtitle_segments(lesson["id"])
    assert [s["text"] for s in segments] == ["Hello world.", "This is useful."]
    assert db.get_v2_lesson(lesson["id"])["subtitle_status"] == "ready"

    db.upsert_v2_lesson_progress(lesson["id"], 3.5, 2)
    progress = db.get_v2_lesson_progress(lesson["id"])
    assert progress["last_position_seconds"] == 3.5
    assert progress["last_segment_index"] == 2

    db.save_v2_chat_message(
        lesson_id=lesson["id"],
        timestamp_seconds=3.5,
        selected_start_seconds=0.0,
        selected_end_seconds=4.0,
        selected_segment_ids=[1, 2],
        user_message="Explain this part",
        ai_response="It introduces the topic.",
        context_mode="selected_range",
    )
    history = db.get_v2_chat_history(lesson["id"])
    assert history[0]["context_mode"] == "selected_range"
    assert history[0]["selected_segment_ids"] == [1, 2]

    db.save_v2_phase_b_sentence(
        lesson_id=lesson["id"],
        segment_index=2,
        start_seconds=2.0,
        end_seconds=4.0,
        text="This is useful.",
    )
    saved = db.get_v2_phase_b_sentences(lesson["id"])
    assert saved[0]["text"] == "This is useful."
    assert saved[0]["sentence_id"] > 0

    tags = db.replace_v2_sentence_tags(
        saved[0]["sentence_id"],
        [
            {"category": "pronunciation", "name": "连读"},
            {"category": "practice", "name": "跟读"},
            {"category": "structure", "name": "可复用句式"},
        ],
    )
    assert {tag["name"] for tag in tags} == {"连读", "跟读", "可复用句式"}
    saved_with_tags = db.get_v2_phase_b_sentences(lesson["id"])
    assert {tag["category"] for tag in saved_with_tags[0]["tags"]} == {"pronunciation", "practice", "structure"}

    db.upsert_word("complex", "2026-07-07", level="v2", analysis={"basic_meaning": "复杂的"})
    db.save_v2_lesson_word(lesson["id"], "complex", "A complex system.")
    lesson_words = db.get_v2_lesson_words(lesson["id"])
    assert lesson_words[0]["word"] == "complex"
    assert lesson_words[0]["sentence"] == "A complex system."
    assert lesson_words[0]["cached_analysis"]["basic_meaning"] == "复杂的"
    assert db.get_v2_lesson_word(lesson["id"], "complex")["word"] == "complex"
    assert db.delete_v2_lesson_word(lesson["id"], "complex") is True
    assert db.get_v2_lesson_words(lesson["id"]) == []


def test_v2_lesson_translation_state_round_trip(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    lesson = db.create_v2_lesson(
        source_type="local_audio",
        source_url="C:/media/a.mp3",
    )

    db.configure_v2_lesson_translation(lesson["id"], requested=True)
    db.update_v2_translation_status(
        lesson["id"],
        status="translating",
        done=2,
        total=10,
        buffer_seconds=18.5,
        rate=3.2,
        ready=True,
    )

    saved = db.get_v2_lesson(lesson["id"])
    assert saved["translation_requested"] == 1
    assert saved["translation_status"] == "translating"
    assert saved["translation_done"] == 2
    assert saved["translation_total"] == 10
    assert saved["translation_buffer_seconds"] == 18.5
    assert saved["translation_rate"] == 3.2
    assert saved["translation_ready"] == 1


def test_v2_reading_lesson_stores_blocks(tmp_path, monkeypatch):
    import db

    test_db = tmp_path / "vocab.db"
    monkeypatch.setattr(db, "DB_PATH", test_db)
    db.init_db()

    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual",
        video_id="",
        title="Reading Passage 1",
        duration=0,
        lesson_mode="reading",
    )
    db.replace_v2_reading_blocks(
        lesson["id"],
        [
            {"index": 1, "text": "A first paragraph."},
            {
                "index": 2,
                "text": "A second paragraph.",
                "start_seconds": 12.5,
                "end_seconds": 16.75,
                "source_segment_ids": [7],
                "sentences": [{
                    "segment_index": 7,
                    "text": "A second paragraph.",
                    "start_seconds": 12.5,
                    "end_seconds": 16.75,
                }],
            },
        ],
    )

    saved = db.get_v2_reading_blocks(lesson["id"])
    assert db.get_v2_lesson(lesson["id"])["lesson_mode"] == "reading"
    assert [b["text"] for b in saved] == ["A first paragraph.", "A second paragraph."]
    assert saved[0]["start_seconds"] is None
    assert saved[0]["end_seconds"] is None
    assert saved[0]["source_segment_ids"] == []
    assert saved[0]["sentences"] == []
    assert saved[1]["start_seconds"] == 12.5
    assert saved[1]["end_seconds"] == 16.75
    assert saved[1]["source_segment_ids"] == [7]
    assert saved[1]["sentences"] == [{
        "segment_index": 7,
        "text": "A second paragraph.",
        "start_seconds": 12.5,
        "end_seconds": 16.75,
    }]
