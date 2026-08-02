import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from sources.subtitle_parser import parse_subtitle_file


def test_parser_keeps_first_youtube_cue_when_payload_starts_after_blank_line(tmp_path):
    subtitle = tmp_path / "youtube.en.vtt"
    subtitle.write_text(
        """WEBVTT

00:00:00.000 --> 00:00:01.390 align:start position:0%
 
So,<00:00:00.160><c> I've</c><00:00:00.240><c> been</c><00:00:00.360><c> learning</c>

00:00:01.390 --> 00:00:01.400 align:start position:0%
So, I've been learning
 

00:00:01.400 --> 00:00:03.990 align:start position:0%
So, I've been learning
for<00:00:02.080><c> almost</c><00:00:02.760><c> 15</c><00:00:03.120><c> years.</c>
""",
        encoding="utf-8",
    )

    segments = parse_subtitle_file(subtitle)

    assert segments[0].text == "So, I've been learning"
    assert segments[0].start == 0.0
    assert segments[0].end == 1.39
