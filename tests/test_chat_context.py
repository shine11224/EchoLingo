import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


def test_selected_range_takes_priority_over_current_anchor(tmp_path, monkeypatch):
    import db
    from webapp.services.chat_context import build_chat_context

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    lesson = db.create_v2_lesson("youtube", "https://youtu.be/abc123def45", "abc123def45", "Demo", 0)
    db.replace_v2_subtitle_segments(lesson["id"], [
        {"index": 1, "start": 0, "end": 10, "text": "First idea."},
        {"index": 2, "start": 10, "end": 20, "text": "Second idea."},
        {"index": 3, "start": 20, "end": 30, "text": "Third idea."},
    ])

    context = build_chat_context(
        lesson_id=lesson["id"],
        message="Explain this range",
        timestamp_seconds=3,
        context_mode="selected_range",
        selected_start_seconds=10,
        selected_end_seconds=30,
        selected_segment_ids=[2, 3],
    )

    assert "Second idea." in context["prompt"]
    assert "Third idea." in context["prompt"]
    assert "First idea." not in context["selected_context"]


def test_summary_pending_is_explicit(tmp_path, monkeypatch):
    import db
    from webapp.services.chat_context import build_chat_context

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    lesson = db.create_v2_lesson("youtube", "https://youtu.be/abc123def45", "abc123def45", "Demo", 0)
    db.replace_v2_subtitle_segments(lesson["id"], [
        {"index": 1, "start": 0, "end": 10, "text": "First idea."},
    ])

    context = build_chat_context(
        lesson_id=lesson["id"],
        message="What is happening?",
        timestamp_seconds=5,
        context_mode="auto",
    )

    assert "summary is still pending" in context["prompt"]
    assert "First idea." in context["prompt"]
