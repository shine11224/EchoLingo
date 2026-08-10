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
        "translate_reading_blocks",
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
    assert translated == []  # 翻译与 TTS 解耦：build 不再触发翻译
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
    monkeypatch.setattr(v2_tts, "translate_reading_blocks", lambda lid: {"status": "ready"})

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
    monkeypatch.setattr(v2_tts, "translate_reading_blocks", lambda lid: {"status": "ready"})

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
    monkeypatch.setattr(v2_tts, "translate_reading_blocks", lambda lid: {"status": "ready"})

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
    monkeypatch.setattr(v2_tts, "translate_reading_blocks", lambda lid: {"status": "ready"})

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


def test_recover_stuck_reading_tts_requeues_only_stuck_lessons(tmp_path, monkeypatch):
    """重启恢复：只对「声明了生成音频 + 成品未落盘 + 仍 pending」的课程重新合成。"""
    import db
    from webapp.services import v2_tts

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    stuck = db.create_v2_lesson(
        source_type="reading_upload", source_url="upload:stuck", title="Stuck",
        lesson_mode="reading", media_kind="generated_audio",
    )
    db.set_v2_lesson_status(stuck["id"], subtitle_status="pending")
    ready = db.create_v2_lesson(
        source_type="reading_upload", source_url="upload:ready", title="Ready",
        lesson_mode="reading", media_kind="generated_audio",
        media_url="/output/v2_assets/x/reading.wav",
    )
    db.set_v2_lesson_status(ready["id"], subtitle_status="ready")
    db.create_v2_lesson(
        source_type="reading_text", source_url="manual:plain", title="Plain", lesson_mode="reading",
    )

    spawned = []
    monkeypatch.setattr(db, "spawn_with_db_context", lambda fn, *a, **k: spawned.append((fn, a)))

    assert v2_tts.recover_stuck_reading_tts() == 1
    assert len(spawned) == 1
    fn, args = spawned[0]
    assert fn is v2_tts._run_reading_tts
    assert args[0] == stuck["id"]


def test_build_reading_tts_skips_unspeakable_garbage_blocks(tmp_path, monkeypatch):
    """纯乱码块（如 ''）跳过；混合乱码句先剥  字符再合成，不丢句子内容。"""
    import db
    from webapp.services import v2_tts

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(v2_tts, "OUTPUT_DIR", tmp_path / "output")
    db.init_db()
    lesson = db.create_v2_lesson(
        source_type="reading_upload",
        source_url="upload:tts-garbage",
        title="TTS Garbage",
        lesson_mode="reading",
    )
    db.replace_v2_reading_blocks(lesson["id"], [
        {"index": 1, "text": "The  result holds."},
        {"index": 2, "text": ""},
        {"index": 3, "text": "Real sentence two."},
    ])

    calls = []

    def fake(text, output_path):
        calls.append(text)
        _make_wav(output_path)
        return [{"text": text.split()[0], "offset": 0.0, "duration": 0.05}]

    monkeypatch.setattr(v2_tts, "synthesize_natural_speech_with_timestamps", fake)
    monkeypatch.setattr(v2_tts, "translate_reading_blocks", lambda lid: {"status": "ready"})

    result = v2_tts.build_reading_tts(lesson["id"])

    assert result["status"] == "ready"
    assert sorted(calls) == ["Real sentence two.", "The result holds."]
    segments = db.get_v2_subtitle_segments(lesson["id"])
    assert [segment["text"] for segment in segments] == ["The result holds.", "Real sentence two."]
    timed_blocks = db.get_v2_reading_blocks(lesson["id"])
    assert timed_blocks[1]["start_seconds"] is None  # 纯乱码块不占时间轴
    assert timed_blocks[1]["sentences"] == []
    assert timed_blocks[2]["sentences"][0]["text"] == "Real sentence two."


def test_set_v2_lesson_status_clears_stale_error_on_pending(tmp_path, monkeypatch):
    """重新入队/重试时必须清掉旧的失败错误，否则旧错误会被误读为新失败。"""
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:status-clear",
        title="Status Clear",
        lesson_mode="reading",
    )
    db.set_v2_lesson_status(lesson["id"], subtitle_status="failed", subtitle_error="old error")
    assert db.get_v2_lesson(lesson["id"])["subtitle_error"] == "old error"

    db.set_v2_lesson_status(lesson["id"], subtitle_status="pending")
    updated = db.get_v2_lesson(lesson["id"])
    assert updated["subtitle_status"] == "pending"
    assert updated["subtitle_error"] == ""


def test_run_reading_tts_failure_stores_repr_for_empty_message(tmp_path, monkeypatch):
    """空消息异常（如 TimeoutError）落库为 repr，不再出现 failed 却无错误文本。"""
    import db
    from webapp.services import v2_tts

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    lesson = db.create_v2_lesson(
        source_type="reading_upload",
        source_url="upload:empty-exc",
        title="Empty Exc",
        lesson_mode="reading",
        media_kind="generated_audio",
    )

    def boom(_lesson_id):
        raise TimeoutError()

    monkeypatch.setattr(v2_tts, "build_reading_tts", boom)

    v2_tts._run_reading_tts(lesson["id"])

    updated = db.get_v2_lesson(lesson["id"])
    assert updated["subtitle_status"] == "failed"
    assert "TimeoutError" in updated["subtitle_error"]


def test_enqueue_reading_tts_spawns_translation_in_parallel(tmp_path, monkeypatch):
    """翻译只依赖文本：与 TTS 同时各起一个后台任务，不再等音频完成。"""
    import db
    from webapp.services import v2_tts

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:tts-parallel",
        title="TTS Parallel",
        lesson_mode="reading",
    )
    spawned = []
    monkeypatch.setattr(db, "spawn_with_db_context", lambda fn, *a, **k: spawned.append(fn))

    assert v2_tts.enqueue_reading_tts(lesson["id"]) is True

    assert spawned == [v2_tts._run_reading_tts, v2_tts.translate_reading_blocks]


def test_sentence_translations_falls_back_to_reading_blocks(tmp_path, monkeypatch):
    """Reading 课 TTS 未完成（无字幕段）时，翻译路由从阅读块取句返回已缓存译文。"""
    import db
    from fastapi.testclient import TestClient
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="reading_upload",
        source_url="upload:trans-fallback",
        title="Translation Fallback",
        lesson_mode="reading",
        media_kind="generated_audio",
    )
    db.replace_v2_reading_blocks(lesson["id"], [
        {"index": 1, "text": "First sentence here. Second sentence here."},
    ])
    db.upsert_v2_sentence("First sentence here.", translation="第一句。")

    response = client.get(f"/api/v2/lessons/{lesson['id']}/sentence-translations")

    assert response.status_code == 200
    assert response.json()["translations"] == {"First sentence here.": "第一句。"}


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
