"""Course-level Hy-MT subtitle translation with adaptive playback readiness."""
from __future__ import annotations

import re
import time

import db
from analyzer import SentenceAnalyzer
from schemas import Segment
from webapp.services.hy_translate import is_ready as hy_ready
from webapp.services.hy_translate import translate as hy_translate

_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
_SENTENCE_END_RE = re.compile(r"[.!?][\"')\]]?$")
MAX_TRANSLATION_UNIT_WORDS = 48
_SAFE_BUFFER_SECONDS = 120.0
_SAFE_TRANSLATION_RATE = 2.0


_WORD_PUNCT_RE = re.compile(r"[.!?]")


def _split_segment_by_words(segment: dict) -> list[Segment] | None:
    """用词级时间戳按标点边界切句（paraformer words），无词数据返回 None 走插值兜底。"""
    words = segment.get("words") or []
    if not words:
        return None
    source_index = int(segment.get("index", segment.get("segment_index", 0)))
    groups: list[list[dict]] = []
    buf: list[dict] = []
    for w in words:
        buf.append(w)
        if _WORD_PUNCT_RE.search(str(w.get("punctuation") or "")):
            groups.append(buf)
            buf = []
    if buf:
        groups.append(buf)
    out: list[Segment] = []
    for group in groups:
        # punctuation 字段（". "/", "）附着在词后，包含所需空格，一并拼接
        text = "".join(
            str(w.get("text") or "") + str(w.get("punctuation") or "") for w in group
        )
        text = " ".join(text.split())
        if not text:
            continue
        out.append(Segment(
            index=source_index,
            text=text,
            start=float(group[0].get("start") or 0),
            end=float(group[-1].get("end") or group[-1].get("start") or 0),
        ))
    return out or None


def _split_piece_by_capitals(piece: Segment) -> list[Segment]:
    """ASR 丢句末点时按句中大写词补切（专名连用不切），时间轴按字符比例插值。"""
    parts = SentenceAnalyzer._split_capital_boundaries(piece.text)
    if len(parts) <= 1:
        return [piece]
    total = sum(max(1, len(part)) for part in parts)
    start = float(piece.start or 0.0)
    end = float(piece.end or start)
    duration = max(0.0, end - start)
    out: list[Segment] = []
    cursor = start
    for i, part in enumerate(parts):
        piece_end = end if i == len(parts) - 1 else cursor + duration * max(1, len(part)) / total
        out.append(Segment(index=piece.index, text=part, start=cursor, end=piece_end))
        cursor = piece_end
    return out


def _split_source_segments(segments: list[dict]) -> list[dict]:
    """Split strong punctuation inside source chunks before cross-chunk merging."""
    pieces: list[dict] = []
    for fallback_index, segment in enumerate(segments):
        source_index = int(segment.get("index", segment.get("segment_index", fallback_index)))
        source = Segment(
            index=source_index,
            text=str(segment.get("text") or ""),
            start=float(segment.get("start", segment.get("start_seconds", 0)) or 0),
            end=float(segment.get("end", segment.get("end_seconds", 0)) or 0),
        )
        raw_pieces = _split_segment_by_words(segment) or SentenceAnalyzer._split_segment_sentences(source)
        for piece in (sub for p in raw_pieces for sub in _split_piece_by_capitals(p)):
            words = {word.casefold() for word in _WORD_RE.findall(piece.text)}
            highlighted = [
                word for word in segment.get("highlighted_words", [])
                if str(word).casefold() in words
            ]
            meanings = {
                word: meaning
                for word, meaning in (segment.get("word_meanings") or {}).items()
                if str(word).casefold() in words
            }
            pieces.append({
                **segment,
                "index": source_index,
                "text": piece.text,
                "start": float(piece.start or 0),
                "end": float(piece.end or piece.start or 0),
                "highlighted_words": highlighted,
                "word_meanings": meanings,
            })
    return pieces


def _split_oversized_unit(unit: dict) -> list[dict]:
    """无句末标点的超限 unit 按词界均衡硬切，时间戳按词位比例单调插值。"""
    text = unit["text"]
    if _SENTENCE_END_RE.search(text.strip()):
        return [unit]
    tokens = text.split()
    counts = [len(_WORD_RE.findall(token)) for token in tokens]
    total = sum(counts)
    if total <= MAX_TRANSLATION_UNIT_WORDS:
        return [unit]
    start = float(unit["start"])
    end = float(unit["end"])
    duration = max(0.0, end - start)

    # 按词索引均分 n 段；多词 token 跨界可能让某段超 48，此时增加段数重试
    n = max(2, -(-total // MAX_TRANSLATION_UNIT_WORDS))
    while True:
        bounds = [i * total // n for i in range(n + 1)]
        bounds[n] = total
        pieces: list[list[str]] = [[] for _ in range(n)]
        word_ranges: list[list[int]] = [[0, 0] for _ in range(n)]
        cursor = 0
        piece_index = 0
        for token, count in zip(tokens, counts):
            while piece_index < n - 1 and cursor >= bounds[piece_index + 1]:
                piece_index += 1
            if not pieces[piece_index]:
                word_ranges[piece_index][0] = cursor
            pieces[piece_index].append(token)
            cursor += count
            word_ranges[piece_index][1] = cursor
        non_empty = [i for i in range(n) if pieces[i]]
        if all(word_ranges[i][1] - word_ranges[i][0] <= MAX_TRANSLATION_UNIT_WORDS for i in non_empty):
            break
        n += 1

    out: list[dict] = []
    for i in non_empty:
        piece_text = " ".join(pieces[i])
        words = {word.casefold() for word in _WORD_RE.findall(piece_text)}
        w0, w1 = word_ranges[i]
        piece_end = end if i == non_empty[-1] else start + duration * w1 / total
        out.append({
            **unit,
            "text": piece_text,
            "start": start + duration * w0 / total,
            "end": piece_end,
            "highlighted_words": [
                word for word in unit["highlighted_words"] if str(word).casefold() in words
            ],
            "word_meanings": {
                word: meaning
                for word, meaning in unit["word_meanings"].items()
                if str(word).casefold() in words
            },
            "highlighted_word_lists": {
                word: list_key
                for word, list_key in (unit.get("highlighted_word_lists") or {}).items()
                if str(word).casefold() in words
            },
        })
    return out


def _rebalance_punctuationless_units(units: list[dict]) -> list[dict]:
    """同一逻辑源被预切出的连续无句末标点 unit 合并后重新均衡切分，避免小尾巴。

    只合并 segment_ids 完全相同的相邻 run，句末边界和其他 cue 的切分保持不变。
    """
    out: list[dict] = []
    i = 0
    while i < len(units):
        unit = units[i]
        if _SENTENCE_END_RE.search(unit["text"].strip()):
            out.append(unit)
            i += 1
            continue
        run = [unit]
        j = i + 1
        while (
            j < len(units)
            and not _SENTENCE_END_RE.search(units[j]["text"].strip())
            and units[j].get("segment_ids")
            and units[j]["segment_ids"] == unit.get("segment_ids")
        ):
            run.append(units[j])
            j += 1
        if len(run) == 1:
            out.append(unit)
        else:
            merged = {
                **unit,
                "text": " ".join(piece["text"] for piece in run),
                "start": float(run[0]["start"]),
                "end": float(run[-1]["end"]),
                "highlighted_words": sorted({
                    word for piece in run for word in piece["highlighted_words"]
                }),
                "word_meanings": {
                    word: meaning
                    for piece in run
                    for word, meaning in piece["word_meanings"].items()
                },
                "highlighted_word_lists": {
                    word: list_key
                    for piece in run
                    for word, list_key in (piece.get("highlighted_word_lists") or {}).items()
                },
            }
            out.extend(_split_oversized_unit(merged))
        i = j
    return out


def _split_unit_by_capitals(unit: dict) -> list[dict]:
    """跨 cue 累积出的粘连 unit 按大写边界补切（cue 内粘连已在 _split_source_segments 切过）。

    whisper 保留词首大小写：cue 首词小写=续句、大写=新句。cue A 无句末点而 cue B 大写开头时
    累积缓冲会把两句粘进同一 unit（"…their services Companies which sell…"），
    只能在合并后的文本上用同一把大写尺补切，时间轴按字符比例插值。
    """
    parts = SentenceAnalyzer._split_capital_boundaries(str(unit.get("text") or ""))
    if len(parts) <= 1:
        return [unit]
    total = sum(max(1, len(part)) for part in parts)
    start = float(unit["start"])
    end = float(unit["end"])
    duration = max(0.0, end - start)
    out: list[dict] = []
    cursor = start
    for i, part in enumerate(parts):
        part_end = end if i == len(parts) - 1 else cursor + duration * max(1, len(part)) / total
        words = {word.casefold() for word in _WORD_RE.findall(part)}
        out.append({
            **unit,
            "text": part,
            "start": cursor,
            "end": part_end,
            "highlighted_words": [
                word for word in unit["highlighted_words"] if str(word).casefold() in words
            ],
            "word_meanings": {
                word: meaning
                for word, meaning in unit["word_meanings"].items()
                if str(word).casefold() in words
            },
            "highlighted_word_lists": {
                word: list_key
                for word, list_key in (unit.get("highlighted_word_lists") or {}).items()
                if str(word).casefold() in words
            },
        })
        cursor = part_end
    return out


def build_translation_units(segments: list[dict]) -> list[dict]:
    """Mirror the workspace sentence-unit boundaries used for playback."""
    units: list[dict] = []
    parts: list[str] = []
    word_count = 0
    highlighted: set[str] = set()
    word_meanings: dict[str, str] = {}
    word_lists: dict[str, str] = {}
    segment_ids: list[int] = []
    start: float | None = None
    end = 0.0

    def flush() -> None:
        nonlocal parts, word_count, highlighted, word_meanings, word_lists, segment_ids, start, end
        text = " ".join(" ".join(parts).split())
        if text:
            units.append({
                "index": len(units),
                "text": text,
                "start": float(start or 0),
                "end": float(end),
                "highlighted_words": sorted(highlighted),
                "word_meanings": dict(word_meanings),
                "highlighted_word_lists": dict(word_lists),
                "segment_ids": list(segment_ids),
            })
        parts = []
        word_count = 0
        highlighted = set()
        word_meanings = {}
        word_lists = {}
        segment_ids = []
        start = None
        end = 0.0

    split_segments = _split_source_segments(segments)
    for index, segment in enumerate(split_segments):
        text = " ".join(str(segment.get("text") or "").split())
        if not text:
            continue
        if start is None:
            start = float(segment.get("start") or 0)
        end = float(segment.get("end") or segment.get("start") or start)
        parts.append(text)
        word_count += len(_WORD_RE.findall(text))
        segment_id = int(segment.get("index", index))
        if not segment_ids or segment_ids[-1] != segment_id:
            segment_ids.append(segment_id)
        highlighted.update(str(word) for word in segment.get("highlighted_words", []))
        for word, meaning in (segment.get("word_meanings") or {}).items():
            if meaning and word not in word_meanings:
                word_meanings[word] = meaning
        for word, list_key in (segment.get("highlighted_word_lists") or {}).items():
            if list_key and word not in word_lists:
                word_lists[word] = list_key
        next_segment = split_segments[index + 1] if index + 1 < len(split_segments) else None
        gap = (float(next_segment.get("start") or 0) - end) if next_segment else 0.0
        next_ends_sentence = bool(
            next_segment
            and _SENTENCE_END_RE.search(str(next_segment.get("text") or "").strip())
        )
        if (
            _SENTENCE_END_RE.search(text)
            or (word_count >= MAX_TRANSLATION_UNIT_WORDS and not next_ends_sentence)
            or (gap > 1.4 and word_count >= 8)
            or index == len(split_segments) - 1
        ):
            flush()
    out: list[dict] = []
    for unit in units:
        for capital_split in _split_unit_by_capitals(unit):
            out.extend(_split_oversized_unit(capital_split))
    out = _rebalance_punctuationless_units(out)
    for index, unit in enumerate(out):
        unit["index"] = index
    return out


def translate_reading_blocks(lesson_id: int) -> dict:
    """Reading 课批量翻译：以阅读块句子为源，与 TTS 合成解耦并行运行。

    翻译缓存按文本键（v2_sentence），TTS 完成后字幕段复用同一份缓存。
    """
    from webapp.services.v2_tts import _synthesizable_sentences

    blocks = db.get_v2_reading_blocks(lesson_id)
    texts: list[str] = []
    seen: set[str] = set()
    for block in blocks:
        for sentence in _synthesizable_sentences(str(block.get("text") or "")):
            normalized = " ".join(sentence.split())
            if normalized and normalized not in seen:
                seen.add(normalized)
                texts.append(normalized)
    total = len(texts)
    if not texts:
        error = "No reading sentences are available for Hy-MT translation"
        db.update_v2_translation_status(
            lesson_id, status="failed", done=0, total=0, ready=False, error=error
        )
        return {"status": "failed", "done": 0, "total": 0, "error": error}
    if not hy_ready():
        error = "Hy-MT translation model is not ready"
        db.update_v2_translation_status(
            lesson_id, status="failed", done=0, total=total, ready=False, error=error
        )
        return {"status": "failed", "done": 0, "total": total, "error": error}

    db.update_v2_translation_status(
        lesson_id, status="translating", done=0, total=total,
        buffer_seconds=0, rate=0, ready=False, error="",
    )
    done = 0
    try:
        for text in texts:
            cached = db.get_v2_sentence(text)
            translation = str((cached or {}).get("translation") or "").strip()
            if not translation:
                translation = hy_translate(text)
                if not translation:
                    raise RuntimeError("Hy-MT returned an empty translation")
                db.upsert_v2_sentence(text, translation=translation)
            done += 1
            db.update_v2_translation_status(
                lesson_id, status="translating", done=done, total=total,
                buffer_seconds=0, rate=0, ready=False,
            )
    except Exception as exc:
        db.update_v2_translation_status(
            lesson_id, status="failed", done=done, total=total,
            ready=False, error=str(exc) or repr(exc),
        )
        return {"status": "failed", "done": done, "total": total, "error": str(exc) or repr(exc)}

    db.update_v2_translation_status(
        lesson_id, status="ready", done=total, total=total,
        buffer_seconds=0, ready=True, error="",
    )
    return {"status": "ready", "done": total, "total": total}


def translate_lesson_subtitles(lesson_id: int) -> dict:
    units = build_translation_units(db.get_v2_subtitle_segments(lesson_id))
    total = len(units)
    if not units:
        error = "No subtitles are available for Hy-MT translation"
        db.update_v2_translation_status(
            lesson_id, status="failed", done=0, total=0, ready=False, error=error
        )
        return {"status": "failed", "done": 0, "total": 0, "error": error}

    pending = []
    for unit in units:
        cached = db.get_v2_sentence(unit["text"])
        if not cached or not str(cached.get("translation") or "").strip():
            pending.append(unit)
    total_duration = float(units[-1]["end"] or 0)
    if not pending:
        db.update_v2_translation_status(
            lesson_id,
            status="ready",
            done=total,
            total=total,
            buffer_seconds=total_duration,
            rate=0,
            ready=True,
            error="",
        )
        return {"status": "ready", "done": total, "total": total}
    if pending and not hy_ready():
        error = "Hy-MT translation model is not ready"
        db.update_v2_translation_status(
            lesson_id, status="failed", done=total - len(pending), total=total,
            ready=False, error=error,
        )
        return {"status": "failed", "done": total - len(pending), "total": total, "error": error}

    started = time.monotonic()
    done = 0
    target_buffer = min(_SAFE_BUFFER_SECONDS, total_duration)
    db.update_v2_translation_status(
        lesson_id, status="translating", done=0, total=total,
        buffer_seconds=0, rate=0, ready=False, error="",
    )
    try:
        for unit in units:
            cached = db.get_v2_sentence(unit["text"])
            translation = str((cached or {}).get("translation") or "").strip()
            if not translation:
                translation = hy_translate(unit["text"])
                if not translation:
                    raise RuntimeError("Hy-MT returned an empty translation")
                db.upsert_v2_sentence(unit["text"], translation=translation)
            done += 1
            buffer_seconds = float(unit["end"] or 0)
            rate = buffer_seconds / max(time.monotonic() - started, 0.001)
            ready = buffer_seconds >= target_buffer and rate >= _SAFE_TRANSLATION_RATE
            db.update_v2_translation_status(
                lesson_id,
                status="translating",
                done=done,
                total=total,
                buffer_seconds=buffer_seconds,
                rate=rate,
                ready=ready,
            )
    except Exception as exc:
        db.update_v2_translation_status(
            lesson_id, status="failed", done=done, total=total,
            ready=False, error=str(exc),
        )
        return {"status": "failed", "done": done, "total": total, "error": str(exc)}

    db.update_v2_translation_status(
        lesson_id,
        status="ready",
        done=total,
        total=total,
        buffer_seconds=total_duration,
        ready=True,
        error="",
    )
    return {"status": "ready", "done": total, "total": total}
