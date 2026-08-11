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


def test_reading_selection_routes_through_rag_with_real_sentence_anchor(tmp_path, monkeypatch):
    """Task 10 返修 Blocker 1：reading_selection 必须走 Task 9 RAG，
    返回 coverage + 服务器真实 sentence_key 锚点；analyze 的 message.ai_response 契约不变。"""
    import db
    from fastapi_server import create_app
    from webapp.runtime import ai_config

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    lesson = db.create_v2_lesson(
        "reading", "upload://sel.txt", title="Reading selection lesson",
        lesson_mode="reading")
    blocks = []
    key = 30001
    for b in range(12):
        sentences = []
        for s in range(10):
            sentences.append({
                "sentence_key": key,
                "text": f"block{b} sentence{s} " + " ".join(f"tok{j}" for j in range(8)),
            })
            key += 1
        blocks.append({"index": b,
                       "text": " ".join(x["text"] for x in sentences),
                       "sentences": sentences})
    db.replace_v2_reading_blocks(lesson["id"], blocks)
    session = db.create_v2_chat_session(lesson["id"])
    selected = blocks[6]["sentences"][5]["text"]  # sentence_key=30066
    completions = _ScriptedCompletions(
        '{"answer": "选区解析结果。", "coverage": "partial",'
        ' "citations": [{"candidate_id": "c004", "sentence_key": 999999}],'
        ' "unsupported": ["选区没有解释作者动机"]}'
    )
    _patch_rag_ai(monkeypatch, ai_config, completions)
    client = TestClient(create_app())

    response = client.post(
        "/api/v2/chat",
        json={
            "lesson_id": lesson["id"],
            "session_id": session["id"],
            "message": "解析这段",
            "context_mode": "reading_selection",
            "selected_text": f"  {selected}\n ",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    message = payload["message"]
    # 选区 analyze 的阅读契约：仍返回 saved message 的 ai_response
    assert message["ai_response"] == "选区解析结果。"
    assert message["coverage_status"] == "partial"
    anchor = message["citations"][0]
    assert anchor["anchor_type"] == "sentence"
    # 服务器解析选区 → 真实 sentence_key；模型伪造的 999999 不出现
    assert anchor["sentence_key"] == 30066
    assert anchor["block_index"] == 6
    # unsupported 持久化在 message 上并随历史恢复
    assert message["unsupported"] == ["选区没有解释作者动机"]
    history = db.get_v2_chat_history(lesson["id"], session_id=session["id"])
    assert history[0]["coverage_status"] == "partial"
    assert history[0]["citations"][0]["sentence_key"] == 30066
    assert history[0]["unsupported"] == ["选区没有解释作者动机"]


def test_chat_message_unsupported_is_bounded_and_roundtrips(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    lesson = _create_reading_lesson(db)
    session = db.create_v2_chat_session(lesson["id"])
    db.save_v2_chat_message(
        lesson_id=lesson["id"], session_id=session["id"], timestamp_seconds=0,
        selected_start_seconds=None, selected_end_seconds=None,
        selected_segment_ids=[], user_message="q", ai_response="a",
        coverage_status="partial",
        unsupported=[f"缺口{i}" for i in range(8)],
    )

    history = db.get_v2_chat_history(lesson["id"], session_id=session["id"])
    assert history[0]["unsupported"] == [f"缺口{i}" for i in range(5)]  # 上限 5
    default = db.save_v2_chat_message(
        lesson_id=lesson["id"], session_id=session["id"], timestamp_seconds=0,
        selected_start_seconds=None, selected_end_seconds=None,
        selected_segment_ids=[], user_message="q2", ai_response="a2",
    )
    assert default["unsupported"] == []


# ---------- Task 9：单课 RAG 问答 ----------

def _create_media_lesson_with_subtitles(db, n=30):
    db.init_db()
    lesson = db.create_v2_lesson("youtube", "yt://rag-sample", title="RAG sample")
    db.replace_v2_subtitle_segments(
        lesson["id"],
        [
            {"index": i + 1, "start": i * 2.0, "end": i * 2.0 + 1.5,
             "text": f"segment {i + 1} " + " ".join(f"token{j}" for j in range(8))}
            for i in range(n)
        ],
    )
    return lesson


class _ScriptedCompletions:
    """按 system prompt 是否为 RAG 契约分发脚本化响应，记录所有调用。"""

    def __init__(self, answer_payload):
        self.answer_payload = answer_payload
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        user = kwargs["messages"][1]["content"]
        if "独立复核" in user:
            content = '{"found": false, "candidate_ids": []}'
        elif "章节列表" in user:
            content = '{"chapters": [0]}'
        elif "candidate id" in kwargs["messages"][0]["content"]:
            content = (
                self.answer_payload.pop(0)
                if isinstance(self.answer_payload, list)
                else self.answer_payload
            )
        else:
            content = '{"chapters": [0]}'
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
        )


def _patch_rag_ai(monkeypatch, ai_config, completions):
    monkeypatch.setattr(ai_config, "AI_API_KEY", "test-key")
    monkeypatch.setattr(
        ai_config, "client",
        SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )


def test_auto_chat_returns_validated_coverage_and_real_anchors(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.runtime import ai_config

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    completions = _ScriptedCompletions(
        '{"answer": "开场讲了 token 序列。", "coverage": "full",'
        ' "citations": [{"candidate_id": "c001", "start_seconds": 999.9},'
        ' {"candidate_id": "c999"}], "unsupported": []}'
    )
    _patch_rag_ai(monkeypatch, ai_config, completions)
    lesson = _create_media_lesson_with_subtitles(db)
    session = db.create_v2_chat_session(lesson["id"])
    client = TestClient(create_app())

    response = client.post(
        "/api/v2/chat",
        json={"lesson_id": lesson["id"], "session_id": session["id"],
              "message": "开场讲了什么？", "timestamp_seconds": 3.0},
    )

    assert response.status_code == 200
    message = response.json()["message"]
    assert message["coverage_status"] == "full"
    assert message["external_knowledge_used"] is False
    assert len(message["citations"]) == 1
    anchor = message["citations"][0]
    assert anchor["anchor_type"] == "time"
    assert anchor["segment_index"] == 1
    assert anchor["start_seconds"] == 0.0  # 模型伪造的 999.9 被丢弃
    # RAG system prompt 强制只用当前课程内容
    rag_call = [c for c in completions.calls if "candidate id" in c["messages"][0]["content"]]
    assert rag_call
    assert "不得使用外部知识" in rag_call[0]["messages"][0]["content"]
    history = db.get_v2_chat_history(lesson["id"], session_id=session["id"])
    assert history[0]["coverage_status"] == "full"
    assert history[0]["citations"][0]["segment_index"] == 1


def test_auto_chat_full_without_valid_citation_is_not_full(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.runtime import ai_config

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    _patch_rag_ai(monkeypatch, ai_config, _ScriptedCompletions(
        '{"answer": "部分回答。", "coverage": "full",'
        ' "citations": [{"candidate_id": "c404"}]}'
    ))
    lesson = _create_media_lesson_with_subtitles(db)
    session = db.create_v2_chat_session(lesson["id"])
    client = TestClient(create_app())

    response = client.post(
        "/api/v2/chat",
        json={"lesson_id": lesson["id"], "session_id": session["id"], "message": "q"},
    )

    assert response.status_code == 200
    message = response.json()["message"]
    assert message["coverage_status"] == "partial"
    assert message["citations"] == []


def test_auto_chat_none_verified_by_second_absence_call(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.runtime import ai_config

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    completions = _ScriptedCompletions([
        '{"answer": "", "coverage": "none", "citations": []}',
    ])
    _patch_rag_ai(monkeypatch, ai_config, completions)
    lesson = _create_media_lesson_with_subtitles(db, n=160)
    session = db.create_v2_chat_session(lesson["id"])
    client = TestClient(create_app())

    response = client.post(
        "/api/v2/chat",
        json={"lesson_id": lesson["id"], "session_id": session["id"],
              "message": "视频里有没有讲量子力学？"},
    )

    assert response.status_code == 200
    message = response.json()["message"]
    assert message["coverage_status"] == "none"
    assert message["citations"] == []
    kinds = [
        "absence" if "独立复核" in c["messages"][1]["content"] else "answer"
        for c in completions.calls if "candidate id" in c["messages"][0]["content"]
    ]
    assert "absence" in kinds  # none 必须经过第二次独立复核


def test_auto_chat_invalid_ai_output_releases_operation_with_502(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.runtime import ai_config

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    _patch_rag_ai(monkeypatch, ai_config, _ScriptedCompletions("not json at all"))
    lesson = _create_media_lesson_with_subtitles(db)
    session = db.create_v2_chat_session(lesson["id"])
    client = TestClient(create_app())

    response = client.post(
        "/api/v2/chat",
        json={"lesson_id": lesson["id"], "session_id": session["id"], "message": "q"},
    )

    assert response.status_code == 502
    assert db.get_v2_chat_history(lesson["id"], session_id=session["id"]) == []


def test_chat_sessions_history_isolated_per_session_with_citations(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    lesson = _create_reading_lesson(db)
    first = db.create_v2_chat_session(lesson["id"], title="A")
    second = db.create_v2_chat_session(lesson["id"], title="B")
    citation = {"anchor_type": "block", "block_index": 2, "chapter_title": "章",
                "excerpt": "ex"}
    db.save_v2_chat_message(
        lesson_id=lesson["id"], session_id=first["id"], timestamp_seconds=0,
        selected_start_seconds=None, selected_end_seconds=None,
        selected_segment_ids=[], user_message="q1", ai_response="a1",
        coverage_status="full", citations=[citation],
    )
    db.save_v2_chat_message(
        lesson_id=lesson["id"], session_id=second["id"], timestamp_seconds=0,
        selected_start_seconds=None, selected_end_seconds=None,
        selected_segment_ids=[], user_message="q2", ai_response="a2",
    )
    client = TestClient(create_app())

    history = client.get(
        f"/api/v2/chat/history/{lesson['id']}?session_id={second['id']}"
    ).json()["messages"]
    assert [m["user_message"] for m in history] == ["q2"]
    assert history[0]["citations"] == []
    first_history = client.get(
        f"/api/v2/chat/history/{lesson['id']}?session_id={first['id']}"
    ).json()["messages"]
    assert first_history[0]["citations"] == [citation]
    assert first_history[0]["coverage_status"] == "full"


def test_uploaded_media_chat_citations_use_authoritative_time_anchors(tmp_path, monkeypatch):
    """Blocker 1：普通用户唯一媒体来源 uploaded_media 必须走字幕时间锚点，不误判 none。"""
    import db
    from fastapi_server import create_app
    from webapp.runtime import ai_config

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    lesson = db.create_v2_lesson("uploaded_media", "upload://user-video", title="User video")
    segments = [
        {"index": i + 1, "start": i * 2.5, "end": i * 2.5 + 2.0,
         "text": f"real subtitle {i + 1} " + " ".join(f"word{j}" for j in range(8))}
        for i in range(30)
    ]
    db.replace_v2_subtitle_segments(lesson["id"], segments)
    session = db.create_v2_chat_session(lesson["id"])
    _patch_rag_ai(monkeypatch, ai_config, _ScriptedCompletions(
        '{"answer": "第二段解释了该概念。", "coverage": "full",'
        ' "citations": [{"candidate_id": "c001", "start_seconds": 12345.0}]}'
    ))
    client = TestClient(create_app())

    response = client.post(
        "/api/v2/chat",
        json={"lesson_id": lesson["id"], "session_id": session["id"],
              "message": "哪里解释了这个概念？"},
    )

    assert response.status_code == 200
    message = response.json()["message"]
    assert message["coverage_status"] == "full"
    anchor = message["citations"][0]
    assert anchor["anchor_type"] == "time"
    assert anchor["segment_index"] == segments[0]["index"]
    assert anchor["start_seconds"] == segments[0]["start"]  # 非模型伪造的 12345
    assert anchor["end_seconds"] > segments[0]["start"]
    assert "real subtitle 1" in anchor["excerpt"]


def test_absence_output_finally_invalid_returns_502(tmp_path, monkeypatch):
    """Blocker 3：none 复核输出修复一次后仍非法 → 502，不留假 none。"""
    import db
    from fastapi_server import create_app
    from webapp.runtime import ai_config

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")

    class BrokenAbsence:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            user = kwargs["messages"][1]["content"]
            if "独立复核" in user:
                content = "坏输出"
            elif "章节列表" in user:
                content = '{"chapters": [0]}'
            else:
                content = '{"answer": "", "coverage": "none", "citations": []}'
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    _patch_rag_ai(monkeypatch, ai_config, BrokenAbsence())
    lesson = _create_media_lesson_with_subtitles(db, n=160)
    session = db.create_v2_chat_session(lesson["id"])
    client = TestClient(create_app())

    response = client.post(
        "/api/v2/chat",
        json={"lesson_id": lesson["id"], "session_id": session["id"], "message": "没有的内容"},
    )

    assert response.status_code == 502
    assert db.get_v2_chat_history(lesson["id"], session_id=session["id"]) == []
