import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from fastapi.testclient import TestClient


def test_translate_lesson_builds_sentence_units_and_caches_translations(tmp_path, monkeypatch):
    import db
    from webapp.services import v2_translation

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    lesson = db.create_v2_lesson(
        source_type="local_audio",
        source_url="C:/media/translation.mp3",
        title="Translation",
        duration=12,
    )
    db.configure_v2_lesson_translation(lesson["id"], requested=True)
    db.replace_v2_subtitle_segments(
        lesson["id"],
        [
            {"index": 0, "start": 0.0, "end": 4.0, "text": "Hello"},
            {"index": 1, "start": 4.0, "end": 8.0, "text": "world."},
            {"index": 2, "start": 8.0, "end": 12.0, "text": "Another complete sentence."},
        ],
    )
    monkeypatch.setattr(v2_translation, "hy_ready", lambda: True)
    monkeypatch.setattr(v2_translation, "hy_translate", lambda text: f"中:{text}")

    result = v2_translation.translate_lesson_subtitles(lesson["id"])

    assert result == {"status": "ready", "done": 2, "total": 2}
    assert db.get_v2_sentence("Hello world.")["translation"] == "中:Hello world."
    assert db.get_v2_sentence("Another complete sentence.")["translation"] == "中:Another complete sentence."
    saved = db.get_v2_lesson(lesson["id"])
    assert saved["translation_status"] == "ready"
    assert saved["translation_done"] == 2
    assert saved["translation_total"] == 2
    assert saved["translation_buffer_seconds"] == 12.0
    assert saved["translation_ready"] == 1


def test_playback_units_split_internal_punctuation_before_merging_fragments():
    from webapp.services.v2_translation import build_translation_units

    units = build_translation_units([
        {"index": 1, "start": 0.0, "end": 9.0, "text": "Part 4. You will hear part"},
        {"index": 2, "start": 9.0, "end": 18.0, "text": "of a talk. First, read question 31"},
        {"index": 3, "start": 18.0, "end": 20.0, "text": "to 40."},
    ])

    assert [unit["text"] for unit in units] == [
        "Part 4.",
        "You will hear part of a talk.",
        "First, read question 31 to 40.",
    ]
    assert units[0]["end"] == units[1]["start"]
    assert units[1]["end"] == units[2]["start"]
    assert units[-1]["end"] == 20.0


def test_translate_lesson_reuses_complete_cache_without_starting_hy_mt(tmp_path, monkeypatch):
    import db
    from webapp.services import v2_translation

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    lesson = db.create_v2_lesson("local_audio", "C:/media/cached-translation.mp3")
    db.configure_v2_lesson_translation(lesson["id"], requested=True)
    db.replace_v2_subtitle_segments(
        lesson["id"],
        [{"index": 0, "start": 0.0, "end": 4.0, "text": "Already translated."}],
    )
    db.upsert_v2_sentence("Already translated.", translation="已经翻译。")
    monkeypatch.setattr(
        v2_translation,
        "hy_ready",
        lambda: (_ for _ in ()).throw(AssertionError("Hy-MT should not start")),
    )

    result = v2_translation.translate_lesson_subtitles(lesson["id"])

    assert result == {"status": "ready", "done": 1, "total": 1}
    saved = db.get_v2_lesson(lesson["id"])
    assert saved["translation_status"] == "ready"
    assert saved["translation_ready"] == 1


def test_playback_units_do_not_split_a_long_sentence_at_the_old_soft_limit():
    from webapp.services.v2_translation import build_translation_units

    units = build_translation_units([
        {
            "index": 1,
            "start": 0.0,
            "end": 4.0,
            "text": "One two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen",
        },
        {
            "index": 2,
            "start": 4.0,
            "end": 8.0,
            "text": "sixteen seventeen eighteen nineteen twenty twenty-one twenty-two twenty-three twenty-four twenty-five twenty-six twenty-seven twenty-eight twenty-nine roll the",
        },
        {"index": 3, "start": 8.0, "end": 9.0, "text": "dice."},
    ])

    assert [unit["text"] for unit in units] == [
        (
            "One two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
            "sixteen seventeen eighteen nineteen twenty twenty-one twenty-two twenty-three twenty-four "
            "twenty-five twenty-six twenty-seven twenty-eight twenty-nine roll the dice."
        )
    ]


def test_playback_units_wait_for_sentence_end_in_the_next_cue_after_word_limit():
    from webapp.services.v2_translation import build_translation_units

    units = build_translation_units([
        {"index": 1003, "start": 0.0, "end": 1.0, "text": "Now, if you want me to go"},
        {"index": 1004, "start": 1.0, "end": 2.0, "text": "into this in a little bit more detail"},
        {"index": 1005, "start": 2.0, "end": 3.0, "text": "and how you can [clears throat] build an"},
        {"index": 1006, "start": 3.0, "end": 4.0, "text": "entire system that follows these"},
        {"index": 1007, "start": 4.0, "end": 5.0, "text": "principles, then you might want to check"},
        {"index": 1008, "start": 5.0, "end": 6.0, "text": "out this video here where I go through"},
        {"index": 1009, "start": 6.0, "end": 7.0, "text": "my process of how I think about building"},
        {"index": 1010, "start": 7.0, "end": 8.0, "text": "a learning system."},
        {"index": 1011, "start": 8.0, "end": 9.0, "text": "Otherwise, thanks for watching."},
    ])

    assert [unit["text"] for unit in units] == [
        (
            "Now, if you want me to go into this in a little bit more detail and how you can "
            "[clears throat] build an entire system that follows these principles, then you might "
            "want to check out this video here where I go through my process of how I think about "
            "building a learning system."
        ),
        "Otherwise, thanks for watching.",
    ]
    assert units[0]["segment_ids"] == list(range(1003, 1011))
    assert units[0]["end"] == units[1]["start"]


def test_playback_units_hard_cap_splits_one_oversized_chunk_without_punctuation():
    from webapp.services.v2_translation import MAX_TRANSLATION_UNIT_WORDS, _WORD_RE, build_translation_units

    tokens = [f"word{i:03d}" for i in range(100)]
    units = build_translation_units([
        {"index": 1, "start": 0.0, "end": 50.0, "text": " ".join(tokens)},
    ])

    assert len(units) == 3
    for unit in units:
        assert len(_WORD_RE.findall(unit["text"])) <= MAX_TRANSLATION_UNIT_WORDS
    # 均衡切分，不出现可均衡时的无意义小尾巴
    assert min(len(_WORD_RE.findall(unit["text"])) for unit in units) >= 30
    # 文本无丢失无重复
    assert [token for unit in units for token in unit["text"].split()] == tokens
    # 时间戳单调且连续
    assert units[0]["start"] == 0.0
    assert units[-1]["end"] == 50.0
    for prev, curr in zip(units, units[1:]):
        assert prev["start"] <= prev["end"]
        assert curr["start"] == prev["end"]


def test_playback_units_hard_cap_avoids_tiny_tail_when_balanced_split_is_possible():
    from webapp.services.v2_translation import MAX_TRANSLATION_UNIT_WORDS, _WORD_RE, build_translation_units

    tokens = [f"word{i:03d}" for i in range(MAX_TRANSLATION_UNIT_WORDS + 1)]
    units = build_translation_units([
        {"index": 1, "start": 10.0, "end": 20.0, "text": " ".join(tokens)},
    ])

    sizes = [len(_WORD_RE.findall(unit["text"])) for unit in units]
    assert len(units) == 2
    assert all(size <= MAX_TRANSLATION_UNIT_WORDS for size in sizes)
    assert min(sizes) >= 20
    assert [token for unit in units for token in unit["text"].split()] == tokens
    assert units[0]["start"] == 10.0
    assert units[1]["start"] == units[0]["end"]
    assert units[1]["end"] == 20.0


def test_playback_units_hard_cap_splits_oversized_chunk_among_mixed_chunks():
    from webapp.services.v2_translation import MAX_TRANSLATION_UNIT_WORDS, _WORD_RE, build_translation_units

    oversized = [f"word{i:03d}" for i in range(80)]
    segments = [
        {"index": 1, "start": 0.0, "end": 2.0, "text": "Hello world."},
        {"index": 2, "start": 2.0, "end": 40.0, "text": " ".join(oversized)},
        {"index": 3, "start": 40.0, "end": 42.0, "text": "Short bridge"},
        {"index": 4, "start": 42.0, "end": 46.0, "text": "Final sentence here."},
    ]
    units = build_translation_units(segments)

    assert [unit["text"] for unit in units] == [
        "Hello world.",
        " ".join(oversized[:40]),
        " ".join(oversized[40:]),
        "Short bridge Final sentence here.",
    ]
    for unit in units:
        assert len(_WORD_RE.findall(unit["text"])) <= MAX_TRANSLATION_UNIT_WORDS
    # 顺序与覆盖：全部 token 依次出现且无重复
    expected_tokens = [token for segment in segments for token in segment["text"].split()]
    assert [token for unit in units for token in unit["text"].split()] == expected_tokens
    # 时间轴单调
    for prev, curr in zip(units, units[1:]):
        assert prev["start"] <= prev["end"]
        assert curr["start"] >= prev["start"]
        assert curr["end"] >= prev["end"]
    assert units[0]["start"] == 0.0
    assert units[-1]["end"] == 46.0


def test_translate_lesson_fails_without_hy_mt_but_keeps_subtitles(tmp_path, monkeypatch):
    import db
    from webapp.services import v2_translation

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    lesson = db.create_v2_lesson("local_audio", "C:/media/no-model.mp3")
    db.configure_v2_lesson_translation(lesson["id"], requested=True)
    db.replace_v2_subtitle_segments(
        lesson["id"],
        [{"index": 0, "start": 0.0, "end": 4.0, "text": "Hello world."}],
    )
    db.set_v2_lesson_status(lesson["id"], subtitle_status="ready")
    monkeypatch.setattr(v2_translation, "hy_ready", lambda: False)

    result = v2_translation.translate_lesson_subtitles(lesson["id"])

    assert result["status"] == "failed"
    saved = db.get_v2_lesson(lesson["id"])
    assert saved["subtitle_status"] == "ready"
    assert saved["translation_status"] == "failed"
    assert "Hy-MT" in saved["translation_error"]


def test_sentence_translation_api_returns_playback_sentence_units(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson("local_audio", "C:/media/api-translation.mp3")
    db.replace_v2_subtitle_segments(
        lesson["id"],
        [
            {"index": 0, "start": 0.0, "end": 2.0, "text": "Hello"},
            {"index": 1, "start": 2.0, "end": 4.0, "text": "world."},
        ],
    )
    db.upsert_v2_sentence("Hello world.", translation="你好，世界。")

    response = client.get(f"/api/v2/lessons/{lesson['id']}/sentence-translations")

    assert response.status_code == 200
    assert response.json()["translations"] == {"Hello world.": "你好，世界。"}
    assert response.json()["cached"] is True
    assert response.json()["translation_status"] == "ready"

    subtitles = client.get(f"/api/v2/lessons/{lesson['id']}/subtitles")
    assert subtitles.status_code == 200
    assert [item["text"] for item in subtitles.json()["sentence_units"]] == ["Hello world."]


def test_reading_selection_translation_uses_hy_mt_and_reuses_cache(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.services import hy_translate

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    calls = []
    monkeypatch.setattr(hy_translate, "is_ready", lambda: True)
    monkeypatch.setattr(
        hy_translate,
        "translate",
        lambda text: calls.append(text) or f"混元:{text}",
    )
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=translate123",
        video_id="translate123",
        title="Selection translation",
    )

    first = client.post(
        f"/api/v2/lessons/{lesson['id']}/translate-selection",
        json={"text": "  A selected\n sentence.  "},
    )
    second = client.post(
        f"/api/v2/lessons/{lesson['id']}/translate-selection",
        json={"text": "A selected sentence."},
    )

    assert first.status_code == 200
    assert first.json() == {
        "translation": "混元:A selected sentence.",
        "engine": "hy-mt",
    }
    # Task 8：缓存命中额外携带零消耗计费标记（首次翻译无该字段）
    assert second.json() == {**first.json(), "credits": {"charged": 0, "cached": True}}
    assert calls == ["A selected sentence."]


def test_translation_endpoints_do_not_fallback_when_hy_mt_is_unavailable(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.services import hy_translate

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(hy_translate, "is_ready", lambda: False)
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:hy-mt-only",
        title="Hy-MT only",
        lesson_mode="reading",
    )

    selection = client.post(
        f"/api/v2/lessons/{lesson['id']}/translate-selection",
        json={"text": "Translate this."},
    )
    sentences = client.post(
        f"/api/v2/lessons/{lesson['id']}/translate-sentences",
        json={"sentences": ["Translate this too."]},
    )

    assert selection.status_code == 503
    assert sentences.status_code == 503
    assert "混元翻译引擎未就绪" in selection.json()["detail"]


def test_translate_reading_blocks_translates_from_blocks_without_segments(tmp_path, monkeypatch):
    """Reading 翻译以阅读块句子为源：无字幕段也能跑，乱码句跳过，缓存按文本键。"""
    import db
    from webapp.services import v2_translation

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    lesson = db.create_v2_lesson(
        source_type="reading_upload",
        source_url="upload:reading-translate",
        title="Reading Translate",
        lesson_mode="reading",
        media_kind="generated_audio",
    )
    db.replace_v2_reading_blocks(lesson["id"], [
        {"index": 1, "text": "First real sentence. "},
        {"index": 2, "text": "Second real sentence."},
    ])
    db.configure_v2_lesson_translation(lesson["id"], requested=True)
    monkeypatch.setattr(v2_translation, "hy_ready", lambda: True)
    monkeypatch.setattr(v2_translation, "hy_translate", lambda text: f"中:{text}")

    result = v2_translation.translate_reading_blocks(lesson["id"])

    assert result == {"status": "ready", "done": 2, "total": 2}
    assert db.get_v2_sentence("First real sentence.")["translation"] == "中:First real sentence."
    assert db.get_v2_sentence("Second real sentence.")["translation"] == "中:Second real sentence."
    saved = db.get_v2_lesson(lesson["id"])
    assert saved["translation_status"] == "ready"
    assert saved["translation_done"] == 2


def test_translate_reading_blocks_marks_failure_with_error(tmp_path, monkeypatch):
    import db
    from webapp.services import v2_translation

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:reading-translate-fail",
        title="Reading Translate Fail",
        lesson_mode="reading",
    )
    db.replace_v2_reading_blocks(lesson["id"], [{"index": 1, "text": "Some sentence."}])
    monkeypatch.setattr(v2_translation, "hy_ready", lambda: True)

    def boom(text):
        raise RuntimeError("hy-mt down")

    monkeypatch.setattr(v2_translation, "hy_translate", boom)

    result = v2_translation.translate_reading_blocks(lesson["id"])

    assert result["status"] == "failed"
    saved = db.get_v2_lesson(lesson["id"])
    assert saved["translation_status"] == "failed"
    assert "hy-mt down" in saved["translation_error"]
