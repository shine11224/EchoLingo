from __future__ import annotations

import json
import re
from pathlib import Path

from schemas import Segment


def _save_debug(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_subtitle_file(path: Path, debug_dir: Path | None = None) -> list[Segment]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    # raw_cues keeps per-line structure for line-level dedup
    raw_cues: list[tuple[float, float, list[str]]] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if "-->" not in line:
            index += 1
            continue
        start_raw, end_raw = [item.strip() for item in line.split("-->")]
        start = _parse_timestamp(start_raw)
        end = _parse_timestamp(end_raw.split(" ")[0])
        index += 1
        payload: list[str] = []
        while index < len(lines):
            raw_line = lines[index].strip()
            if not raw_line:
                if payload:
                    break
                index += 1
                continue
            if "-->" in raw_line:
                break
            cue_line = re.sub(r"<[^>]+>", "", raw_line).strip()
            cue_line = re.sub(r"&nbsp;", " ", cue_line)
            if cue_line and not cue_line.startswith("NOTE"):
                payload.append(cue_line)
            index += 1
        if payload:
            raw_cues.append((start, end, payload))
        if index < len(lines) and not lines[index].strip():
            index += 1

    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)
        _save_debug(debug_dir / "01_raw_cues.json",
                    [{"start": s, "end": e, "lines": p} for s, e, p in raw_cues])

    cues = _dedup_cue_lines(raw_cues)
    if debug_dir:
        _save_debug(debug_dir / "02_dedup_lines.json",
                    [{"start": s, "end": e, "text": t} for s, e, t in cues])

    cues = _dedup_rolling_cues(cues)
    if debug_dir:
        _save_debug(debug_dir / "03_dedup_rolling.json",
                    [{"start": s, "end": e, "text": t} for s, e, t in cues])

    sentences = _split_cues_on_punctuation(cues)
    if debug_dir:
        _save_debug(debug_dir / "04_split_punctuation.json",
                    [{"i": s.index, "text": s.text, "start": s.start, "end": s.end} for s in sentences])

    return sentences


def _dedup_cue_lines(raw_cues: list[tuple[float, float, list[str]]]) -> list[tuple[float, float, str]]:
    """Remove lines that appeared in the immediately preceding cue.

    YouTube's 2-line scrolling format repeats the current 2nd display line as
    the 2nd line of the *next* cue while the display scrolls.  Comparing lines
    before joining catches exact ghost lines without any word-count heuristics.
    """
    result: list[tuple[float, float, str]] = []
    prev_line_set: set[str] = set()
    for start, end, payload in raw_cues:
        new_lines = [l for l in payload
                     if re.sub(r"\s+", " ", l).strip().lower() not in prev_line_set]
        prev_line_set = {re.sub(r"\s+", " ", l).strip().lower() for l in payload}
        merged = re.sub(r"\s+", " ", " ".join(new_lines)).strip()
        if merged:
            result.append((start, end, merged))
    return result


def _dedup_rolling_cues(cues: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """Handle YouTube caption duplication from two display formats:

    1. Growing-prefix (single-line rolling): cue N+1 starts with cue N → keep only last.
    2. Two-line scrolling: cue N's tail == cue N+1's tail (repeated 2nd display line),
       and cue N's head == cue N+1's head (repeated 1st display line). Strip the ghost.
    """
    if not cues:
        return cues

    # Step 1: collapse growing-prefix sequences
    collapsed: list[tuple[float, float, str]] = []
    i = 0
    while i < len(cues):
        start, end, text = cues[i]
        clean = re.sub(r"\s+", " ", text).strip().lower()
        j = i + 1
        while j < len(cues):
            next_end, next_text = cues[j][1], cues[j][2]
            clean_next = re.sub(r"\s+", " ", next_text).strip().lower()
            if clean_next.startswith(clean):
                end, text, clean = next_end, next_text, clean_next
                j += 1
            else:
                break
        collapsed.append((start, end, text))
        i = j

    # Step 2: strip two-line scrolling artifacts (shared tail OR shared head)
    if len(collapsed) < 2:
        return collapsed

    MIN_MATCH = 3   # minimum shared words to trigger strip
    MIN_REMAIN = 3  # minimum words that must remain after strip

    result: list[tuple[float, float, str]] = [collapsed[0]]
    prev_words = re.findall(r"[A-Za-z0-9']+", collapsed[0][2].lower())

    for cur_start, cur_end, cur_text in collapsed[1:]:
        cur_words = re.findall(r"[A-Za-z0-9']+", cur_text.lower())
        cur_orig = cur_text.split()
        stripped = cur_text

        if len(cur_words) > MIN_REMAIN:
            max_n = min(len(prev_words), len(cur_words) - MIN_REMAIN, 15)
            # Try shared TAIL (repeated 2nd display line)
            for n in range(max_n, MIN_MATCH - 1, -1):
                if prev_words[-n:] == cur_words[-n:]:
                    trimmed = " ".join(cur_orig[:-n]).strip()
                    if len(trimmed.split()) >= MIN_REMAIN:
                        stripped = trimmed
                    break
            # If no tail match, try shared HEAD (repeated 1st display line)
            if stripped == cur_text:
                for n in range(max_n, MIN_MATCH - 1, -1):
                    if prev_words[:n] == cur_words[:n]:
                        trimmed = " ".join(cur_orig[n:]).strip()
                        if len(trimmed.split()) >= MIN_REMAIN:
                            stripped = trimmed
                        break

        if stripped.strip():
            result.append((cur_start, cur_end, stripped))
            prev_words = re.findall(r"[A-Za-z0-9']+", stripped.lower())
        else:
            prev_words = cur_words

    return result


_SENT_END = re.compile(r'[.!?]["\']?\s*$')
_SENT_SPLIT = re.compile(r'(?<=[.!?])\s+')


def _split_cues_on_punctuation(cues: list[tuple[float, float, str]]) -> list[Segment]:
    """Preserve cue boundaries; only split cue text at reliable punctuation."""
    segments: list[Segment] = []
    sentence_index = 1

    for cue_start, cue_end, text in cues:
        phrases = [phrase.strip() for phrase in _SENT_SPLIT.split(text.strip()) if phrase.strip()]
        total_chars = sum(max(1, len(phrase)) for phrase in phrases)
        cursor = cue_start
        for phrase in phrases:
            normalized = _normalize_sentence(phrase)
            if not normalized:
                continue
            if phrase == phrases[-1] or cue_end <= cue_start:
                phrase_end = cue_end
            else:
                phrase_end = cursor + (cue_end - cue_start) * len(phrase) / max(total_chars, 1)
            segments.append(Segment(index=sentence_index, text=normalized,
                                    start=cursor, end=phrase_end))
            sentence_index += 1
            cursor = phrase_end
    return segments


def _normalize_sentence(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    parts: list[str] = []
    for token in text.split(" "):
        if not parts or parts[-1].lower() != token.lower():
            parts.append(token)
    return " ".join(parts)
def _parse_timestamp(value: str) -> float:
    hours = 0
    parts = value.replace(",", ".").split(":")
    if len(parts) == 3:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
    else:
        minutes = int(parts[0])
        seconds = float(parts[1])
    return hours * 3600 + minutes * 60 + seconds
