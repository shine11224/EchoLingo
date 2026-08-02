import json
import os
import sys
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from fastapi.testclient import TestClient


def _wait_for_outline(client: TestClient, lesson_id: int, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v2/lessons/{lesson_id}/outline-summary")
        data = response.json()
        if data.get("status") == "error":
            raise AssertionError(f"outline generation failed: {data.get('error')}")
        if data.get("status") != "pending":
            return response
        time.sleep(0.01)
    raise AssertionError("outline generation did not finish")


def _mock_ai_client(create, option_calls: list[dict] | None = None):
    def with_options(**kwargs):
        if option_calls is not None:
            option_calls.append(kwargs)
        return SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

    return SimpleNamespace(with_options=with_options)


def test_document_outline_prompt_requests_fine_single_topic_chapters():
    from prompts import DOCUMENT_OUTLINE_PROMPT

    assert "每个章节只包含一个明确主题" in DOCUMENT_OUTLINE_PROMPT
    assert "不要把同一主题拆成多个短章" in DOCUMENT_OUTLINE_PROMPT
    assert "5 分钟以内" in DOCUMENT_OUTLINE_PROMPT
    assert "5–10 分钟" in DOCUMENT_OUTLINE_PROMPT
    assert "30–90 分钟" in DOCUMENT_OUTLINE_PROMPT
    assert "90 分钟以上" in DOCUMENT_OUTLINE_PROMPT
    assert "例子、解释和结论默认归入所属主题" in DOCUMENT_OUTLINE_PROMPT
    assert "anchor_id 必须严格递增且不得重复" in DOCUMENT_OUTLINE_PROMPT
    assert "先概括视频主要内容" in DOCUMENT_OUTLINE_PROMPT
    assert "共分为多少个章节" in DOCUMENT_OUTLINE_PROMPT
    assert "与 sections 中的章节标题保持一致" in DOCUMENT_OUTLINE_PROMPT


def test_document_outline_is_generated_once_then_served_from_cache(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.runtime import ai_config
    from webapp.services import document_outline

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:outline-test",
        title="Outline Test",
        lesson_mode="reading",
    )
    db.replace_v2_reading_blocks(
        lesson["id"],
        [
            {"index": 0, "text": "This guide introduces the practice test."},
            {"index": 1, "text": "It then explains how candidates should answer."},
        ],
    )
    calls = []
    option_calls = []
    started = threading.Event()
    release = threading.Event()

    def create(**kwargs):
        calls.append(kwargs)
        started.set()
        assert release.wait(2)
        content = json.dumps(
            {
                "summary": "这是一份考试练习指南。",
                "document_type": "guide",
                "sections": [
                    {
                        "anchor_id": 0,
                        "title": "材料目的",
                        "description": "介绍练习测试及答题方式。",
                    }
                ],
            },
            ensure_ascii=False,
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )

    monkeypatch.setattr(ai_config, "AI_API_KEY", "configured")
    monkeypatch.setattr(
        ai_config,
        "client",
        _mock_ai_client(create, option_calls),
    )

    first_start = client.post(f"/api/v2/lessons/{lesson['id']}/outline-summary")
    assert first_start.status_code == 202
    assert first_start.json()["status"] == "pending"
    assert started.wait(1)
    duplicate = client.post(f"/api/v2/lessons/{lesson['id']}/outline-summary")
    assert duplicate.status_code == 202
    assert duplicate.json()["status"] == "pending"
    assert len(calls) == 1
    assert option_calls[0] == {"timeout": 180, "max_retries": 0}
    release.set()
    first = _wait_for_outline(client, lesson["id"])
    second = client.post(f"/api/v2/lessons/{lesson['id']}/outline-summary")

    assert first.status_code == 200
    assert first.json()["status"] == "ready"
    assert first.json()["cached"] is False
    assert first.json()["outline"]["summary"] == "这是一份考试练习指南。"
    assert first.json()["outline"]["sections"][0]["anchor_type"] == "block"
    assert first.json()["outline"]["sections"][0]["anchor_id"] == 0
    assert second.status_code == 200
    assert second.json()["cached"] is True
    assert second.json()["outline"] == first.json()["outline"]
    assert len(calls) == 1
    assert '"duration_seconds": 0.0' in calls[0]["messages"][0]["content"]

    with document_outline._OUTLINE_JOBS_LOCK:
        document_outline._OUTLINE_JOBS.pop(lesson["id"], None)
    reopened = client.get(f"/api/v2/lessons/{lesson['id']}/outline-summary")

    assert reopened.status_code == 200
    assert reopened.json()["status"] == "ready"
    assert reopened.json()["cached"] is True
    assert reopened.json()["outline"] == first.json()["outline"]
    assert len(calls) == 1

    monkeypatch.setattr(
        document_outline,
        "DOCUMENT_OUTLINE_PROMPT",
        "updated outline prompt\n{document_json}",
    )
    after_prompt_change_start = client.post(
        f"/api/v2/lessons/{lesson['id']}/outline-summary"
    )
    assert after_prompt_change_start.status_code == 202
    after_prompt_change = _wait_for_outline(client, lesson["id"])

    assert after_prompt_change.status_code == 200
    assert after_prompt_change.json()["cached"] is False
    assert len(calls) == 2


def test_document_outline_prompt_represents_the_end_of_a_long_document(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.runtime import ai_config

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:outline-long-test",
        title="Long Outline Test",
        lesson_mode="reading",
    )
    blocks = [
        {"index": index, "text": f"Section {index} supporting detail."}
        for index in range(48)
    ]
    blocks[-1]["text"] = "TAIL-MARKER final conclusion."
    db.replace_v2_reading_blocks(lesson["id"], blocks)
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        content = json.dumps(
            {
                "summary": "完整长文摘要。",
                "document_type": "article",
                "sections": [
                    {
                        "anchor_id": 0,
                        "title": "开篇",
                        "description": "长文开篇。",
                    }
                ],
            },
            ensure_ascii=False,
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )

    monkeypatch.setattr(ai_config, "AI_API_KEY", "configured")
    monkeypatch.setattr(
        ai_config,
        "client",
        _mock_ai_client(create),
    )

    start = client.post(f"/api/v2/lessons/{lesson['id']}/outline-summary")
    assert start.status_code == 202
    response = _wait_for_outline(client, lesson["id"])

    assert response.status_code == 200
    assert len(calls) == 1
    assert "TAIL-MARKER" in calls[0]["messages"][0]["content"]


def test_document_outline_prompt_includes_one_hour_media_duration(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.runtime import ai_config

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="local_video",
        source_url="C:/media/one-hour.mp4",
        title="One Hour Course",
    )
    db.update_v2_lesson_metadata(
        lesson["id"],
        duration=3600.0,
        lesson_mode="reading",
    )
    db.replace_v2_subtitle_segments(
        lesson["id"],
        [
            {"index": 0, "start": 0.0, "end": 10.0, "text": "Opening topic."},
            {"index": 1, "start": 1800.0, "end": 1810.0, "text": "Middle topic."},
            {"index": 2, "start": 3590.0, "end": 3600.0, "text": "Final topic."},
        ],
    )
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        content = json.dumps(
            {
                "summary": "一小时课程摘要。",
                "document_type": "course",
                "sections": [
                    {"anchor_id": 0.0, "title": "课程开场", "description": "介绍课程主题。"},
                    {"anchor_id": 1800.0, "title": "中段主题", "description": "进入课程中段。"},
                    {"anchor_id": 3590.0, "title": "课程总结", "description": "总结课程内容。"},
                ],
            },
            ensure_ascii=False,
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )

    monkeypatch.setattr(ai_config, "AI_API_KEY", "configured")
    monkeypatch.setattr(
        ai_config,
        "client",
        _mock_ai_client(create),
    )

    start = client.post(f"/api/v2/lessons/{lesson['id']}/outline-summary")
    assert start.status_code == 202
    response = _wait_for_outline(client, lesson["id"])

    assert response.status_code == 200
    assert '"duration_seconds": 3600.0' in calls[0]["messages"][0]["content"]
    assert all(
        item["anchor_type"] == "time"
        for item in response.json()["outline"]["sections"]
    )
    assert [item["anchor_id"] for item in response.json()["outline"]["sections"]] == [
        0.0,
        1800.0,
        3590.0,
    ]
