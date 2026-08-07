"""词级时间戳切句：paraformer words 精确断句 + 数据库往返。"""
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import db
from webapp.services.v2_translation import _split_source_segments

# 模拟 paraformer chunk：真实词时间（秒），语速不按字符均匀分布
_SEGMENT_WITH_WORDS = {
    "index": 1,
    "start": 4.44,
    "end": 12.0,
    "text": "Hi there. Welcome to the course. It is great.",
    "words": [
        {"text": "Hi ", "start": 4.44, "end": 4.9, "punctuation": ""},
        {"text": "there", "start": 4.9, "end": 5.3, "punctuation": ". "},
        {"text": "Welcome ", "start": 5.35, "end": 5.8, "punctuation": ""},
        {"text": "to ", "start": 5.8, "end": 6.0, "punctuation": ""},
        {"text": "the ", "start": 6.0, "end": 6.2, "punctuation": ""},
        {"text": "course", "start": 6.2, "end": 8.9, "punctuation": ". "},
        {"text": "It ", "start": 8.95, "end": 9.2, "punctuation": ""},
        {"text": "is ", "start": 9.2, "end": 9.5, "punctuation": ""},
        {"text": "great", "start": 9.5, "end": 11.9, "punctuation": ". "},
    ],
}


def test_word_split_uses_real_timestamps():
    pieces = _split_source_segments([_SEGMENT_WITH_WORDS])
    assert len(pieces) == 3
    assert pieces[0]["text"] == "Hi there."
    assert pieces[0]["start"] == pytest.approx(4.44)
    assert pieces[0]["end"] == pytest.approx(5.3)   # 真实词尾，非插值
    assert pieces[1]["text"] == "Welcome to the course."
    assert pieces[1]["start"] == pytest.approx(5.35)
    assert pieces[1]["end"] == pytest.approx(8.9)   # 长词 course 占 2.7s
    assert pieces[2]["text"] == "It is great."
    assert pieces[2]["end"] == pytest.approx(11.9)


def test_no_words_falls_back_to_interpolation():
    segment = {k: v for k, v in _SEGMENT_WITH_WORDS.items() if k != "words"}
    pieces = _split_source_segments([segment])
    assert len(pieces) == 3
    # 插值结果与词级结果不同（证明走了兜底）
    assert pieces[0]["end"] != pytest.approx(5.3)


def test_words_db_roundtrip(tmp_path):
    user_db = tmp_path / "users" / "t" / "vocab.db"
    token = db.set_current_db_path(user_db)
    try:
        lesson_id = db.create_v2_lesson(source_type="bilibili", source_url="u", title="t")
        db.replace_v2_subtitle_segments(1, [_SEGMENT_WITH_WORDS])
        segs = db.get_v2_subtitle_segments(1)
        assert len(segs) == 1
        assert segs[0]["words"] == _SEGMENT_WITH_WORDS["words"]
        # 无 words 的段读出时不带 words 键
        db.replace_v2_subtitle_segments(1, [{"index": 1, "start": 0.0, "end": 1.0, "text": "no words here"}])
        assert "words" not in db.get_v2_subtitle_segments(1)[0]
    finally:
        db.reset_current_db_path(token)


def test_transcript_cache_preserves_words(tmp_path, monkeypatch):
    """缓存命中不得丢词级时间戳（2026-08-07 云端吞句根因回归）。"""
    from schemas import Segment
    from sources import transcript_cache

    monkeypatch.setattr(transcript_cache, "TRANSCRIPT_CACHE_DIR", tmp_path / "cache")
    media = tmp_path / "media.m4a"
    media.write_bytes(b"fake-audio")
    source = [Segment(
        index=int(_SEGMENT_WITH_WORDS["index"]),
        text=str(_SEGMENT_WITH_WORDS["text"]),
        start=float(_SEGMENT_WITH_WORDS["start"]),
        end=float(_SEGMENT_WITH_WORDS["end"]),
        words=list(_SEGMENT_WITH_WORDS["words"]),
    )]
    transcript_cache.save_transcript_cache(media, "paraformer", source)
    loaded = transcript_cache.load_transcript_cache(media, "paraformer")
    assert loaded is not None
    assert loaded[0].words == _SEGMENT_WITH_WORDS["words"]
