import os
import sys
import wave

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


@pytest.fixture(autouse=True)
def _clean_tts_state():
    """模块级 active/cancel 集合按 (scope, lesson_id) 计，测试间 lesson id 重复会互相污染。"""
    from webapp.services import v2_tts
    with v2_tts._ACTIVE_LOCK:
        v2_tts._ACTIVE_LESSONS.clear()
        v2_tts._CANCEL_REQUESTED.clear()
    yield
    with v2_tts._ACTIVE_LOCK:
        v2_tts._ACTIVE_LESSONS.clear()
        v2_tts._CANCEL_REQUESTED.clear()


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
        _make_wav(output_path, frames=1600)  # 0.2s @ 8kHz
        return [
            {"text": "First", "offset": 0.0, "duration": 0.04},
            {"text": "sentence", "offset": 0.04, "duration": 0.06},
            {"text": "Second", "offset": 0.1, "duration": 0.04},
            {"text": "sentence", "offset": 0.14, "duration": 0.06},
        ]

    monkeypatch.setattr(v2_tts, "synthesize_natural_speech_with_timestamps", fake_synthesize)
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


def test_enqueue_reading_tts_opens_listening_capability_immediately(tmp_path, monkeypatch):
    """TTS 排队即声明 generated_audio：生成中能力开放精听（加载态），不等成品落盘。"""
    import db
    from webapp.services import v2_lessons, v2_tts

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    lesson = db.create_v2_lesson(
        source_type="reading_upload",
        source_url="upload:queued-tts",
        title="Queued TTS",
        lesson_mode="reading",
    )
    monkeypatch.setattr(db, "spawn_with_db_context", lambda *a, **k: None)

    assert v2_tts.enqueue_reading_tts(lesson["id"]) is True

    updated = db.get_v2_lesson(lesson["id"])
    assert updated["media_kind"] == "generated_audio"
    assert updated["media_url"] == ""
    assert updated["subtitle_status"] == "pending"
    assert v2_lessons.get_lesson_capabilities(updated) == {"can_listen": True, "can_read": False}


def _make_wav(output_path, frames: int = 800):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8_000)
        audio.writeframes(b"\0\0" * frames)


def test_build_reading_tts_retries_transient_synthesis_failure(tmp_path, monkeypatch):
    """edge_tts 单次抖动（NoAudioReceived）按块重试后成功，整篇合成不中断。"""
    import db
    from webapp.services import v2_tts

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(v2_tts, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(v2_tts.time, "sleep", lambda _s: None)
    db.init_db()
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:tts-retry",
        title="TTS Retry",
        lesson_mode="reading",
    )
    db.replace_v2_reading_blocks(lesson["id"], [{"index": 1, "text": "First sentence. Second sentence!"}])

    calls = []

    def flaky(text, output_path):
        calls.append(text)
        if len(calls) == 1:
            raise RuntimeError("No audio was received. Please verify that your parameters are correct.")
        _make_wav(output_path, frames=1600)
        return [
            {"text": "First", "offset": 0.0, "duration": 0.1},
            {"text": "Second", "offset": 0.1, "duration": 0.1},
        ]

    monkeypatch.setattr(v2_tts, "synthesize_natural_speech_with_timestamps", flaky)
    monkeypatch.setattr(v2_tts, "translate_lesson_subtitles", lambda lid: {"status": "ready"})

    result = v2_tts.build_reading_tts(lesson["id"])

    assert result["status"] == "ready"
    assert result["sentence_count"] == 2
    assert calls == ["First sentence. Second sentence!"] * 2
    assert db.get_v2_lesson(lesson["id"])["subtitle_status"] == "ready"


def test_build_reading_tts_raises_with_block_context_after_retries(tmp_path, monkeypatch):
    """持续失败按块号上下文报错，且 3 次尝试后不再重试。"""
    import db
    import pytest
    from webapp.services import v2_tts

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(v2_tts, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(v2_tts.time, "sleep", lambda _s: None)
    db.init_db()
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:tts-exhaust",
        title="TTS Exhaust",
        lesson_mode="reading",
    )
    db.replace_v2_reading_blocks(lesson["id"], [{"index": 1, "text": "Only sentence."}])

    calls = []

    def always_fail(text, output_path):
        calls.append(text)
        raise RuntimeError("No audio was received")

    monkeypatch.setattr(v2_tts, "synthesize_natural_speech_with_timestamps", always_fail)

    with pytest.raises(RuntimeError, match=r"block 0 failed after 3 attempts"):
        v2_tts.build_reading_tts(lesson["id"])
    assert calls == ["Only sentence."] * 3


def test_align_sentences_to_boundaries_maps_word_offsets():
    from webapp.services import v2_tts

    spans = v2_tts.align_sentences_to_boundaries(
        ["First sentence.", "Second one."],
        [
            {"text": "First", "offset": 0.0, "duration": 0.3},
            {"text": "sentence", "offset": 0.3, "duration": 0.5},
            {"text": "Second", "offset": 1.0, "duration": 0.3},
            {"text": "one", "offset": 1.3, "duration": 0.4},
        ],
        2.0,
    )

    assert spans[0] == {"start": 0.0, "end": 1.0}
    assert spans[1]["start"] == 1.0
    assert abs(spans[1]["end"] - 1.7) < 1e-9


def test_align_sentences_to_boundaries_falls_back_proportionally():
    from webapp.services import v2_tts

    spans = v2_tts.align_sentences_to_boundaries(
        ["AAAA", "BB"],
        [{"text": "mismatch", "offset": 0.0, "duration": 1.0}],
        3.0,
    )

    assert spans[0] == {"start": 0.0, "end": 2.0}
    assert spans[1] == {"start": 2.0, "end": 3.0}

    empty = v2_tts.align_sentences_to_boundaries(["AAAA", "BB"], [], 3.0)
    assert empty == spans


def test_build_reading_tts_synthesizes_once_per_block(tmp_path, monkeypatch):
    """每个阅读块一次 edge_tts 调用：多块课程的调用数 = 块数，不再是句数。"""
    import db
    from webapp.services import v2_tts

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(v2_tts, "OUTPUT_DIR", tmp_path / "output")
    db.init_db()
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:tts-blocks",
        title="TTS Blocks",
        lesson_mode="reading",
    )
    db.replace_v2_reading_blocks(lesson["id"], [
        {"index": 1, "text": "Alpha one."},
        {"index": 2, "text": "Beta two."},
    ])

    calls = []

    def fake(text, output_path):
        calls.append(text)
        _make_wav(output_path)  # 0.1s @ 8kHz
        return [{"text": text.split()[0], "offset": 0.0, "duration": 0.05}]

    monkeypatch.setattr(v2_tts, "synthesize_natural_speech_with_timestamps", fake)
    monkeypatch.setattr(v2_tts, "translate_lesson_subtitles", lambda lid: {"status": "ready"})

    result = v2_tts.build_reading_tts(lesson["id"])

    assert result["status"] == "ready"
    assert sorted(calls) == ["Alpha one.", "Beta two."]  # 并发合成，调用顺序不定
    segments = db.get_v2_subtitle_segments(lesson["id"])
    assert [(segment["start"], segment["end"]) for segment in segments] == [
        (0.0, 0.05),
        (0.1, 0.15000000000000002),
    ]


def test_cancel_reading_tts_restores_no_audio_state(tmp_path, monkeypatch):
    """生成中取消：撤销未开始的块，课程回到未申请 TTS 状态（无错误、无精听入口）。"""
    import db
    from webapp.services import v2_lessons, v2_tts

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(v2_tts, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(v2_tts, "_TTS_WORKERS", 1)
    db.init_db()
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:tts-cancel",
        title="TTS Cancel",
        lesson_mode="reading",
    )
    db.replace_v2_reading_blocks(lesson["id"], [
        {"index": 1, "text": "Alpha one."},
        {"index": 2, "text": "Beta two."},
        {"index": 3, "text": "Gamma three."},
    ])
    monkeypatch.setattr(db, "spawn_with_db_context", lambda *a, **k: None)
    assert v2_tts.enqueue_reading_tts(lesson["id"]) is True

    calls = []

    def fake(text, output_path):
        calls.append(text)
        _make_wav(output_path)
        assert v2_tts.cancel_reading_tts(lesson["id"]) is True  # 首块合成后用户取消
        return [{"text": text.split()[0], "offset": 0.0, "duration": 0.05}]

    monkeypatch.setattr(v2_tts, "synthesize_natural_speech_with_timestamps", fake)
    monkeypatch.setattr(v2_tts, "translate_lesson_subtitles", lambda lid: {"status": "ready"})

    v2_tts._run_reading_tts(lesson["id"])

    assert calls == ["Alpha one."]  # 取消后未开始的块被撤销
    updated = db.get_v2_lesson(lesson["id"])
    assert updated["subtitle_status"] == ""
    assert not updated["subtitle_error"]
    assert updated["media_kind"] == ""
    assert v2_lessons.get_lesson_capabilities(updated)["can_listen"] is False


def test_cancel_reading_tts_without_active_job_leaves_no_flag(tmp_path, monkeypatch):
    """无进行中任务时取消是 no-op 且不留标记：随后重新入队能正常合成。"""
    import db
    from webapp.services import v2_tts

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(v2_tts, "OUTPUT_DIR", tmp_path / "output")
    db.init_db()
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:tts-cancel-noop",
        title="TTS Cancel Noop",
        lesson_mode="reading",
    )
    db.replace_v2_reading_blocks(lesson["id"], [{"index": 1, "text": "Only block."}])

    assert v2_tts.cancel_reading_tts(lesson["id"]) is False

    monkeypatch.setattr(db, "spawn_with_db_context", lambda *a, **k: None)
    assert v2_tts.enqueue_reading_tts(lesson["id"]) is True

    def fake(text, output_path):
        _make_wav(output_path)
        return [{"text": "Only", "offset": 0.0, "duration": 0.05}]

    monkeypatch.setattr(v2_tts, "synthesize_natural_speech_with_timestamps", fake)
    monkeypatch.setattr(v2_tts, "translate_lesson_subtitles", lambda lid: {"status": "ready"})

    v2_tts._run_reading_tts(lesson["id"])

    updated = db.get_v2_lesson(lesson["id"])
    assert updated["subtitle_status"] == "ready"
    assert updated["media_kind"] == "generated_audio"


def test_reading_tts_cancel_route(tmp_path, monkeypatch):
    import db
    from fastapi.testclient import TestClient
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:tts-cancel-route",
        title="TTS Cancel Route",
        lesson_mode="reading",
    )

    response = client.post(f"/api/v2/lessons/{lesson['id']}/reading/tts/cancel")

    assert response.status_code == 200
    assert response.json() == {
        "lesson_id": lesson["id"],
        "cancel_requested": True,
        "was_running": False,
    }
    assert client.post("/api/v2/lessons/99999/reading/tts/cancel").status_code == 404


def test_run_reading_tts_failure_clears_generated_audio(tmp_path, monkeypatch):
    """合成失败撤回 generated_audio 声明：课程回到不可精听，不残留误导入口。"""
    import db
    from webapp.services import v2_lessons, v2_tts

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    lesson = db.create_v2_lesson(
        source_type="reading_upload",
        source_url="upload:failing-tts",
        title="Failing TTS",
        lesson_mode="reading",
        media_kind="generated_audio",
    )

    def boom(_lesson_id):
        raise RuntimeError("sapi unavailable")

    monkeypatch.setattr(v2_tts, "build_reading_tts", boom)

    v2_tts._run_reading_tts(lesson["id"])

    updated = db.get_v2_lesson(lesson["id"])
    assert updated["subtitle_status"] == "failed"
    assert "sapi unavailable" in updated["subtitle_error"]
    assert updated["media_kind"] == ""
    assert v2_lessons.get_lesson_capabilities(updated)["can_listen"] is False
