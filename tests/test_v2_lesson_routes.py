import sys
import os
import time
import json
import pytest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from fastapi.testclient import TestClient


def _wait_for_upload(client: TestClient, job_id: str, timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v2/lessons/reading/upload-status/{job_id}")
        assert response.status_code == 200
        status = response.json()
        if status["stage"] in {"done", "failed"}:
            return status
        time.sleep(0.01)
    raise AssertionError(f"Reading upload job {job_id} did not finish")


def test_media_reading_endpoint_returns_original_sentence_playback_anchors(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.services.v2_lessons import _store_media_segments

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=abc123def45",
        video_id="abc123def45",
        title="Demo",
    )
    _store_media_segments(lesson["id"], [
        {"index": 4, "start": 1.0, "end": 2.5, "text": "First sentence."},
        {"index": 5, "start": 2.7, "end": 4.0, "text": "Second sentence."},
    ])

    response = client.get(f"/api/v2/lessons/{lesson['id']}/reading")

    assert response.status_code == 200
    block = response.json()["blocks"][0]
    assert block["start_seconds"] == 1.0
    assert block["end_seconds"] == 4.0
    assert block["source_segment_ids"] == [4, 5]
    assert [item["sentence_key"] for item in block["sentences"]] == [0, 1]
    assert [item["segment_index"] for item in block["sentences"]] == [4, 5]


def test_media_reading_endpoint_rebuilds_legacy_fragment_projection(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="bilibili",
        source_url="https://www.bilibili.com/video/BV1legacy",
        title="Legacy projection",
    )
    segments = [
        {"index": 7, "start": 0.0, "end": 2.0, "text": "A sentence split across"},
        {"index": 8, "start": 2.0, "end": 4.0, "text": "subtitle fragments."},
    ]
    db.replace_v2_subtitle_segments(lesson["id"], segments)
    db.replace_v2_reading_blocks(lesson["id"], [{
        "index": 1,
        "text": "A sentence split across subtitle fragments.",
        "start_seconds": 0.0,
        "end_seconds": 4.0,
        "source_segment_ids": [7, 8],
        "sentences": [
            {
                "segment_index": 7,
                "source_segment_ids": [7],
                "text": "A sentence split across",
                "start_seconds": 0.0,
                "end_seconds": 2.0,
            },
            {
                "segment_index": 8,
                "source_segment_ids": [8],
                "text": "subtitle fragments.",
                "start_seconds": 2.0,
                "end_seconds": 4.0,
            },
        ],
    }])

    block = client.get(f"/api/v2/lessons/{lesson['id']}/reading").json()["blocks"][0]

    assert block["sentences"][0]["text"] == "A sentence split across subtitle fragments."
    assert block["sentences"][0]["sentence_key"] == 0
    assert block["sentences"][0]["source_segment_ids"] == [7, 8]


def test_media_lesson_exposes_listening_and_reading_modes(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=abc123def45",
        video_id="abc123def45",
        title="Demo",
    )
    db.replace_v2_reading_blocks(lesson["id"], [{"index": 1, "text": "A paragraph."}])

    response = client.get(f"/api/v2/lessons/{lesson['id']}/status")

    assert response.json()["available_modes"] == ["listening", "reading"]


def test_text_lesson_exposes_only_reading_mode(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="reading://mode-test",
        title="Passage",
        lesson_mode="reading",
    )
    db.replace_v2_reading_blocks(lesson["id"], [{"index": 1, "text": "A paragraph."}])

    response = client.get(f"/api/v2/lessons/{lesson['id']}/status")

    assert response.json()["available_modes"] == ["reading"]


def test_patch_mode_persists_valid_mode_and_rejects_unavailable_mode(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    media = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=abc123def45",
        video_id="abc123def45",
        title="Demo",
    )
    db.replace_v2_reading_blocks(media["id"], [{"index": 1, "text": "A paragraph."}])
    switched = client.patch(f"/api/v2/lessons/{media['id']}/mode", json={"mode": "reading"})
    assert switched.status_code == 200
    assert db.get_v2_lesson(media["id"])["lesson_mode"] == "reading"

    text = db.create_v2_lesson(
        source_type="reading_text",
        source_url="reading://mode-test",
        title="Passage",
        lesson_mode="reading",
    )
    db.replace_v2_reading_blocks(text["id"], [{"index": 1, "text": "A paragraph."}])
    rejected = client.patch(f"/api/v2/lessons/{text['id']}/mode", json={"mode": "listening"})
    assert rejected.status_code == 400
    assert db.get_v2_lesson(text["id"])["lesson_mode"] == "reading"


def test_capabilities_reading_pdf_lesson_without_media(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="reading_pdf",
        source_url="reading://pdf-caps",
        title="PDF passage",
        lesson_mode="reading",
    )
    db.replace_v2_reading_blocks(lesson["id"], [{"index": 1, "text": "A paragraph."}])

    status = client.get(f"/api/v2/lessons/{lesson['id']}/status").json()

    assert status["capabilities"] == {"can_listen": False, "can_read": True}
    assert status["available_modes"] == ["reading"]

    rejected = client.patch(f"/api/v2/lessons/{lesson['id']}/mode", json={"mode": "listening"})
    assert rejected.status_code == 400

    library = client.get("/api/v2/lessons/library").json()["lessons"]
    entry = next(c for c in library if c["id"] == lesson["id"])
    assert entry["capabilities"] == {"can_listen": False, "can_read": True}


def test_capabilities_youtube_lesson_can_listen(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=abc123def45",
        video_id="abc123def45",
        title="Demo",
    )
    db.replace_v2_reading_blocks(lesson["id"], [{"index": 1, "text": "A paragraph."}])

    status = client.get(f"/api/v2/lessons/{lesson['id']}/status").json()

    assert status["capabilities"] == {"can_listen": True, "can_read": True}


def test_start_youtube_lesson_returns_immediately(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    import webapp.services.v2_lessons as service

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(service, "enqueue_subtitle_fetch", lambda *args, **kwargs: None)

    app = create_app()
    client = TestClient(app)

    resp = client.post("/api/v2/lessons/start", json={
        "source_type": "youtube",
        "url": "https://www.youtube.com/watch?v=abc123def45",
    })

    assert resp.status_code == 200
    data = resp.json()
    assert data["lesson"]["video_id"] == "abc123def45"
    assert data["lesson"]["subtitle_status"] == "pending"
    assert data["lesson"]["title"] == "YouTube Lesson abc123def45"
    assert data["workspace_url"] == f"/workspace/{data['lesson']['id']}"


def test_start_youtube_lesson_backfills_blank_title_on_reuse(tmp_path, monkeypatch):
    import db
    import webapp.services.v2_lessons as service

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    url = "https://www.youtube.com/watch?v=abc123def45"
    existing = db.create_v2_lesson(
        source_type="youtube",
        source_url=url,
        video_id="abc123def45",
        title="",
    )

    result = service.start_youtube_lesson(url)

    assert result["lesson"]["id"] == existing["id"]
    assert result["lesson"]["title"] == "YouTube Lesson abc123def45"


def _stub_subtitle_pipeline(monkeypatch, service, title):
    bundle = SimpleNamespace(title=title)
    monkeypatch.setattr(service, "fetch_youtube_subtitles", lambda url: bundle)
    monkeypatch.setattr(service, "source_bundle_to_segment_dicts", lambda b: [])
    monkeypatch.setattr(service, "_store_media_segments", lambda *a, **k: None)
    monkeypatch.setattr(service, "_enqueue_media_alignment", lambda *a, **k: None)
    monkeypatch.setattr(service.credit_meter, "settle_current", lambda *a, **k: None)


def test_youtube_subtitle_fetch_backfills_real_title(tmp_path, monkeypatch):
    import db
    import webapp.services.v2_lessons as service

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    _stub_subtitle_pipeline(monkeypatch, service, "Real Video Title")

    lesson = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=abc123def45",
        video_id="abc123def45",
        title="YouTube Lesson abc123def45",
    )
    service._fetch_and_store_subtitles(lesson["id"], lesson["source_url"])

    saved = db.get_v2_lesson(lesson["id"])
    assert saved["title"] == "Real Video Title"
    assert saved["subtitle_status"] == "ready"


def test_youtube_subtitle_fetch_blank_title_keeps_fallback(tmp_path, monkeypatch):
    import db
    import webapp.services.v2_lessons as service

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    _stub_subtitle_pipeline(monkeypatch, service, "   ")

    lesson = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=abc123def45",
        video_id="abc123def45",
        title="YouTube Lesson abc123def45",
    )
    service._fetch_and_store_subtitles(lesson["id"], lesson["source_url"])

    saved = db.get_v2_lesson(lesson["id"])
    assert saved["title"] == "YouTube Lesson abc123def45"
    assert saved["subtitle_status"] == "ready"


def test_start_local_media_lesson_uses_v2_workspace(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    import webapp.services.v2_lessons as service

    media = tmp_path / "sample.mp3"
    media.write_bytes(b"fake mp3")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    captured = {}

    def fake_enqueue(*args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(service, "enqueue_local_import", fake_enqueue)

    app = create_app()
    client = TestClient(app)

    resp = client.post("/api/v2/lessons/start", json={
        "source_type": "local",
        "local_path": str(media),
    })

    assert resp.status_code == 200
    data = resp.json()
    assert data["lesson"]["source_type"] == "local_audio"
    assert data["lesson"]["media_kind"] == "local_audio"
    assert data["lesson"]["media_url"].startswith("/output/v2_assets/")
    assert data["lesson"]["translation_requested"] == 1
    assert data["lesson"]["translation_status"] == "pending"
    assert captured["translate"] is True
    assert data["workspace_url"] == f"/workspace/{data['lesson']['id']}"


def test_start_bilibili_lesson_uses_v2_workspace(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    import webapp.services.v2_lessons as service

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    captured = {}

    def fake_enqueue(lesson_id, url, **kwargs):
        captured["lesson_id"] = lesson_id
        captured["url"] = url
        captured.update(kwargs)

    monkeypatch.setattr(service, "enqueue_bilibili_import", fake_enqueue)

    app = create_app()
    client = TestClient(app)

    resp = client.post("/api/v2/lessons/start", json={
        "source_type": "bilibili",
        "url": "https://www.bilibili.com/video/BV123",
        "bilibili_page": "2",
        "download_mode": "audio",
    })

    assert resp.status_code == 200
    data = resp.json()
    assert data["lesson"]["source_type"] == "bilibili"
    assert data["workspace_url"] == f"/workspace/{data['lesson']['id']}"
    assert captured["lesson_id"] == data["lesson"]["id"]
    assert captured["url"].endswith("?p=2")
    assert captured["download_video"] is False


def test_start_reading_text_lesson_and_fetch_blocks(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    import webapp.services.v2_lessons as service

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    enqueued = []
    monkeypatch.setattr(service, "enqueue_reading_tts", lambda lesson_id: enqueued.append(lesson_id))
    app = create_app()
    client = TestClient(app)

    resp = client.post("/api/v2/lessons/start", json={
        "source_type": "reading_text",
        "title": "Reading Passage 1",
        "text": "A first paragraph.\n\nA second paragraph.",
        "tts": True,
    })

    assert resp.status_code == 200
    data = resp.json()
    assert data["lesson"]["lesson_mode"] == "reading"
    assert data["workspace_url"] == f"/workspace/{data['lesson']['id']}"
    assert enqueued == [data["lesson"]["id"]]

    blocks = client.get(f"/api/v2/lessons/{data['lesson']['id']}/reading")
    assert blocks.status_code == 200
    assert [b["text"] for b in blocks.json()["blocks"]] == [
        "A first paragraph.",
        "A second paragraph.",
    ]


def test_reading_highlights_are_initial_lesson_words_until_cancelled(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    import webapp.fastapi_routes.v2_lessons as routes
    import webapp.services.v2_vocab as vocab

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(
        vocab,
        "load_v2_wordlist_index",
        lambda: {
            "climate": {"word": "climate", "level": "ielts"},
            "migration": {"word": "migration", "level": "academic"},
        },
    )
    monkeypatch.setattr(vocab, "load_word_meanings", lambda: {"climate": "气候", "migration": "迁移"})
    monkeypatch.setattr(routes, "load_word_meanings", lambda: {"climate": "气候", "migration": "迁移"})
    monkeypatch.setattr(vocab, "_lookup_local_dict_meaning", lambda candidates: "")
    app = create_app()
    client = TestClient(app)
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:test",
        title="Reading Passage 1",
        lesson_mode="reading",
    )
    db.replace_v2_reading_blocks(lesson["id"], [
        {"index": 1, "text": "Climate change affects migration patterns."}
    ])

    reading = client.get(f"/api/v2/lessons/{lesson['id']}/reading")
    assert reading.status_code == 200
    assert [item["normalized"] for item in reading.json()["blocks"][0]["highlights"]] == ["climate", "migration"]
    assert client.get(f"/api/v2/lessons/{lesson['id']}/words").json()["words"] == []

    sync = client.post(f"/api/v2/lessons/{lesson['id']}/highlighted-words/sync")
    assert sync.status_code == 200
    assert sync.json()["synced"] == 2

    words = client.get(f"/api/v2/lessons/{lesson['id']}/words")
    assert words.status_code == 200
    assert words.json()["words"] == ["climate", "migration"]
    assert words.json()["meanings"] == {"climate": "气候", "migration": "迁移"}

    delete = client.delete(f"/api/v2/lessons/{lesson['id']}/word/climate")
    assert delete.status_code == 200

    refreshed = client.get(f"/api/v2/lessons/{lesson['id']}/reading")
    assert refreshed.status_code == 200
    assert [item["normalized"] for item in refreshed.json()["blocks"][0]["highlights"]] == ["migration"]
    resync = client.post(f"/api/v2/lessons/{lesson['id']}/highlighted-words/sync")
    assert resync.status_code == 200
    assert resync.json()["synced"] == 0
    words_after_delete = client.get(f"/api/v2/lessons/{lesson['id']}/words").json()
    assert words_after_delete["words"] == ["migration"]
    assert words_after_delete["hidden_words"] == ["climate"]


def test_mastered_word_is_not_highlighted_in_future_reading_lessons(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    import webapp.services.v2_vocab as vocab

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(
        vocab,
        "load_v2_wordlist_index",
        lambda: {"climate": {"word": "climate", "level": "ielts"}},
    )
    monkeypatch.setattr(vocab, "load_word_meanings", lambda: {"climate": "气候"})
    client = TestClient(create_app())
    assert client.post(
        "/api/vocab-review/activate",
        json={"word": "climate", "source": "manual"},
    ).status_code == 200
    assert client.patch(
        "/api/vocab-review/climate/lifecycle",
        json={"mastered": True},
    ).status_code == 200
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:mastered-reading",
        title="Mastered Reading",
        lesson_mode="reading",
    )
    db.replace_v2_reading_blocks(
        lesson["id"],
        [{"index": 1, "text": "Climate change affects everyone."}],
    )

    reading = client.get(f"/api/v2/lessons/{lesson['id']}/reading")

    assert reading.status_code == 200
    assert reading.json()["blocks"][0]["highlights"] == []


def test_start_reading_pdf_lesson_uses_text_layer(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    import webapp.services.v2_lessons as service

    pdf = tmp_path / "passage.pdf"
    pdf.write_bytes(b"%PDF fake")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(service, "extract_text_from_pdf", lambda path: "A PDF paragraph.\n\nAnother PDF paragraph.")

    app = create_app()
    client = TestClient(app)

    resp = client.post("/api/v2/lessons/start", json={
        "source_type": "reading_pdf",
        "local_path": str(pdf),
    })

    assert resp.status_code == 200
    data = resp.json()
    assert data["lesson"]["lesson_mode"] == "reading"
    blocks = client.get(f"/api/v2/lessons/{data['lesson']['id']}/reading").json()["blocks"]
    assert [b["text"] for b in blocks] == ["A PDF paragraph.", "Another PDF paragraph."]


def test_start_reading_pdf_lesson_reports_ocr_requirement(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    import webapp.services.v2_lessons as service

    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF scanned")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(
        service,
        "extract_text_from_pdf",
        lambda path: (_ for _ in ()).throw(ValueError("PDF 没有可读取的文本层，需要 OCR；当前环境缺少 pytesseract 或 Tesseract 程序。")),
    )

    app = create_app()
    client = TestClient(app)

    resp = client.post("/api/v2/lessons/start", json={
        "source_type": "reading_pdf",
        "local_path": str(pdf),
    })

    assert resp.status_code == 400
    assert "当前环境缺少 pytesseract 或 Tesseract" in resp.json()["detail"]


def test_upload_reading_txt_file_creates_reading_lesson(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    app = create_app()
    client = TestClient(app)

    resp = client.post(
        "/api/v2/lessons/reading/upload",
        files={"file": ("passage.txt", b"A txt paragraph.\n\nAnother paragraph.", "text/plain")},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["cached"] is False
    assert data["job_id"]

    status = _wait_for_upload(client, data["job_id"])
    assert status["stage"] == "done"
    assert status["percent"] == 100
    assert status["workspace_url"].startswith("/workspace/")
    lesson_id = int(status["workspace_url"].rsplit("/", 1)[-1])
    blocks = client.get(f"/api/v2/lessons/{lesson_id}/reading").json()["blocks"]
    assert [b["text"] for b in blocks] == ["A txt paragraph.", "Another paragraph."]


def test_upload_reading_file_reuses_cached_lesson(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    app = create_app()
    client = TestClient(app)
    upload = {"file": ("passage.txt", b"Cached paragraph.", "text/plain")}

    first = client.post("/api/v2/lessons/reading/upload", files=upload).json()
    first_status = _wait_for_upload(client, first["job_id"])

    second = client.post("/api/v2/lessons/reading/upload", files=upload)

    assert second.status_code == 200
    assert second.json() == {
        "cached": True,
        "workspace_url": first_status["workspace_url"],
    }


def test_upload_reading_cache_uses_content_digest_not_filename(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    content = b"Same passage under a different filename."

    first = client.post(
        "/api/v2/lessons/reading/upload",
        files={"file": ("first-name.txt", content, "text/plain")},
    ).json()
    first_status = _wait_for_upload(client, first["job_id"])
    second = client.post(
        "/api/v2/lessons/reading/upload",
        files={"file": ("renamed.txt", content, "text/plain")},
    )

    assert second.json() == {"cached": True, "workspace_url": first_status["workspace_url"]}


def test_upload_reading_deduplicates_inflight_digest(tmp_path, monkeypatch):
    import threading
    import db
    from fastapi_server import create_app
    import webapp.services.v2_lessons as service

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    release = threading.Event()
    monkeypatch.setattr(
        service,
        "extract_text_from_upload",
        lambda filename, content: (release.wait(2), "Inflight passage.")[1],
    )
    client = TestClient(create_app())
    content = b"same inflight bytes"

    first = client.post(
        "/api/v2/lessons/reading/upload",
        files={"file": ("one.txt", content, "text/plain")},
    ).json()
    second = client.post(
        "/api/v2/lessons/reading/upload",
        files={"file": ("two.txt", content, "text/plain")},
    ).json()
    release.set()

    assert second == {"cached": False, "job_id": first["job_id"], "status": "queued"}
    assert _wait_for_upload(client, first["job_id"])["stage"] == "done"


def test_upload_reading_rejects_when_background_workers_are_full(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    import webapp.services.v2_lessons as service

    class NoAvailableSlot:
        @staticmethod
        def acquire(blocking=False):
            return False

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(service, "_READING_UPLOAD_SLOTS", NoAvailableSlot())
    client = TestClient(create_app())

    response = client.post(
        "/api/v2/lessons/reading/upload",
        files={"file": ("busy.txt", b"unique busy content", "text/plain")},
    )

    assert response.status_code == 503
    assert "busy" in response.json()["detail"].lower()


def test_upload_reading_file_reports_background_failure(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    import webapp.services.v2_lessons as service

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(
        service,
        "extract_text_from_upload",
        lambda filename, content: (_ for _ in ()).throw(ValueError("Unreadable PDF")),
    )
    app = create_app()
    client = TestClient(app)

    upload = client.post(
        "/api/v2/lessons/reading/upload",
        files={"file": ("broken.pdf", b"%PDF broken", "application/pdf")},
    ).json()
    status = _wait_for_upload(client, upload["job_id"])

    assert status["stage"] == "failed"
    assert status["percent"] == 10
    assert status["message"] == "Reading import failed"
    assert status["error"] == "Unreadable PDF"
    assert status["workspace_url"] == ""


def test_upload_reading_pdf_file_uses_upload_parser(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    import webapp.services.v2_lessons as service

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(service, "extract_text_from_upload", lambda filename, content: "A PDF upload paragraph.")

    app = create_app()
    client = TestClient(app)

    resp = client.post(
        "/api/v2/lessons/reading/upload",
        files={"file": ("passage.pdf", b"%PDF fake", "application/pdf")},
    )

    assert resp.status_code == 200
    data = resp.json()
    status = _wait_for_upload(client, data["job_id"])
    assert status["stage"] == "done"
    lesson_id = int(status["workspace_url"].rsplit("/", 1)[-1])
    blocks = client.get(f"/api/v2/lessons/{lesson_id}/reading").json()["blocks"]
    assert [b["text"] for b in blocks] == ["A PDF upload paragraph."]


def test_lesson_status_returns_progress_and_readiness(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    import webapp.services.v2_lessons as service

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(service, "enqueue_subtitle_fetch", lambda *args, **kwargs: None)

    app = create_app()
    client = TestClient(app)
    lesson_id = client.post("/api/v2/lessons/start", json={
        "source_type": "youtube",
        "url": "https://www.youtube.com/watch?v=abc123def45",
    }).json()["lesson"]["id"]

    resp = client.get(f"/api/v2/lessons/{lesson_id}/status")
    assert resp.status_code == 200
    assert resp.json()["lesson"]["subtitle_status"] == "pending"


def test_course_library_lists_modes_progress_and_archive_state(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    import webapp.services.v2_lessons as service

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(service, "OUTPUT_DIR", tmp_path / "output")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:library-test",
        title="Library Course",
        duration=120,
        media_url="/output/v2_assets/1/reading.wav",
        media_kind="audio",
        lesson_mode="reading",
    )
    db.replace_v2_reading_blocks(
        lesson["id"],
        [{"index": 0, "text": "First sentence. Second sentence."}],
    )
    db.upsert_v2_lesson_progress(lesson["id"], 30, 1)

    response = client.get("/api/v2/lessons/library")

    assert response.status_code == 200
    course = response.json()["lessons"][0]
    assert course["id"] == lesson["id"]
    assert course["available_modes"] == ["listening", "reading"]
    assert course["progress_percent"] == 25
    assert course["intensive_ready"] is False

    export_dir = tmp_path / "output"
    export_dir.mkdir()
    (export_dir / f"v2-intensive-{lesson['id']}.html").write_text("ready", encoding="utf-8")
    ready_course = client.get("/api/v2/lessons/library").json()["lessons"][0]
    assert ready_course["intensive_ready"] is True
    assert ready_course["intensive_url"] == f"/workspace/{lesson['id']}/intensive"

    patched = client.patch(
        f"/api/v2/lessons/library/{lesson['id']}",
        json={"title": "Renamed Course", "archived": True},
    )
    assert patched.status_code == 200
    assert client.get("/api/v2/lessons/library").json()["lessons"] == []
    archived = client.get("/api/v2/lessons/library?include_archived=1").json()["lessons"][0]
    assert archived["title"] == "Renamed Course"
    assert archived["archived"] is True

def test_course_library_tags_counts_and_delete(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    import webapp.services.v2_lessons as service

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(service, "OUTPUT_DIR", tmp_path / "output")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:library-tags-test",
        title="Tagged Course",
    )
    db.upsert_word('complex', '2026-07-24')
    db.save_v2_lesson_word(lesson["id"], "complex", "A complex system.")
    db.save_v2_phase_b_sentence(lesson["id"], 0, 0.0, 4.0, "A complex system.")

    course = client.get("/api/v2/lessons/library").json()["lessons"][0]
    assert course["word_count"] == 1
    assert course["saved_sentence_count"] == 1
    assert course["tags"] == []

    patched = client.patch(
        f"/api/v2/lessons/library/{lesson['id']}",
        json={"tags": ["雅思", "听力", "雅思", ""]},
    )
    assert patched.status_code == 200
    course = client.get("/api/v2/lessons/library").json()["lessons"][0]
    assert course["tags"] == ["雅思", "听力"]

    assets = tmp_path / "output" / "v2_assets" / str(lesson["id"])
    assets.mkdir(parents=True)
    (assets / "reading.wav").write_bytes(b"wav")
    deleted = client.delete(f"/api/v2/lessons/library/{lesson['id']}")
    assert deleted.status_code == 200
    assert client.get("/api/v2/lessons/library").json()["lessons"] == []
    assert db.get_v2_lesson(lesson["id"]) is None
    assert not assets.exists()


def test_saved_sentences_support_listening_results_archive_and_legacy_ratings(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    from fastapi_server import create_app
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:sentence-review",
        title="Sentence Review Course",
        lesson_mode="reading",
    )
    first = db.save_v2_phase_b_sentence(
        lesson["id"], -10001, 0, 0, "The first saved sentence."
    )
    db.save_v2_phase_b_sentence(
        lesson["id"], -10002, 0, 0, "The second saved sentence."
    )
    db.replace_v2_sentence_tags(
        first["sentence_id"],
        [{"category": "structure", "name": "长难句"}],
    )

    queue = client.get("/api/v2/lessons/sentence-review")

    assert queue.status_code == 200
    data = queue.json()
    assert data["total"] == 2
    first_item = next(item for item in data["sentences"] if item["id"] == first["sentence_id"])
    assert first_item["lesson_titles"] == ["Sentence Review Course"]
    assert first_item["tags"][0]["name"] == "长难句"
    assert first_item["listening_result"] == "untested"
    assert first_item["archived"] is False

    reviewed = client.post(
        f"/api/v2/lessons/sentence-review/{first['sentence_id']}/listening-result",
        json={"result": "understood"},
    )

    assert reviewed.status_code == 200
    result = reviewed.json()["sentence"]
    assert result["review_count"] == 1
    assert result["last_reviewed_at"]
    assert result["listening_result"] == "understood"
    refreshed = client.get("/api/v2/lessons/sentence-review").json()
    assert refreshed["sentences"][0]["listening_result"] == "untested"

    archived = client.patch(
        f"/api/v2/lessons/sentence-review/{first['sentence_id']}",
        json={"archived": True},
    )
    assert archived.status_code == 200
    assert archived.json()["sentence"]["archived"] is True
    assert first["sentence_id"] not in {
        item["id"] for item in client.get("/api/v2/lessons/sentence-review").json()["sentences"]
    }
    archived_list = client.get(
        "/api/v2/lessons/sentence-review?include_archived=true"
    ).json()["sentences"]
    assert next(item for item in archived_list if item["id"] == first["sentence_id"])["archived"] is True

    legacy = client.post(
        f"/api/v2/lessons/sentence-review/{first['sentence_id']}",
        json={"rating": "hard"},
    )
    assert legacy.status_code == 200
    assert legacy.json()["sentence"]["listening_result"] == "not_understood"

    invalid = client.post(
        f"/api/v2/lessons/sentence-review/{first['sentence_id']}/listening-result",
        json={"result": "unknown"},
    )
    assert invalid.status_code == 400


def test_sentence_library_tags_share_catalog_and_support_replacement(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    from fastapi_server import create_app
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:sentence-library-tags",
        title="Tagged Sentence Course",
        lesson_mode="reading",
    )
    saved = db.save_v2_phase_b_sentence(
        lesson["id"], -10003, 0, 0, "This sentence uses the shared tag catalog."
    )
    sentence_id = saved["sentence_id"]

    updated = client.patch(
        f"/api/v2/lessons/sentence-review/{sentence_id}/tags",
        json={"tags": [
            {"category": "structure", "name": "长难句"},
            {"category": "practice", "name": "跟读"},
        ]},
    )
    assert updated.status_code == 200
    assert {(tag["category"], tag["name"]) for tag in updated.json()["tags"]} == {
        ("structure", "长难句"), ("practice", "跟读"),
    }

    catalog = client.get("/api/v2/lessons/sentence-tags").json()["tags"]
    assert {(tag["category"], tag["name"]) for tag in catalog} >= {
        ("structure", "长难句"), ("practice", "跟读"),
    }
    queue_item = next(
        item for item in client.get(
            "/api/v2/lessons/sentence-review?include_archived=true"
        ).json()["sentences"] if item["id"] == sentence_id
    )
    assert len(queue_item["tags"]) == 2

    replaced = client.patch(
        f"/api/v2/lessons/sentence-review/{sentence_id}/tags",
        json={"tags": [{"category": "practice", "name": "跟读"}]},
    )
    assert replaced.status_code == 200
    assert [(tag["category"], tag["name"]) for tag in replaced.json()["tags"]] == [
        ("practice", "跟读")
    ]
    assert client.patch(
        "/api/v2/lessons/sentence-review/999999/tags",
        json={"tags": []},
    ).status_code == 404


def test_sentence_review_queue_exposes_media_audio_and_lesson_links(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    media_lesson = db.create_v2_lesson(
        source_type="local_audio",
        source_url="manual:media-audio",
        title="Media Course",
        media_url="/output/demo/audio.mp3",
        media_kind="audio",
    )
    reading_lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:reading-no-audio",
        title="Reading Course",
        lesson_mode="reading",
    )
    saved = db.save_v2_phase_b_sentence(
        media_lesson["id"], 1000000005, 12.0, 15.5, "A sentence with real audio."
    )
    db.save_v2_phase_b_sentence(
        reading_lesson["id"], -20001, 0, 0, "A reading sentence without audio."
    )

    data = client.get("/api/v2/lessons/sentence-review").json()

    media_item = next(item for item in data["sentences"] if item["id"] == saved["sentence_id"])
    assert media_item["audio"] == {"url": "/output/demo/audio.mp3", "start": 12.0, "end": 15.5}
    assert media_item["lesson_links"][0]["lesson_id"] == media_lesson["id"]
    assert media_item["lesson_links"][0]["segment_index"] == 1000000005
    reading_item = next(
        item for item in data["sentences"] if item["lesson_titles"] == ["Reading Course"]
    )
    assert reading_item["audio"] is None


def test_sentence_review_queue_exposes_youtube_original_audio(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=CQQTwvDb5xg",
        video_id="CQQTwvDb5xg",
        title="YouTube Course",
    )
    saved = db.save_v2_phase_b_sentence(
        lesson["id"], 12, 78.3386, 87.1136, "Original YouTube sentence."
    )

    data = client.get("/api/v2/lessons/sentence-review").json()

    item = next(row for row in data["sentences"] if row["id"] == saved["sentence_id"])
    assert item["audio"] == {
        "kind": "youtube",
        "video_id": "CQQTwvDb5xg",
        "start": 78.3386,
        "end": 87.1136,
    }
    assert item["lesson_links"][0]["source_type"] == "youtube"
    assert item["lesson_links"][0]["video_id"] == "CQQTwvDb5xg"


def test_sentence_review_recovers_original_range_for_legacy_zero_timing(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=legacy",
        video_id="legacy",
        title="Legacy Course",
    )
    db.replace_v2_subtitle_segments(lesson["id"], [
        {"index": 3, "start": 10.0, "end": 11.0, "text": "This sentence"},
        {"index": 4, "start": 11.1, "end": 12.0, "text": "was saved before"},
        {"index": 5, "start": 12.1, "end": 13.2, "text": "timing was preserved."},
    ])
    saved = db.save_v2_phase_b_sentence(
        lesson["id"], 999999, 0, 0,
        "This sentence was saved before timing was preserved.",
    )

    data = client.get("/api/v2/lessons/sentence-review").json()

    item = next(row for row in data["sentences"] if row["id"] == saved["sentence_id"])
    assert item["audio"] == {
        "kind": "youtube", "video_id": "legacy", "start": 10.0, "end": 13.2,
    }


def test_sentence_pattern_is_opt_in_and_caches_template_and_scenario(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    from fastapi_server import create_app
    import webapp.fastapi_routes.v2_lessons as routes

    responses = iter([
        {"pattern_template": "I find it + adjective + to + verb"},
        {"scenario_cn": "我发现每天坚持阅读很有帮助。"},
    ])
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        payload = next(responses)
        message = SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(
        routes.ai_config,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:pattern",
        title="Pattern Course",
        lesson_mode="reading",
    )
    saved = db.save_v2_phase_b_sentence(
        lesson["id"], -30001, 0, 0, "I find it helpful to read every day."
    )
    sentence_id = saved["sentence_id"]

    before = client.get("/api/v2/lessons/sentence-review").json()["sentences"][0]
    assert before["has_pattern"] is False
    created = client.post(f"/api/v2/lessons/sentence-review/{sentence_id}/pattern")
    assert created.status_code == 200
    assert created.json()["cached"] is False
    assert created.json()["has_pattern"] is True
    assert created.json()["pattern_template"] == "I find it + adjective + to + verb"
    assert created.json()["scenario"] == ""
    cached = client.post(f"/api/v2/lessons/sentence-review/{sentence_id}/pattern")
    assert cached.json()["cached"] is True
    assert len(calls) == 1

    scenario = client.post(
        f"/api/v2/lessons/sentence-review/{sentence_id}/pattern/scenario",
        json={"regenerate": False},
    )
    assert scenario.status_code == 200
    assert scenario.json()["scenario"] == "我发现每天坚持阅读很有帮助。"
    assert scenario.json()["pattern"]["scenario_cn"] == "我发现每天坚持阅读很有帮助。"
    cached_scenario = client.post(
        f"/api/v2/lessons/sentence-review/{sentence_id}/pattern/scenario",
        json={"regenerate": False},
    )
    assert cached_scenario.json()["cached"] is True
    assert len(calls) == 2

    edited = client.patch(
        f"/api/v2/lessons/sentence-review/{sentence_id}/pattern",
        json={"pattern_template": "It is + adjective + to + verb"},
    )
    assert edited.status_code == 200
    assert edited.json()["pattern"]["scenario_cn"] == ""


def test_sentence_phonetics_generates_rule_version_and_caches(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:phonetics",
        title="Phonetics Course",
        lesson_mode="reading",
    )
    saved = db.save_v2_phase_b_sentence(
        lesson["id"], -30001, 0, 0, "She sells seashells by the seashore."
    )

    first = client.get(f"/api/v2/lessons/sentence-phonetics/{saved['sentence_id']}")

    assert first.status_code == 200
    payload = first.json()
    assert payload["phonetics"]
    assert payload["source"] == "rule"
    stored = db.get_v2_sentence_by_id(saved["sentence_id"])
    assert stored["phonetics"] == payload["phonetics"]
    assert stored["phonetics_source"] == "rule"

    db.set_v2_sentence_phonetics(saved["sentence_id"], "/ai version/", source="ai")
    second = client.get(f"/api/v2/lessons/sentence-phonetics/{saved['sentence_id']}").json()
    assert second == {"phonetics": "/ai version/", "source": "ai"}

    missing = client.get("/api/v2/lessons/sentence-phonetics/999999")
    assert missing.status_code == 404


def test_retry_subtitles_reenqueues_failed_lesson_and_clears_error(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.services import v2_lessons as service

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    calls = []
    monkeypatch.setattr(
        service,
        "enqueue_bilibili_import",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="bilibili",
        source_url="https://www.bilibili.com/video/BV1xx411c7mD",
        title="Retry Course",
    )
    db.set_v2_lesson_status(
        lesson["id"], subtitle_status="failed", subtitle_error="Error code: 403"
    )

    resp = client.post(
        f"/api/v2/lessons/{lesson['id']}/retry-subtitles",
        json={"whisper_model": "large-v3"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert len(calls) == 1
    assert calls[0][1]["whisper_model"] == "large-v3"
    updated = db.get_v2_lesson(lesson["id"])
    assert updated["subtitle_status"] == "pending"
    assert updated["subtitle_error"] == ""

    conflict = client.post(f"/api/v2/lessons/{lesson['id']}/retry-subtitles")
    assert conflict.status_code == 409


def test_word_meaning_route_returns_lookup_without_activating_review(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    import webapp.services.v2_vocab as vocab

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(vocab, "load_word_meanings", lambda: {"complex": "复杂的"})
    import webapp.fastapi_routes.v2_lessons as v2_routes
    monkeypatch.setattr(
        v2_routes.dict_service,
        "lookup_ecdict_meta",
        lambda w: {"frq": 1523, "bnc": None, "collins": 3, "oxford": 1, "tags": ["四级", "雅思"]},
    )

    app = create_app()
    client = TestClient(app)
    lesson = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=abc123def45",
        video_id="abc123def45",
        title="Demo",
    )

    resp = client.get(f"/api/v2/lessons/{lesson['id']}/word-meaning/complex")
    assert resp.status_code == 200
    data = resp.json()
    assert data["word"] == "complex"
    assert data["meaning"] == "复杂的"
    assert data["found"] is True
    assert isinstance(data.get("phonetic"), str) and len(data["phonetic"]) > 0
    assert "COCA #1523" in data["dict_meta"]
    # 考试标签按 may 要求不在 dict_meta 展示（format_ecdict_meta 刻意排除）
    assert "四级" not in data["dict_meta"]
    assert data["in_review_book"] is False
    assert db.is_word_in_review("complex") is False


def test_word_meaning_route_enables_external_fallback(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    import webapp.fastapi_routes.v2_lessons as routes

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab-fallback.db")
    calls = []

    def fake_lookup(word, *, allow_external_fallback=False):
        calls.append((word, allow_external_fallback))
        return {
            "word": "nonnegotiables",
            "lemma": "non-negotiable",
            "meaning": "不可妥协的条件",
            "phonetic": "",
            "found": True,
            "source": "external",
        }

    monkeypatch.setattr(routes, "lookup_word_meaning", fake_lookup)
    app = create_app()
    client = TestClient(app)
    lesson = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=abc123def45",
        video_id="abc123def45",
        title="Demo",
    )

    resp = client.get(
        f"/api/v2/lessons/{lesson['id']}/word-meaning/non-negotiables"
    )

    assert resp.status_code == 200
    assert resp.json()["meaning"] == "不可妥协的条件"
    assert calls == [("non-negotiables", True)]


def test_lookup_ignores_pending_meaning_cache(monkeypatch):
    import webapp.services.v2_vocab as vocab

    meanings = {"distractible": "查询中..."}
    vocab.clear_vocab_caches()
    monkeypatch.setattr(vocab, "load_word_meanings", lambda: meanings)
    monkeypatch.setattr(vocab, "_lookup_local_dict_meaning", lambda candidates: "")
    monkeypatch.setattr(vocab, "_translate_word_fallback", lambda word: "容易分心的")

    result = vocab.lookup_word_meaning("distractible", allow_external_fallback=True)

    assert result["source"] == "external"
    assert result["meaning"] == "容易分心的"
    vocab.clear_vocab_caches()


def test_save_word_retries_pending_meaning_with_external_lookup(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    import webapp.fastapi_routes.v2_lessons as routes

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab-pending.db")
    calls = []

    def fake_lookup(word, *, allow_external_fallback=False):
        calls.append((word, allow_external_fallback))
        return {
            "word": "distractible",
            "meaning": "容易分心的",
            "found": True,
            "source": "external",
        }

    monkeypatch.setattr(routes, "lookup_word_meaning", fake_lookup)
    app = create_app()
    client = TestClient(app)
    lesson = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=abc123def45",
        video_id="abc123def45",
        title="Demo",
    )

    saved = client.post(
        f"/api/v2/lessons/{lesson['id']}/word",
        json={"word": "distractible", "meaning": "查询中...", "sentence": "Stay focused."},
    )

    assert saved.status_code == 200
    assert saved.json()["meaning"] == "容易分心的"
    assert calls == [("distractible", True)]
    assert client.get(f"/api/v2/lessons/{lesson['id']}/words").json()["meanings"] == {
        "distractible": "容易分心的"
    }


def test_wordlist_upload_clears_v2_vocab_caches(tmp_path, monkeypatch):
    from fastapi_server import create_app
    import webapp.fastapi_routes.misc as misc
    import webapp.storage.wordlists as wl_storage

    monkeypatch.setattr(wl_storage, "USER_DIR", tmp_path)
    monkeypatch.setattr(wl_storage, "compile_user_wordlist", lambda *args, **kwargs: True)
    calls = {"cleared": 0}
    monkeypatch.setattr(misc, "clear_vocab_caches", lambda: calls.__setitem__("cleared", calls["cleared"] + 1))

    app = create_app()
    client = TestClient(app)

    resp = client.post(
        "/api/wordlists/upload",
        files={"file": ("academic.txt", b"urban\nmigration\n", "text/plain")},
        data={"name": "Academic", "tag": "IELTS"},
    )

    assert resp.status_code == 200
    assert resp.json()["key"] == "user_academic"
    assert calls["cleared"] == 1


def test_wordlist_expansion_returns_local_preview_without_uploading(monkeypatch):
    from fastapi_server import create_app
    import webapp.fastapi_routes.misc as misc

    monkeypatch.setattr(
        misc.wl_storage,
        "expand_with_local_word_families",
        lambda words: {
            "study": ["study", "studies", "studied", "studying"],
            "city": ["city", "cities"],
        },
    )
    client = TestClient(create_app())

    response = client.post(
        "/api/wordlists/expand",
        files={"file": ("prototype.txt", b"study\ncity\n", "text/plain")},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["original_count"] == 2
    assert data["local_family_count"] == 2
    assert data["added_count"] == 4
    assert data["words"] == ["cities", "city", "studied", "studies", "study", "studying"]


def test_wordlist_expansion_keeps_uncovered_words_as_is(monkeypatch):
    """无词形变化的词（功能词等）原样保留，不需要 AI 补漏。"""
    from fastapi_server import create_app
    import webapp.fastapi_routes.misc as misc

    monkeypatch.setattr(misc.wl_storage, "expand_with_local_word_families", lambda words: {})
    response = TestClient(create_app()).post(
        "/api/wordlists/expand",
        files={"file": ("plain.txt", b"alpha\nbravo\n", "text/plain")},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["local_family_count"] == 0
    assert data["added_count"] == 0
    assert data["words"] == ["alpha", "bravo"]


def test_wordlist_expansion_can_limit_a_large_source_for_preview(monkeypatch):
    from fastapi_server import create_app
    import webapp.fastapi_routes.misc as misc

    monkeypatch.setattr(misc.wl_storage, "expand_with_local_word_families", lambda words: {})
    response = TestClient(create_app()).post(
        "/api/wordlists/expand",
        files={"file": ("large.txt", b"zulu\nalpha\nbravo\ncharlie\n", "text/plain")},
        data={"limit": "3"},
    )

    assert response.status_code == 200
    assert response.json()["source_total_count"] == 4
    assert response.json()["original_count"] == 3
    assert response.json()["truncated_count"] == 1
    assert response.json()["words"] == ["alpha", "bravo", "zulu"]


def test_local_word_family_expansion_keeps_inflections_not_derivations():
    import webapp.storage.wordlists as wl_storage

    if not wl_storage.ECDICT_DB.exists():
        pytest.skip("ECDICT database not built; run python backend/build_ecdict.py")

    forms = set(wl_storage.expand_with_local_word_families(["study"])["study"])

    assert {"study", "studies", "studied", "studying"} <= forms
    assert "studious" not in forms


def test_wordlist_expansion_endpoint_returns_completed_result(monkeypatch):
    from fastapi_server import create_app
    import webapp.fastapi_routes.misc as misc

    monkeypatch.setattr(
        misc.wl_storage,
        "expand_with_local_word_families",
        lambda words: {"study": ["study", "studies", "studied", "studying"]},
    )
    client = TestClient(create_app())
    response = client.post(
        "/api/wordlists/expand",
        files={"file": ("prototype.txt", b"study\n", "text/plain")},
        data={"limit": "1000"},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is True
    assert result["local_family_count"] == 1
    assert result["words"] == ["studied", "studies", "study", "studying"]

def test_homepage_hides_pattern_upload_and_keeps_wordlist_expansion_optional():
    from fastapi_server import create_app

    page = TestClient(create_app()).get("/")

    assert page.status_code == 200
    assert 'onclick="expandSelectedWordlist()"' in page.text
    assert "扩展词形" in page.text
    assert "formData.append('limit', '1000')" not in page.text
    assert 'fetch(\'/api/wordlists/expand\'' in page.text
    assert "/api/wordlists/expand/start" not in page.text
    assert "expandedWordlistSelection?.sourceName" in page.text
    assert "toggleResourceWordlist" in page.text
    assert "setAllResourceWordlists(true)" in page.text
    assert "setAllResourceWordlists(false)" in page.text
    assert "KNOWN_USER_WORDLISTS_KEY" in page.text
    pattern_heading = page.text.index("上传重点句式表")
    hidden_section = page.text.rfind('class="resource-section" hidden aria-hidden="true"', 0, pattern_heading)
    assert hidden_section >= 0


def test_phase_b_sentence_can_be_deleted(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    app = create_app()
    client = TestClient(app)
    lesson = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=abc123def45",
        video_id="abc123def45",
        title="Demo",
    )

    save = client.post(f"/api/v2/lessons/{lesson['id']}/phase-b", json={
        "segment_index": 3,
        "start_seconds": 1,
        "end_seconds": 2,
        "text": "A useful sentence.",
    })
    assert save.status_code == 200
    assert save.json()["saved"] is True

    delete = client.delete(f"/api/v2/lessons/{lesson['id']}/phase-b/3")
    assert delete.status_code == 200
    assert delete.json()["saved"] is False
    assert db.get_v2_phase_b_sentences(lesson["id"]) == []


def test_reading_sentence_can_be_saved_for_phase_b(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    app = create_app()
    client = TestClient(app)
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:test",
        title="Reading Passage 1",
        lesson_mode="reading",
    )
    db.replace_v2_reading_blocks(lesson["id"], [{"index": 1, "text": "A sentence worth studying."}])

    resp = client.post(f"/api/v2/lessons/{lesson['id']}/reading/saved-sentences", json={
        "block_index": 1,
        "text": "A sentence worth studying.",
    })

    assert resp.status_code == 200
    saved = client.get(f"/api/v2/lessons/{lesson['id']}/phase-b").json()
    assert any(item["text"] == "A sentence worth studying." for item in saved["sentences"])


def test_phase_b_sentence_tags_can_be_customized_with_fixed_categories(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    app = create_app()
    client = TestClient(app)
    lesson = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=tagdemo12345",
        video_id="tagdemo12345",
        title="Tag Demo",
    )
    client.post(f"/api/v2/lessons/{lesson['id']}/phase-b", json={
        "segment_index": 5,
        "start_seconds": 1,
        "end_seconds": 3,
        "text": "This is a reusable pattern.",
    })

    catalog = client.get("/api/v2/lessons/sentence-tags")
    assert catalog.status_code == 200
    assert {item["id"] for item in catalog.json()["categories"]} == {
        "vocabulary", "pronunciation", "structure", "expression", "practice"
    }

    created = client.post("/api/v2/lessons/sentence-tags", json={
        "category": "structure",
        "name": "自定义句式",
    })
    assert created.status_code == 200
    assert created.json()["tag"]["source"] == "user"

    tagged = client.post(f"/api/v2/lessons/{lesson['id']}/phase-b/5/tags", json={
        "tags": [
            {"category": "structure", "name": "自定义句式"},
            {"category": "practice", "name": "跟读"},
        ]
    })
    assert tagged.status_code == 200
    assert {tag["name"] for tag in tagged.json()["tags"]} == {"自定义句式", "跟读"}

    saved = client.get(f"/api/v2/lessons/{lesson['id']}/phase-b").json()["sentences"]
    assert {tag["category"] for tag in saved[0]["tags"]} == {"structure", "practice"}


def test_v2_word_can_be_saved_and_deleted(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    import webapp.services.v2_vocab as vocab

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(vocab, "load_word_meanings", lambda: {"complex": "复杂的"})
    monkeypatch.setattr(vocab, "_translate_word_fallback", lambda word: "")
    app = create_app()
    client = TestClient(app)
    lesson = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=abc123def45",
        video_id="abc123def45",
        title="Demo",
    )

    save = client.post(f"/api/v2/lessons/{lesson['id']}/word", json={
        "word": "complex",
        "meaning": "复杂的",
        "sentence": "A complex system.",
    })
    assert save.status_code == 200
    assert save.json()["saved"] is True
    assert "complex" in db.get_all_words()
    assert db.is_word_in_review("complex") is True
    assert db.get_v2_lesson_word(lesson["id"], "complex")["sentence"] == "A complex system."
    words = client.get(f"/api/v2/lessons/{lesson['id']}/words")
    assert words.status_code == 200
    assert words.json()["words"] == ["complex"]
    assert words.json()["review_words"] == ["complex"]
    assert words.json()["hidden_words"] == []

    state = client.get(f"/api/v2/lessons/{lesson['id']}/word-state/complex")
    assert state.status_code == 200
    assert state.json()["saved"] is True

    delete = client.delete(f"/api/v2/lessons/{lesson['id']}/word/complex")
    assert delete.status_code == 200
    assert delete.json()["saved"] is False
    assert "complex" in db.get_all_words()
    assert db.get_v2_lesson_word(lesson["id"], "complex") is None
    assert "complex" in db.get_v2_lesson_hidden_words(lesson["id"])

    state_after_delete = client.get(f"/api/v2/lessons/{lesson['id']}/word-state/complex")
    assert state_after_delete.status_code == 200
    assert state_after_delete.json()["saved"] is False
    words_after_delete = client.get(f"/api/v2/lessons/{lesson['id']}/words")
    assert words_after_delete.json()["words"] == []
    assert words_after_delete.json()["hidden_words"] == ["complex"]


def test_subtitles_skip_hidden_words_after_word_delete(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    import webapp.services.v2_vocab as vocab

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(vocab, "load_default_intermediate_words", lambda: {"complex", "analyzed"})
    monkeypatch.setattr(vocab, "load_word_meanings", lambda: {"complex": "复杂的", "analyzed": "分析"})
    import webapp.fastapi_routes.v2_lessons as v2_routes
    monkeypatch.setattr(v2_routes, "load_exclude_words", lambda: set())
    app = create_app()
    client = TestClient(app)
    lesson = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=abc123def45",
        video_id="abc123def45",
        title="Demo",
    )
    db.replace_v2_subtitle_segments(lesson["id"], [
        {"index": 0, "start": 0, "end": 2, "text": "We analyzed complex systems."}
    ])
    db.upsert_word("complex", "2026-07-12", level="v2", analysis={"basic_meaning": "复杂的"})
    db.save_v2_lesson_word(lesson["id"], "complex", "We analyzed complex systems.")

    before = client.get(f"/api/v2/lessons/{lesson['id']}/subtitles")
    assert before.status_code == 200
    assert before.json()["segments"][0]["highlighted_words"] == ["analyzed", "complex"]

    sync = client.post(f"/api/v2/lessons/{lesson['id']}/highlighted-words/sync")
    assert sync.status_code == 200
    assert sync.json()["synced"] == 1
    assert client.get(f"/api/v2/lessons/{lesson['id']}/words").json()["words"] == ["complex", "analyzed"]

    delete = client.delete(f"/api/v2/lessons/{lesson['id']}/word/complex")
    assert delete.status_code == 200

    after = client.get(f"/api/v2/lessons/{lesson['id']}/subtitles")
    assert after.status_code == 200
    assert after.json()["segments"][0]["highlighted_words"] == ["analyzed"]
    resync = client.post(f"/api/v2/lessons/{lesson['id']}/highlighted-words/sync")
    assert resync.status_code == 200
    assert resync.json()["synced"] == 0


def test_subtitles_highlight_follows_selected_wordlists(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    import webapp.services.v2_vocab as vocab

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    compiled = tmp_path / "compiled"
    compiled.mkdir()
    (compiled / "builtin_gre.json").write_text(json.dumps({
        "metadata": {"name": "GRE", "type": "domain", "key": "gre", "builtin": True},
        "words": ["complex", "systems"],
    }), encoding="utf-8")
    (compiled / "builtin_coca_2000.json").write_text(json.dumps({
        "metadata": {"name": "COCA2000", "type": "exclude", "key": "coca2000", "builtin": True},
        "words": ["systems"],
    }), encoding="utf-8")
    monkeypatch.setattr(vocab, "_COMPILED_DIR", compiled)
    monkeypatch.setattr(vocab, "load_default_intermediate_words", lambda: {"analyzed"})
    monkeypatch.setattr(vocab, "load_word_meanings", lambda: {})
    app = create_app()
    client = TestClient(app)
    lesson = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=abc123def45",
        video_id="abc123def45",
        title="Demo",
    )
    db.replace_v2_subtitle_segments(lesson["id"], [
        {"index": 0, "start": 0, "end": 2, "text": "We analyzed complex systems."}
    ])

    default = client.get(f"/api/v2/lessons/{lesson['id']}/subtitles")
    assert default.json()["segments"][0]["highlighted_words"] == ["analyzed"]

    # 选择 GRE 词表：complex 命中；systems 虽在词表中但被 exclude 基础词表过滤
    selected = client.get(f"/api/v2/lessons/{lesson['id']}/subtitles?wordlists=builtin_gre")
    assert selected.json()["segments"][0]["highlighted_words"] == ["complex"]
    # 词表归属映射：前端据此着色
    assert selected.json()["segments"][0]["highlighted_word_lists"] == {"complex": "builtin_gre"}
    assert any(
        u.get("highlighted_word_lists", {}).get("complex") == "builtin_gre"
        for u in selected.json()["sentence_units"]
    )

    # 虚拟生词本词表
    db.activate_word_review("analyzed", source="manual", analysis={"basic_meaning": "分析"})
    vocab_book = client.get(f"/api/v2/lessons/{lesson['id']}/subtitles?wordlists=my_vocab")
    assert vocab_book.json()["segments"][0]["highlighted_words"] == ["analyzed"]
    assert vocab_book.json()["segments"][0]["highlighted_word_lists"] == {"analyzed": "my_vocab"}

    # 空选择：不高亮任何词
    none = client.get(f"/api/v2/lessons/{lesson['id']}/subtitles?wordlists=")
    assert none.json()["segments"][0]["highlighted_words"] == []


def test_resume_interrupted_translations_on_startup(tmp_path, monkeypatch):
    import db
    import fastapi_server
    import webapp.services.v2_translation as v2_translation

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    stuck = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=stuck123abc",
        video_id="stuck123abc",
        title="Stuck",
    )
    db.configure_v2_lesson_translation(stuck["id"], requested=True)
    db.update_v2_translation_status(stuck["id"], status="translating", done=3, total=10, ready=False)
    other = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=done123abcd",
        video_id="done123abcd",
        title="Done",
    )
    db.configure_v2_lesson_translation(other["id"], requested=True)
    db.update_v2_translation_status(other["id"], status="ready", done=10, total=10, ready=True)

    resumed: list[int] = []

    def fake_translate(lesson_id: int) -> dict:
        resumed.append(int(lesson_id))
        return {"status": "ready"}

    monkeypatch.setattr(v2_translation, "translate_lesson_subtitles", fake_translate)
    fastapi_server._resume_interrupted_translations()
    for _ in range(50):
        if resumed:
            break
        time.sleep(0.05)
    assert resumed == [stuck["id"]]


def test_lesson_words_exclude_mastered_and_known(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    app = create_app()
    client = TestClient(app)
    lesson = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=abc123def45",
        video_id="abc123def45",
        title="Demo",
    )
    db.upsert_word("column", "2026-08-04", level="v2")
    db.upsert_word("excel", "2026-08-04", level="v2")
    db.save_v2_lesson_word(lesson["id"], "column", "a column of data")
    db.save_v2_lesson_word(lesson["id"], "excel", "excel at work")
    db.add_known_word("column", "2026-08-04")

    resp = client.get(f"/api/v2/lessons/{lesson['id']}/words")
    assert resp.status_code == 200
    assert resp.json()["words"] == ["excel"]


def test_active_words_returns_saved_meanings(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    app = create_app()
    client = TestClient(app)
    db.upsert_word("complex", "2026-07-04", level="v2", analysis={"basic_meaning": "复杂的"})
    db.activate_word_review(
        "complex",
        source="manual",
        analysis={"basic_meaning": "复杂的"},
    )

    resp = client.get("/api/active-words")

    assert resp.status_code == 200
    assert "complex" in resp.json()["words"]
    assert resp.json()["meanings"]["complex"] == "复杂的"


def test_reading_saved_sentence_accepts_frontend_payload_shape(tmp_path, monkeypatch):
    """回归：前端整句收藏把 source 作为对象上送，模型曾声明 source: str 导致 422。"""
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="reading://save-shape",
        title="Reading save shape",
        lesson_mode="reading",
    )
    db.replace_v2_reading_blocks(lesson["id"], [{"index": 0, "text": "A paragraph. Another sentence."}])

    response = client.post(
        f"/api/v2/lessons/{lesson['id']}/reading/saved-sentences",
        json={
            "block_index": -10001,
            "text": "A paragraph.",
            "start_seconds": 0,
            "end_seconds": 0,
            "lesson_id": lesson["id"],
            "mode": "reading",
            "sentence_key": "-10001",
            "source": {
                "lesson_id": lesson["id"],
                "mode": "reading",
                "block_index": 0,
                "start_seconds": None,
                "end_seconds": None,
            },
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True


# ── 普通用户浏览器音视频上传（Task 3）─────────────────────────


def _upload_env(tmp_path, monkeypatch, *, duration=120.0, kind="local_video"):
    """单用户上传测试环境：隔离 DB/output/uploads，桩掉 ffprobe 与后台导入。"""
    import db
    import webapp.services.v2_lessons as service
    from webapp.storage import user_assets

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(user_assets, "GLOBAL_OUTPUT_DIR", tmp_path / "global-output")
    monkeypatch.setattr(service, "OUTPUT_DIR", tmp_path / "global-output")
    monkeypatch.setattr(
        service, "_probe_uploaded_media", lambda path: (duration, kind))
    monkeypatch.setattr(service, "enqueue_local_import", lambda *a, **k: None)
    from fastapi_server import create_app
    return service, TestClient(create_app())


def _post_media(client, name="lesson.mp4", payload=b"fake-media-bytes"):
    return client.post(
        "/api/v2/lessons/media-uploads",
        files={"file": (name, payload, "application/octet-stream")},
    )


def test_media_upload_returns_quote_and_ready_record(tmp_path, monkeypatch):
    import db
    service, client = _upload_env(tmp_path, monkeypatch, duration=120.0)

    resp = _post_media(client)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["upload_id"] if "upload_id" in data else data["id"]
    upload_id = data.get("upload_id") or data["id"]
    assert data["media_kind"] == "local_video"
    assert data["duration_seconds"] == 120.0
    assert data["quote"] == {
        "operation_type": "course_build_media",
        "points": 10,  # 120s → 2 分钟 × 5 分
        "rate_version": "shadow-v1",
        "mode": "shadow",
    }
    record = db.get_v2_media_upload(upload_id)
    assert record["status"] == "ready"
    stored = tmp_path / "global-output" / "uploads" / upload_id / "lesson.mp4"
    assert stored.is_file() and stored.read_bytes() == b"fake-media-bytes"


def test_media_upload_rejects_bad_extension_and_leaves_no_staging(tmp_path, monkeypatch):
    _, client = _upload_env(tmp_path, monkeypatch)

    resp = _post_media(client, name="evil.exe")

    assert resp.status_code == 400
    uploads_root = tmp_path / "global-output" / "uploads"
    assert not uploads_root.exists() or not any(uploads_root.iterdir())


def test_media_upload_rejects_fake_media_and_cleans_staging(tmp_path, monkeypatch):
    import webapp.services.v2_lessons as service
    _, client = _upload_env(tmp_path, monkeypatch)

    def fake_probe(path):
        raise service.MediaUploadError("无法识别的媒体文件")

    monkeypatch.setattr(service, "_probe_uploaded_media", fake_probe)
    resp = _post_media(client, name="fake.mp4")  # 扩展名合法但内容不是媒体

    assert resp.status_code == 400
    uploads_root = tmp_path / "global-output" / "uploads"
    assert not uploads_root.exists() or not any(uploads_root.iterdir())


def test_media_upload_over_size_limit_413_and_cleans_staging(tmp_path, monkeypatch):
    monkeypatch.setenv("ELT_MEDIA_UPLOAD_MAX_MB", "1")
    _, client = _upload_env(tmp_path, monkeypatch)

    resp = _post_media(client, payload=b"x" * (1024 * 1024 + 8))

    assert resp.status_code == 413
    uploads_root = tmp_path / "global-output" / "uploads"
    assert not uploads_root.exists() or not any(uploads_root.iterdir())


def test_uploaded_media_start_consumes_upload_and_copies_media(tmp_path, monkeypatch):
    import db
    _, client = _upload_env(tmp_path, monkeypatch, duration=90.0, kind="local_audio")
    upload = _post_media(client, name="talk.mp3").json()
    upload_id = upload.get("upload_id") or upload["id"]

    resp = client.post("/api/v2/lessons/start", json={
        "source_type": "uploaded_media",
        "upload_id": upload_id,
        "whisper_model": "groq",
        "local_path": "/etc/passwd",  # 普通用户上送路径字段必须被忽略
    }, headers={"Idempotency-Key": "test-key-1"})

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["lesson"]["source_type"] == "uploaded_media"
    assert data["lesson"]["media_kind"] == "local_audio"
    lesson_id = int(data["lesson"]["id"])
    media_url = data["lesson"]["media_url"]
    assert media_url.startswith("/output/v2_assets/")
    copied = tmp_path / "global-output" / "v2_assets" / str(lesson_id) / "talk.mp3"
    assert copied.is_file()
    record = db.get_v2_media_upload(upload_id)
    assert record["status"] == "consumed" and record["consumed_at"]

    # 同一 upload 不能二次消费
    again = client.post("/api/v2/lessons/start", json={
        "source_type": "uploaded_media", "upload_id": upload_id,
    })
    assert again.status_code == 409


def test_uploaded_media_start_unknown_upload_404(tmp_path, monkeypatch):
    _, client = _upload_env(tmp_path, monkeypatch)
    resp = client.post("/api/v2/lessons/start", json={
        "source_type": "uploaded_media", "upload_id": "nonexistent",
    })
    assert resp.status_code == 404


def test_media_upload_delete_then_start_409(tmp_path, monkeypatch):
    _, client = _upload_env(tmp_path, monkeypatch)
    upload = _post_media(client).json()
    upload_id = upload.get("upload_id") or upload["id"]

    assert client.delete(f"/api/v2/lessons/media-uploads/{upload_id}").status_code == 200
    assert client.delete(f"/api/v2/lessons/media-uploads/{upload_id}").status_code == 404
    resp = client.post("/api/v2/lessons/start", json={
        "source_type": "uploaded_media", "upload_id": upload_id,
    })
    assert resp.status_code == 404  # 已删除的上传与不存在同等语义


def test_local_path_requires_admin_in_multiuser(tmp_path, monkeypatch):
    """多用户模式普通用户走 local_path 建课：后端 404（管理员私有入口保持）。"""
    import importlib
    pytest.importorskip("webapp.auth.store", reason="auth 仅私有库/云端，公开库跳过")
    monkeypatch.setenv("ELT_AUTH_ENABLED", "1")
    monkeypatch.setenv("ELT_AUTH_DB", str(tmp_path / "auth.db"))
    monkeypatch.setenv("ELT_USERS_ROOT", str(tmp_path / "users"))
    import webapp.auth.store as store_mod
    importlib.reload(store_mod)
    import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "default" / "vocab.db")
    db._initialized_paths.clear()
    import fastapi_server
    importlib.reload(fastapi_server)
    client = TestClient(fastapi_server.create_app())

    code = store_mod.create_invite_code(created_by="may")
    resp = client.post("/api/auth/register", json={
        "username": "alice", "password": "secret6", "invite_code": code})
    assert resp.status_code == 200

    media = tmp_path / "sample.mp3"
    media.write_bytes(b"fake")
    resp = client.post("/api/v2/lessons/start", json={
        "source_type": "local", "local_path": str(media)})
    assert resp.status_code == 404

    store_mod.create_user("boss", "adminpass1", is_admin=True)
    client.post("/api/auth/login", json={"username": "boss", "password": "adminpass1"})
    import webapp.services.v2_lessons as service
    monkeypatch.setattr(service, "enqueue_local_import", lambda *a, **k: None)
    monkeypatch.setattr(service, "OUTPUT_DIR", tmp_path / "users" / "boss" / "output")
    resp = client.post("/api/v2/lessons/start", json={
        "source_type": "local", "local_path": str(media)})
    assert resp.status_code == 200
    monkeypatch.delenv("ELT_AUTH_ENABLED")


# ── 上传建课：并发安全 claim + 失败回滚（Task 3 复审）─────────


def _service_upload(tmp_path, monkeypatch, name="talk.mp4", duration=60.0):
    """service 级上传（不走路由），返回 (service, upload 记录)。"""
    import io
    import webapp.services.v2_lessons as service
    from webapp.storage import user_assets
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(user_assets, "GLOBAL_OUTPUT_DIR", tmp_path / "global-output")
    monkeypatch.setattr(service, "OUTPUT_DIR", tmp_path / "global-output")
    monkeypatch.setattr(service, "_probe_uploaded_media", lambda p: (duration, "local_video"))
    upload = service.save_media_upload(name, io.BytesIO(b"fake-media"))
    return service, upload


def test_uploaded_media_missing_file_not_consumed_and_retryable(tmp_path, monkeypatch):
    import db
    service, upload = _service_upload(tmp_path, monkeypatch)
    stored = tmp_path / "global-output" / "uploads" / upload["id"] / "talk.mp4"
    stored.unlink()  # 暂存文件丢失

    with pytest.raises(FileNotFoundError):
        service.start_uploaded_media_lesson(upload["id"])
    assert db.get_v2_media_upload(upload["id"])["status"] == "ready"
    assert db.list_v2_lessons() == []

    # 补齐文件后可原样重试并成功消费
    stored.write_bytes(b"fake-media")
    monkeypatch.setattr(service, "enqueue_local_import", lambda *a, **k: None)
    result = service.start_uploaded_media_lesson(upload["id"])
    assert result["lesson"]["source_type"] == "uploaded_media"
    assert db.get_v2_media_upload(upload["id"])["status"] == "consumed"


def test_uploaded_media_copy_failure_rolls_back(tmp_path, monkeypatch):
    import db
    service, upload = _service_upload(tmp_path, monkeypatch)
    real_copy = service._copy_media_for_lesson

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(service, "_copy_media_for_lesson", boom)
    with pytest.raises(RuntimeError, match="disk full"):
        service.start_uploaded_media_lesson(upload["id"])

    assert db.get_v2_media_upload(upload["id"])["status"] == "ready"
    assert db.list_v2_lessons() == []
    assert not (tmp_path / "global-output" / "v2_assets").exists()

    # 回滚后可重试成功
    monkeypatch.setattr(service, "_copy_media_for_lesson", real_copy)
    monkeypatch.setattr(service, "enqueue_local_import", lambda *a, **k: None)
    result = service.start_uploaded_media_lesson(upload["id"])
    assert result["lesson"]["source_type"] == "uploaded_media"
    assert db.get_v2_media_upload(upload["id"])["status"] == "consumed"


def test_uploaded_media_enqueue_failure_rolls_back(tmp_path, monkeypatch):
    import db
    service, upload = _service_upload(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("queue broken")

    monkeypatch.setattr(service, "enqueue_local_import", boom)
    with pytest.raises(RuntimeError, match="queue broken"):
        service.start_uploaded_media_lesson(upload["id"])

    # upload 恢复 ready；lesson 与已复制资产全部清除
    assert db.get_v2_media_upload(upload["id"])["status"] == "ready"
    assert db.list_v2_lessons() == []
    assets_root = tmp_path / "global-output" / "v2_assets"
    assert not assets_root.exists() or not any(assets_root.iterdir())
    # 暂存文件保留，可重试
    assert (tmp_path / "global-output" / "uploads" / upload["id"] / "talk.mp4").is_file()


def test_uploaded_media_concurrent_start_single_consume(tmp_path, monkeypatch):
    """并发消费同一 upload：只有一个获胜者，另一个 409，upload 终态 consumed。"""
    import threading
    import db
    service, upload = _service_upload(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "enqueue_local_import", lambda *a, **k: None)

    results, errors = [], []
    barrier = threading.Barrier(2)

    def worker():
        try:
            barrier.wait(timeout=10)
            results.append(service.start_uploaded_media_lesson(upload["id"]))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(results) == 1
    assert len(errors) == 1 and isinstance(errors[0], service.MediaUploadError)
    assert db.get_v2_media_upload(upload["id"])["status"] == "consumed"
    assert len(db.list_v2_lessons()) == 1


def test_uploaded_media_missing_file_route_404_and_ready(tmp_path, monkeypatch):
    """路由层：暂存文件缺失返回 404，upload 保持 ready。"""
    import db
    _, client = _upload_env(tmp_path, monkeypatch)
    upload = _post_media(client, name="talk.mp4").json()
    stored = tmp_path / "global-output" / "uploads" / upload["upload_id"] / "talk.mp4"
    stored.unlink()

    resp = client.post("/api/v2/lessons/start", json={
        "source_type": "uploaded_media", "upload_id": upload["upload_id"]})
    assert resp.status_code == 404
    assert db.get_v2_media_upload(upload["upload_id"])["status"] == "ready"
