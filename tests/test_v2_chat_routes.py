import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from fastapi.testclient import TestClient


def _create_reading_lesson(db):
    db.init_db()
    return db.create_v2_lesson(
        "reading",
        "upload://sample.txt",
        title="Reading sample",
        lesson_mode="reading",
    )


def test_chat_sessions_can_be_created_and_listed(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    lesson = _create_reading_lesson(db)
    client = TestClient(create_app())

    created = client.post(
        f"/api/v2/chat/sessions/{lesson['id']}",
        json={"title": "Grammar notes"},
    )

    assert created.status_code == 200
    assert created.json()["session"]["title"] == "Grammar notes"
    listed = client.get(f"/api/v2/chat/sessions/{lesson['id']}")
    assert listed.status_code == 200
    assert listed.json()["sessions"] == [created.json()["session"]]


def test_chat_session_can_be_created_without_request_body(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    lesson = _create_reading_lesson(db)
    client = TestClient(create_app())

    created = client.post(f"/api/v2/chat/sessions/{lesson['id']}")

    assert created.status_code == 200
    assert created.json()["session"]["title"] == ""


def test_chat_history_is_scoped_to_session_and_rejects_cross_lesson_session(
    tmp_path, monkeypatch
):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    lesson = _create_reading_lesson(db)
    other_lesson = db.create_v2_lesson("reading", "upload://other.txt", title="Other")
    first = db.create_v2_chat_session(lesson["id"], title="First")
    second = db.create_v2_chat_session(lesson["id"], title="Second")
    other = db.create_v2_chat_session(other_lesson["id"], title="Other")
    for session, text in ((first, "first"), (second, "second")):
        db.save_v2_chat_message(
            lesson_id=lesson["id"],
            session_id=session["id"],
            timestamp_seconds=0,
            selected_start_seconds=None,
            selected_end_seconds=None,
            selected_segment_ids=[],
            user_message=text,
            ai_response=f"{text} answer",
        )
    client = TestClient(create_app())

    response = client.get(
        f"/api/v2/chat/history/{lesson['id']}?session_id={second['id']}"
    )

    assert response.status_code == 200
    assert [message["user_message"] for message in response.json()["messages"]] == [
        "second"
    ]
    cross_lesson = client.get(
        f"/api/v2/chat/history/{lesson['id']}?session_id={other['id']}"
    )
    assert cross_lesson.status_code == 404


def test_chat_session_export_returns_markdown_attachment(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    lesson = _create_reading_lesson(db)
    session = db.create_v2_chat_session(lesson["id"], title="Meaning discussion")
    db.save_v2_chat_message(
        lesson_id=lesson["id"],
        session_id=session["id"],
        timestamp_seconds=0,
        selected_start_seconds=None,
        selected_end_seconds=None,
        selected_segment_ids=[],
        user_message="What does this mean?",
        ai_response="It describes a change.",
    )
    client = TestClient(create_app())

    response = client.get(
        f"/api/v2/chat/sessions/{lesson['id']}/{session['id']}/export"
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "attachment;" in response.headers["content-disposition"]
    assert ".md" in response.headers["content-disposition"]
    assert "# Reading sample" in response.text
    assert "Meaning discussion" in response.text
    assert "## You" in response.text
    assert "What does this mean?" in response.text
    assert "## AI" in response.text
    assert "It describes a change." in response.text


def test_reading_selection_rejects_empty_text_with_400(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.runtime import ai_config

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(ai_config, "AI_API_KEY", "")
    lesson = _create_reading_lesson(db)
    session = db.create_v2_chat_session(lesson["id"])
    client = TestClient(create_app())

    response = client.post(
        "/api/v2/chat",
        json={
            "lesson_id": lesson["id"],
            "session_id": session["id"],
            "message": "Explain this.",
            "context_mode": "reading_selection",
            "selected_text": " \n\t ",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Reading selection cannot be empty"


def test_reading_selection_rejects_more_than_4000_characters(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.runtime import ai_config

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(ai_config, "AI_API_KEY", "")
    lesson = _create_reading_lesson(db)
    session = db.create_v2_chat_session(lesson["id"])
    client = TestClient(create_app())

    response = client.post(
        "/api/v2/chat",
        json={
            "lesson_id": lesson["id"],
            "session_id": session["id"],
            "message": "Explain this.",
            "context_mode": "reading_selection",
            "selected_text": "x" * 4001,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Reading selection cannot exceed 4000 characters"


def test_reading_selection_is_sent_to_ai_with_lesson_neutral_system_prompt(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.runtime import ai_config

    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="解析结果"))]
            )

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(ai_config, "AI_API_KEY", "test-key")
    monkeypatch.setattr(
        ai_config,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())),
    )
    lesson = _create_reading_lesson(db)
    session = db.create_v2_chat_session(lesson["id"])
    client = TestClient(create_app())

    response = client.post(
        "/api/v2/chat",
        json={
            "lesson_id": lesson["id"],
            "session_id": session["id"],
            "message": "Explain this.",
            "context_mode": "reading_selection",
            "selected_text": "  A selected\npassage.  ",
        },
    )

    assert response.status_code == 200
    assert response.json()["context"]["selected_context"] == "A selected passage."
    assert "[Reading selection]\nA selected passage." in captured["messages"][1]["content"]
    assert "video or reading lesson content" in captured["messages"][0]["content"]
    assert captured["timeout"] == 45
    history = db.get_v2_chat_history(lesson["id"], session_id=session["id"])
    assert [message["user_message"] for message in history] == ["Explain this."]
