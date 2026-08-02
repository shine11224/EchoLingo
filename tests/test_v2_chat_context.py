import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


def test_reading_selection_is_normalized_and_keeps_recent_history(tmp_path, monkeypatch):
    import db
    from webapp.services.chat_context import build_chat_context

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    lesson = db.create_v2_lesson(
        "reading",
        "upload://sample.txt",
        title="Reading sample",
        lesson_mode="reading",
    )
    session = db.create_v2_chat_session(lesson["id"], title="Grammar questions")
    db.save_v2_chat_message(
        lesson_id=lesson["id"],
        session_id=session["id"],
        timestamp_seconds=0,
        selected_start_seconds=None,
        selected_end_seconds=None,
        selected_segment_ids=[],
        user_message="What does it mean?",
        ai_response="It describes a change.",
        context_mode="reading_selection",
    )

    context = build_chat_context(
        lesson_id=lesson["id"],
        session_id=session["id"],
        message="Explain the grammar.",
        context_mode="reading_selection",
        selected_text="  This\n  is\tselected.  ",
    )

    assert context["selected_context"] == "This is selected."
    assert "[Lesson: Reading sample]" in context["prompt"]
    assert "[Video:" not in context["prompt"]
    assert "[Reading selection]\nThis is selected." in context["prompt"]
    assert "[Subtitle context]" not in context["prompt"]
    assert "User: What does it mean?" in context["prompt"]
    assert "AI: It describes a change." in context["prompt"]
    assert context["prompt"].index("[Recent chat history]") < context["prompt"].index("[Reading selection]")
    assert context["prompt"].index("[Reading selection]") < context["prompt"].index("[User message]")


def test_chat_context_uses_only_the_active_session_history(tmp_path, monkeypatch):
    import db
    from webapp.services.chat_context import build_chat_context

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    lesson = db.create_v2_lesson("reading", "upload://isolated.txt", title="Isolation")
    first = db.create_v2_chat_session(lesson["id"], title="First")
    second = db.create_v2_chat_session(lesson["id"], title="Second")

    for session, question, answer in (
        (first, "First question", "First answer"),
        (second, "Second question", "Second answer"),
    ):
        db.save_v2_chat_message(
            lesson_id=lesson["id"],
            session_id=session["id"],
            timestamp_seconds=0,
            selected_start_seconds=None,
            selected_end_seconds=None,
            selected_segment_ids=[],
            user_message=question,
            ai_response=answer,
        )

    context = build_chat_context(
        lesson_id=lesson["id"],
        session_id=second["id"],
        message="Continue",
    )

    assert "Second question" in context["prompt"]
    assert "Second answer" in context["prompt"]
    assert "First question" not in context["prompt"]
    assert "First answer" not in context["prompt"]


def test_init_db_backfills_legacy_chat_messages_into_one_session(tmp_path, monkeypatch):
    import db

    database = tmp_path / "vocab.db"
    monkeypatch.setattr(db, "DB_PATH", database)
    db.init_db()
    lesson = db.create_v2_lesson("reading", "upload://legacy.txt", title="Legacy")
    with sqlite3.connect(database) as conn:
        conn.execute("DROP TABLE v2_chat_messages")
        conn.execute("""
            CREATE TABLE v2_chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lesson_id INTEGER NOT NULL,
                timestamp_seconds REAL NOT NULL DEFAULT 0,
                selected_start_seconds REAL,
                selected_end_seconds REAL,
                selected_segment_ids TEXT NOT NULL DEFAULT '[]',
                user_message TEXT NOT NULL DEFAULT '',
                ai_response TEXT NOT NULL DEFAULT '',
                context_mode TEXT NOT NULL DEFAULT 'auto',
                created_at TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute(
            "INSERT INTO v2_chat_messages"
            " (lesson_id, user_message, ai_response, created_at) VALUES (?, ?, ?, ?)",
            (lesson["id"], "Old question", "Old answer", "2026-07-01T10:00:00"),
        )

    db.init_db()
    db.init_db()

    sessions = db.list_v2_chat_sessions(lesson["id"])
    assert len(sessions) == 1
    assert sessions[0]["title"] == "Legacy conversation"
    history = db.get_v2_chat_history(lesson["id"], session_id=sessions[0]["id"])
    assert [message["user_message"] for message in history] == ["Old question"]
