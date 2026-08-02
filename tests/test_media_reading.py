import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from webapp.services.media_reading import build_media_reading_blocks


def _segment(index, start, end, text):
    return {"index": index, "start": start, "end": end, "text": text}


def test_groups_adjacent_segments_and_preserves_sentence_anchors():
    blocks = build_media_reading_blocks([
        _segment(0, 0.0, 2.0, "First sentence."),
        _segment(1, 2.3, 4.5, "Second sentence."),
    ])

    assert blocks == [{
        "index": 1,
        "text": "First sentence. Second sentence.",
        "start_seconds": 0.0,
        "end_seconds": 4.5,
        "source_segment_ids": [0, 1],
        "sentences": [
            {
                "sentence_key": 0,
                "segment_index": 0,
                "source_segment_ids": [0],
                "text": "First sentence.",
                "start_seconds": 0.0,
                "end_seconds": 2.0,
            },
            {
                "sentence_key": 1,
                "segment_index": 1,
                "source_segment_ids": [1],
                "text": "Second sentence.",
                "start_seconds": 2.3,
                "end_seconds": 4.5,
            },
        ],
    }]


def test_starts_a_new_paragraph_after_a_long_gap():
    blocks = build_media_reading_blocks([
        _segment(0, 0.0, 2.0, "First sentence."),
        _segment(1, 3.5, 5.0, "New paragraph."),
    ])

    assert [block["source_segment_ids"] for block in blocks] == [[0], [1]]


def test_ignores_blank_segments_without_renumbering_source_ids():
    blocks = build_media_reading_blocks([
        _segment(3, 0.0, 1.0, "  "),
        _segment(4, 1.0, 2.0, "Kept sentence."),
    ])

    assert blocks[0]["source_segment_ids"] == [4]


def test_merges_subtitle_fragments_into_translation_aligned_sentences():
    blocks = build_media_reading_blocks([
        _segment(7, 0.0, 2.0, "A sentence split across"),
        _segment(8, 2.0, 4.0, "subtitle fragments."),
        _segment(9, 4.2, 6.0, "The next sentence."),
    ])

    assert blocks[0]["source_segment_ids"] == [7, 8, 9]
    assert blocks[0]["sentences"] == [
        {
            "sentence_key": 0,
            "segment_index": 7,
            "source_segment_ids": [7, 8],
            "text": "A sentence split across subtitle fragments.",
            "start_seconds": 0.0,
            "end_seconds": 4.0,
        },
        {
            "sentence_key": 1,
            "segment_index": 9,
            "source_segment_ids": [9],
            "text": "The next sentence.",
            "start_seconds": 4.2,
            "end_seconds": 6.0,
        },
    ]
