import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from pathlib import Path
from sources.youtube import extract_video_id, source_bundle_to_segment_dicts
from schemas import Segment, SourceBundle
from webapp.services import v2_lessons
from webapp.services.media_reading import build_media_reading_blocks


def test_extract_video_id_supports_common_youtube_urls():
    assert extract_video_id("https://www.youtube.com/watch?v=abc123def45") == "abc123def45"
    assert extract_video_id("https://youtu.be/abc123def45") == "abc123def45"
    assert extract_video_id("abc123def45") == "abc123def45"


def test_source_bundle_to_segment_dicts():
    bundle = SourceBundle(
        source_type="youtube",
        title="Demo",
        source_value="https://www.youtube.com/watch?v=abc123def45",
        youtube_id="abc123def45",
        segments=[Segment(index=1, start=1.0, end=2.5, text="Hello world.")],
    )
    result = source_bundle_to_segment_dicts(bundle)
    assert result == [{"index": 1, "start": 1.0, "end": 2.5, "text": "Hello world."}]


def test_store_media_segments_persists_subtitles_and_reading_projection(monkeypatch):
    segments = [
        {"index": 0, "start": 0.0, "end": 2.0, "text": "First sentence."},
        {"index": 1, "start": 2.2, "end": 4.0, "text": "Second sentence."},
    ]
    stored_subtitles = []
    stored_reading = []
    monkeypatch.setattr(
        v2_lessons.db,
        "replace_v2_subtitle_segments",
        lambda lesson_id, values: stored_subtitles.append((lesson_id, values)),
    )
    monkeypatch.setattr(
        v2_lessons.db,
        "replace_v2_reading_blocks",
        lambda lesson_id, values: stored_reading.append((lesson_id, values)),
    )

    v2_lessons._store_media_segments(42, segments)

    assert stored_subtitles == [(42, segments)]
    assert stored_reading == [(42, build_media_reading_blocks(segments))]
