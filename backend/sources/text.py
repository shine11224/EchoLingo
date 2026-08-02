from __future__ import annotations

import re

from schemas import Segment, SourceBundle


def build_text_lesson(file_path: str, title: str = "") -> SourceBundle:
    """Parse MD or TXT file into segments for Pipeline (no audio, Phase A skipped)."""
    with open(file_path, encoding="utf-8") as f:
        content = f.read()

    # Strip markdown syntax (headers, bold, italic, inline code)
    content = re.sub(r"#{1,6}\s+", "", content)
    content = re.sub(r"\*\*(.+?)\*\*", r"\1", content)
    content = re.sub(r"\*(.+?)\*", r"\1", content)
    content = re.sub(r"`(.+?)`", r"\1", content)
    content = re.sub(r"!\[.*?\]\(.*?\)", "", content)   # images
    content = re.sub(r"\[(.+?)\]\(.*?\)", r"\1", content)  # links

    # Split into sentences at sentence boundaries
    raw = re.split(r"(?<=[.!?])\s+", content.strip())
    segments: list[Segment] = []
    idx = 0
    for text in raw:
        text = text.strip()
        if len(text) > 10:  # skip very short fragments
            segments.append(Segment(index=idx, text=text, start=0.0, end=0.0))
            idx += 1

    derived_title = title or _derive_title(file_path)
    return SourceBundle(
        source_type="text",
        title=derived_title,
        source_value=file_path,
        segments=segments,
    )


def _derive_title(file_path: str) -> str:
    """Derive a human-readable title from the file path."""
    from pathlib import Path
    stem = Path(file_path).stem
    # Replace hyphens/underscores with spaces, title-case
    return stem.replace("-", " ").replace("_", " ").title() or "Text Lesson"
