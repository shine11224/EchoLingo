"""轻量单课 RAG 检索（Task 9）。

职责单一：构建精细候选窗口、复用 AI outline 章节做粗路由、词项排序、
AI 回答/无内容复核的结果校验，并把 opaque candidate ID 映射回服务器侧
真实锚点（视频 segment 时间区间 / Reading sentence_key 或 block）。

不直接渲染 HTML；不引入向量库；绝不跨 lesson 读取（全部经由 db 当前用户
上下文内的 lesson_id 查询）。
"""
from __future__ import annotations

import json
import math
import re

import db
from prompts import (
    LESSON_RAG_ABSENCE_PROMPT,
    LESSON_RAG_ANSWER_PROMPT,
    LESSON_RAG_ROUTE_PROMPT,
)

MIN_WINDOW_WORDS = 100
MAX_WINDOW_WORDS = 180
TOP_CHAPTERS = 4
TOP_CANDIDATES = 8
ABSENCE_CANDIDATES = 6
MAX_CITATIONS = 3
COVERAGE_VALUES = {"full", "partial", "none"}
MEDIA_SOURCE_TYPES = {
    "youtube", "bilibili", "local_audio", "local_video", "uploaded_media",
}

CHAPTER_PREVIEW_CHARS = 180
CHAPTER_KEYWORDS = 6

NONE_FALLBACK_ANSWER = "当前课程中没有找到与该问题对应的内容。"

_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "is", "are",
    "was", "were", "be", "been", "it", "this", "that", "these", "those",
    "i", "you", "he", "she", "we", "they", "do", "does", "did", "what",
    "why", "how", "when", "where", "which", "who", "does", "can", "could",
}


class RetrievalFormatError(ValueError):
    """AI 输出经一次修复重试后仍无法校验通过。"""


def _words(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z']+", (text or "").lower())


def _word_set(text: str) -> set[str]:
    return {w for w in _words(text) if w not in _STOPWORDS}


def _clip(text: str, limit: int = 160) -> str:
    normalized = " ".join((text or "").split())
    return normalized if len(normalized) <= limit else normalized[: limit - 1].rstrip() + "…"


# ── 候选窗口构建 ──────────────────────────────────────────────────────


def _window_ranges(lengths: list[int]) -> list[tuple[int, int]]:
    """按 100–180 词合并连续单元，保留 1 个单元 overlap。返回 (start, end) 半开区间。"""
    ranges = []
    n = len(lengths)
    start = 0
    while start < n:
        total = 0
        end = start
        while end < n:
            w = max(lengths[end], 1)
            if total >= MIN_WINDOW_WORDS and total + w > MAX_WINDOW_WORDS:
                break
            total += w
            end += 1
        if end == start:  # 防御：单个超长单元
            end = start + 1
        ranges.append((start, end))
        next_start = end - 1 if end < n else n
        if next_start <= start:
            next_start = start + 1
        start = next_start
    return ranges


def build_media_candidates(segments: list[dict]) -> list[dict]:
    """视频：字幕句 → 重叠精细窗口，锚定真实 segment index/start/end/text。"""
    segs = [s for s in segments if str(s.get("text", "")).strip()]
    lengths = [len(_words(str(s.get("text", "")))) for s in segs]
    candidates = []
    for n, (start, end) in enumerate(_window_ranges(lengths), start=1):
        group = segs[start:end]
        candidates.append(
            {
                "id": f"c{n:03d}",
                "kind": "time",
                "segment_indices": [int(s["index"]) for s in group],
                "segment_index": int(group[0]["index"]),
                "start": float(group[0].get("start", 0.0) or 0.0),
                "end": float(group[-1].get("end", group[-1].get("start", 0.0)) or 0.0),
                "text": " ".join(str(s.get("text", "")).strip() for s in group),
            }
        )
    return candidates


def build_reading_candidates(blocks: list[dict]) -> list[dict]:
    """Reading：优先句级窗口（保留真实 sentence_key）；无句数据时退化为 block 窗口。"""
    sentences = []
    for block in blocks:
        block_index = int(block.get("index", 0))
        for sentence in block.get("sentences") or []:
            text = str(sentence.get("text", "")).strip()
            if not text:
                continue
            sentences.append(
                {
                    "block_index": block_index,
                    "sentence_key": sentence.get("sentence_key"),
                    "text": text,
                }
            )
    candidates = []
    if sentences:
        lengths = [len(_words(s["text"])) for s in sentences]
        for n, (start, end) in enumerate(_window_ranges(lengths), start=1):
            group = sentences[start:end]
            candidates.append(
                {
                    "id": f"c{n:03d}",
                    "kind": "sentence",
                    "block_index": group[0]["block_index"],
                    "sentences": group,
                    "sentence_keys": [s["sentence_key"] for s in group],
                    "text": " ".join(s["text"] for s in group),
                }
            )
        return candidates
    non_empty = [b for b in blocks if str(b.get("text", "")).strip()]
    for n, block in enumerate(non_empty, start=1):
        candidates.append(
            {
                "id": f"c{n:03d}",
                "kind": "block",
                "block_index": int(block.get("index", 0)),
                "text": " ".join(str(block.get("text", "")).split()),
            }
        )
    return candidates


# ── Reading 选区解析（服务端权威匹配，不信任客户端键）──────────────────


def _norm_text(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def _selection_matched_sentence_ids(
    blocks: list[dict], selected_text: str
) -> set[tuple[int, str]]:
    """把用户选区解析为当前课真实 (block_index, norm_sentence_text) 集合。

    只信服务器 blocks/sentences：block 文本整体包含选区 → 该 block 全部句子命中；
    否则按句文本是否被选区包含逐句匹配。客户端自报 sentence_key 从不参与。
    """
    selected_norm = _norm_text(selected_text)
    if not selected_norm:
        return set()
    matched: set[tuple[int, str]] = set()
    for block in blocks:
        block_index = int(block.get("index", 0))
        sentences = [
            s for s in (block.get("sentences") or []) if str(s.get("text", "")).strip()
        ]
        # 句级精确包含优先：选区含哪句就命中哪句（单句选区只命中该句）
        sentence_hits = [
            s for s in sentences
            if _norm_text(s["text"]) and _norm_text(s["text"]) in selected_norm
        ]
        if sentence_hits:
            matched.update((block_index, _norm_text(s["text"])) for s in sentence_hits)
            continue
        # 句级对不上但整段包含选区（句切分与客户端选区边界不一致）→ 该段全部句子
        block_norm = _norm_text(block.get("text", ""))
        if block_norm and selected_norm in block_norm:
            matched.update((block_index, _norm_text(s["text"])) for s in sentences)
    return matched


def _selection_exact_sentence_ids(
    blocks: list[dict], selected_text: str
) -> set[tuple[int, str]]:
    """citation 收敛用的精确层：先取完整落在选区内的句子；
    选区是句内片段时退取包含整个选区的句子。块级命中（_selection_matched_sentence_ids）
    只用于窗口过滤，句子级锚点必须以本层为准，否则整段选区会错误锚到段首句。"""
    selected_norm = _norm_text(selected_text)
    if not selected_norm:
        return set()
    exact: set[tuple[int, str]] = set()
    for block in blocks:
        block_index = int(block.get("index", 0))
        for sentence in (block.get("sentences") or []):
            sentence_norm = _norm_text(sentence.get("text", ""))
            if sentence_norm and sentence_norm in selected_norm:
                exact.add((block_index, sentence_norm))
    if exact:
        return exact
    for block in blocks:
        block_index = int(block.get("index", 0))
        for sentence in (block.get("sentences") or []):
            sentence_norm = _norm_text(sentence.get("text", ""))
            if sentence_norm and selected_norm in sentence_norm:
                exact.add((block_index, sentence_norm))
    return exact


def _selection_matched_block_indices(blocks: list[dict], selected_text: str) -> set[int]:
    """无句数据时的 block 级选区匹配（双向包含，覆盖选区跨句/段内片段）。"""
    selected_norm = _norm_text(selected_text)
    if not selected_norm:
        return set()
    matched = set()
    for block in blocks:
        block_norm = _norm_text(block.get("text", ""))
        if not block_norm:
            continue
        if selected_norm in block_norm or block_norm in selected_norm:
            matched.add(int(block.get("index", 0)))
    return matched


def _filter_candidates_with_context(
    candidates: list[dict], hit_positions: list[int]
) -> list[dict]:
    """命中窗口 + 前后各 1 个窗口的有界上下文。"""
    positions = set()
    for pos in hit_positions:
        positions.update({pos - 1, pos, pos + 1})
    return [c for pos, c in enumerate(candidates) if pos in positions]


# ── 章节粗路由边界 ────────────────────────────────────────────────────


def _chapter_signal(candidate_ids: list[str], candidate_map: dict[str, dict]) -> dict:
    """有界章节预览：首候选截断文本 +  deterministic 高频内容词，供 AI 语义路由。

    不发送全章原文；预览与关键词都有硬上限。
    """
    texts = [candidate_map[cid].get("text", "") for cid in candidate_ids if cid in candidate_map]
    preview = _clip(texts[0] if texts else "", CHAPTER_PREVIEW_CHARS)
    freq: dict[str, int] = {}
    for text in texts:
        for word in set(_words(text)):
            if word in _STOPWORDS or len(word) < 3:
                continue
            freq[word] = freq.get(word, 0) + 1
    keywords = [
        word for word, _n in
        sorted(freq.items(), key=lambda item: (-item[1], item[0]))[:CHAPTER_KEYWORDS]
    ]
    return {"preview": preview, "keywords": keywords}


def _fallback_chapters(candidates: list[dict]) -> list[dict]:
    """无 outline 时的确定性粗分块（最多 6 章）；附预览/关键词使其可被 AI 语义路由。"""
    if not candidates:
        return []
    candidate_map = {c["id"]: c for c in candidates}
    size = max(1, math.ceil(len(candidates) / 6))
    chapters = []
    for index, offset in enumerate(range(0, len(candidates), size)):
        group = candidates[offset : offset + size]
        ids = [c["id"] for c in group]
        chapters.append(
            {
                "index": len(chapters),
                "title": f"第 {index + 1} 部分",
                "description": "",
                "generic": True,
                "candidate_ids": ids,
                **_chapter_signal(ids, candidate_map),
            }
        )
    return chapters


def build_chapters(outline: dict | None, candidates: list[dict]) -> list[dict]:
    """把 outline sections 的 time/block anchor 映射为候选窗口的章节边界。"""
    sections = (outline or {}).get("sections") or []
    if not candidates or not sections:
        return _fallback_chapters(candidates)
    is_time = candidates[0]["kind"] == "time"

    def position(candidate: dict) -> float:
        if is_time:
            return float(candidate.get("start", 0.0))
        return float(candidate.get("block_index", 0))

    anchors = []
    for section in sections:
        try:
            anchor = float(section.get("anchor_id"))
        except (TypeError, ValueError):
            continue
        anchors.append(
            {
                "anchor": anchor,
                "title": str(section.get("title") or ""),
                "description": str(section.get("description") or ""),
            }
        )
    anchors.sort(key=lambda item: item["anchor"])
    if not anchors:
        return _fallback_chapters(candidates)
    candidate_map = {c["id"]: c for c in candidates}
    chapters = [
        {
            "index": i,
            "title": item["title"],
            "description": item["description"],
            "generic": False,
            "candidate_ids": [],
        }
        for i, item in enumerate(anchors)
    ]
    for candidate in candidates:
        pos = position(candidate)
        chapter_index = 0
        for i, item in enumerate(anchors):
            if pos >= item["anchor"]:
                chapter_index = i
            else:
                break
        chapters[chapter_index]["candidate_ids"].append(candidate["id"])
    chapters = [c for c in chapters if c["candidate_ids"]]
    if not chapters:
        return _fallback_chapters(candidates)
    for chapter in chapters:
        chapter.update(_chapter_signal(chapter["candidate_ids"], candidate_map))
    return chapters


# ── 本地词项排序（确定性，不承担“无内容”判定）─────────────────────────


def rank_candidates(
    question: str,
    candidates: list[dict],
    *,
    timestamp_seconds: float = 0.0,
    limit: int = TOP_CANDIDATES,
) -> list[dict]:
    q = _word_set(question)
    scored = []
    for order, candidate in enumerate(candidates):
        score = float(len(q & _word_set(candidate.get("text", ""))))
        if timestamp_seconds and candidate.get("kind") == "time":
            distance = abs(float(candidate.get("start", 0.0)) - float(timestamp_seconds))
            score += max(0.0, 1.0 - distance / 300.0) * 0.5
        scored.append((-score, order, candidate))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [candidate for _neg, _order, candidate in scored[:limit]]


# ── AI 输出解析与校验 ─────────────────────────────────────────────────


def _parse_json_object(content: str) -> dict:
    """容忍 fenced block / 前后杂音的 JSON 对象提取。"""
    text = (content or "").strip()
    if not text:
        raise RetrievalFormatError("empty AI output")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        if start < 0:
            raise RetrievalFormatError("no JSON object in AI output")
        depth = 0
        end = -1
        in_string = False
        escape = False
        for i, ch in enumerate(text[start:], start=start):
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end < 0:
            raise RetrievalFormatError("unbalanced JSON object in AI output")
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RetrievalFormatError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RetrievalFormatError("AI output is not a JSON object")
    return data


def _stratified_chapter_indices(chapters: list[dict]) -> list[int]:
    """路由失败的有界兜底：等距抽取最多 TOP_CHAPTERS 章，保证覆盖整课而非只有开头。"""
    n = len(chapters)
    if n <= TOP_CHAPTERS:
        return [c["index"] for c in chapters]
    picked = []
    for i in range(TOP_CHAPTERS):
        pos = round(i * (n - 1) / (TOP_CHAPTERS - 1))
        idx = chapters[pos]["index"]
        if idx not in picked:
            picked.append(idx)
    return picked


def parse_route(content: str, chapters: list[dict]) -> list[int]:
    """章节路由结果：只保留真实存在的章节 index，去重，最多 TOP_CHAPTERS 个；
    任何解析失败都退回等距分层兜底（覆盖整课）。"""
    valid = {c["index"] for c in chapters}
    try:
        data = _parse_json_object(content)
    except RetrievalFormatError:
        return _stratified_chapter_indices(chapters)
    raw = data.get("chapters")
    if not isinstance(raw, list):
        return _stratified_chapter_indices(chapters)
    picked = []
    for item in raw:
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)) and int(item) in valid and int(item) not in picked:
            picked.append(int(item))
        if len(picked) >= TOP_CHAPTERS:
            break
    return picked or _stratified_chapter_indices(chapters)


def validate_answer_payload(
    content: str,
    candidate_map: dict[str, dict],
    *,
    allow_external: bool,
) -> dict:
    """严格校验回答契约；未知/伪造 candidate ID 一律丢弃，模型时间戳从不被接受。

    - coverage 必须是 full|partial|none，否则抛 RetrievalFormatError（路由层修复重试）。
    - full 必须有至少一条有效 citation，否则降级 partial。
    - none 不允许任何 citation。
    - external_knowledge_used 只有在用户显式允许时才可能为 true。
    """
    data = _parse_json_object(content)
    coverage = str(data.get("coverage") or "").strip().lower()
    if coverage not in COVERAGE_VALUES:
        raise RetrievalFormatError(f"invalid coverage: {coverage!r}")
    answer = str(data.get("answer") or "").strip()
    raw_citations = data.get("citations")
    if raw_citations is None:
        raw_citations = []
    if not isinstance(raw_citations, list):
        raise RetrievalFormatError("citations must be a list")
    candidate_ids: list[str] = []
    for item in raw_citations:
        cid = item if isinstance(item, str) else None
        if cid is None and isinstance(item, dict):
            cid = item.get("candidate_id")
        cid = str(cid or "").strip()
        if cid and cid in candidate_map and cid not in candidate_ids:
            candidate_ids.append(cid)
        if len(candidate_ids) >= MAX_CITATIONS:
            break
    if coverage == "full" and not candidate_ids:
        coverage = "partial"
    if coverage == "none":
        candidate_ids = []
        if not answer:
            answer = NONE_FALLBACK_ANSWER
    elif not answer:
        raise RetrievalFormatError("answer is empty")
    unsupported_raw = data.get("unsupported")
    unsupported = (
        [str(x) for x in unsupported_raw if str(x).strip()][:5]
        if isinstance(unsupported_raw, list)
        else []
    )
    return {
        "answer": answer,
        "coverage": coverage,
        "candidate_ids": candidate_ids,
        "unsupported": unsupported,
        "external_knowledge_used": bool(data.get("external_knowledge_used")) and allow_external,
    }


def validate_absence_payload(content: str, candidate_map: dict[str, dict]) -> dict:
    """无内容复核的严格校验（与回答同级的修复重试由编排层负责）。

    - found 必须是 bool；candidate_ids 必须是 list；否则抛 RetrievalFormatError。
    - 未知 candidate ID 丢弃；found=true 必须至少剩一条有效 ID。
    """
    data = _parse_json_object(content)
    found = data.get("found")
    if not isinstance(found, bool):
        raise RetrievalFormatError("absence.found must be a bool")
    ids_raw = data.get("candidate_ids", [])
    if not isinstance(ids_raw, list):
        raise RetrievalFormatError("absence.candidate_ids must be a list")
    ids = []
    for item in ids_raw:
        cid = str(item or "").strip()
        if cid in candidate_map and cid not in ids:
            ids.append(cid)
    if found and not ids:
        raise RetrievalFormatError("absence found=true without any valid candidate_id")
    return {"found": found, "candidate_ids": ids}


# ── candidate → 真实锚点 ──────────────────────────────────────────────


def citation_anchor(
    candidate: dict,
    *,
    chapter_title: str = "",
    question: str = "",
    prefer_sentence_ids: set[tuple[int, str]] | None = None,
) -> dict:
    """服务器侧映射：candidate 数据即事实来源，模型输出不参与锚点构造。

    prefer_sentence_ids（reading_selection 服务端解析的 (block_index, norm_text)）
    命中窗口内某句时优先选该句——选区场景下窗口内句子词面高度相似，
    词项重叠无法区分，必须以服务端选区解析为准。
    """
    if candidate["kind"] == "time":
        return {
            "anchor_type": "time",
            "segment_index": int(candidate["segment_index"]),
            "start_seconds": float(candidate["start"]),
            "end_seconds": float(candidate["end"]),
            "chapter_title": chapter_title,
            "excerpt": _clip(candidate.get("text", "")),
        }
    if candidate["kind"] == "sentence":
        sentences = candidate.get("sentences") or []
        best = None
        if prefer_sentence_ids:
            for sentence in sentences:
                identity = (
                    int(sentence.get("block_index", -1)),
                    _norm_text(sentence.get("text", "")),
                )
                if identity in prefer_sentence_ids:
                    best = sentence
                    break
        if best is None and question and len(sentences) > 1:
            q = _word_set(question)
            best = max(
                sentences,
                key=lambda s: len(q & _word_set(s.get("text", ""))),
            )
            if not (q & _word_set(best.get("text", ""))):
                best = None
        chosen = best or (sentences[0] if sentences else {})
        anchor = {
            "anchor_type": "sentence",
            "block_index": int(chosen.get("block_index", candidate.get("block_index", 0))),
            "chapter_title": chapter_title,
            "excerpt": _clip(str(chosen.get("text", candidate.get("text", "")))),
        }
        if chosen.get("sentence_key") is not None:
            anchor["sentence_key"] = int(chosen["sentence_key"])
        return anchor
    return {
        "anchor_type": "block",
        "block_index": int(candidate.get("block_index", 0)),
        "chapter_title": chapter_title,
        "excerpt": _clip(candidate.get("text", "")),
    }


# ── 编排：路由 → 回答 → none 二次复核 ─────────────────────────────────


def _format_history(history: list[dict] | None) -> str:
    lines = []
    for item in (history or [])[-5:]:
        lines.append(f"User: {item.get('user_message', '')}\nAI: {item.get('ai_response', '')}")
    return "\n\n".join(lines)


def _format_candidates(candidates: list[dict], chapter_titles: dict[str, str]) -> str:
    parts = []
    for candidate in candidates:
        title = chapter_titles.get(candidate["id"], "")
        label = f' chapter="{title}"' if title else ""
        parts.append(f'<candidate id="{candidate["id"]}"{label}>\n{candidate.get("text", "")}\n</candidate>')
    return "\n\n".join(parts)


def answer_lesson_question(
    call_ai,
    *,
    lesson: dict,
    question: str,
    history: list[dict] | None = None,
    timestamp_seconds: float = 0.0,
    selected_segment_ids: list[int] | None = None,
    selected_text: str | None = None,
    allow_external: bool = False,
) -> dict:
    """一次用户问题的完整 RAG 流程。call_ai(kind, content) -> str，kind ∈ route/answer/absence。

    所有 AI 调用都由调用方包裹在同一 credit operation 上下文内；本函数不产生扣费。
    selected_text（reading_selection）只在服务端 blocks/sentences 上解析，
    客户端自报键从不被信任；解析不到任何真实句子时不放宽到全文，直接 none。
    """
    lesson_id = int(lesson["id"])
    is_media = str(lesson.get("source_type") or "") in MEDIA_SOURCE_TYPES
    reading_blocks = None
    if is_media:
        candidates = build_media_candidates(db.get_v2_subtitle_segments(lesson_id))
    else:
        reading_blocks = db.get_v2_reading_blocks(lesson_id)
        candidates = build_reading_candidates(reading_blocks)

    selection_active = bool(selected_text and reading_blocks is not None)
    matched_sentence_ids: set[tuple[int, str]] | None = None
    exact_sentence_ids: set[tuple[int, str]] = set()
    if selection_active:
        # 服务端选区过滤：命中窗口 + 前后各 1 窗口有界上下文
        if candidates and candidates[0]["kind"] == "sentence":
            matched_sentence_ids = _selection_matched_sentence_ids(reading_blocks, selected_text)
            exact_sentence_ids = _selection_exact_sentence_ids(reading_blocks, selected_text)
            hits = [
                pos
                for pos, c in enumerate(candidates)
                if {
                    (s.get("block_index", -1), _norm_text(s.get("text", "")))
                    for s in (c.get("sentences") or [])
                } & matched_sentence_ids
            ]
        else:
            matched_blocks = _selection_matched_block_indices(reading_blocks, selected_text)
            hits = [
                pos
                for pos, c in enumerate(candidates)
                if int(c.get("block_index", -1)) in matched_blocks
            ]
        candidates = _filter_candidates_with_context(candidates, hits) if hits else []
    elif selected_segment_ids:
        wanted = {int(x) for x in selected_segment_ids}
        if candidates and candidates[0]["kind"] == "time":
            candidates = [
                c for c in candidates if wanted & set(c.get("segment_indices") or [])
            ]
        else:
            candidates = [
                c
                for c in candidates
                if wanted & {
                    s.get("segment_index")
                    for s in (c.get("sentences") or [])
                    if s.get("segment_index") is not None
                }
            ]

    if not candidates:
        return {
            "answer": NONE_FALLBACK_ANSWER,
            "coverage": "none",
            "citations": [],
            "unsupported": [],
            "external_knowledge_used": False,
        }

    scoped = bool(selection_active or selected_segment_ids)
    effective_question = question
    if selection_active:
        # 选区原文随问题进入排序/回答，锚点仍只来自服务端 candidate map
        effective_question = f"{question}\n（用户选中原文：{_clip(selected_text, 500)}）"

    candidate_map = {c["id"]: c for c in candidates}
    outline_row = db.get_latest_v2_document_outline(lesson_id)
    chapters = build_chapters(outline_row["outline"] if outline_row else None, candidates)
    chapter_titles = {
        cid: chapter["title"]
        for chapter in chapters
        for cid in chapter["candidate_ids"]
    }

    # 1) 章节路由：generic 章节携带预览/关键词，中文问题也可语义命中后置章节；
    #    路由输出非法时 parse_route 退回等距分层兜底，仍然覆盖整课；
    #    选区/选句限定时不做路由，候选已经是用户指定范围
    needs_ai_route = len(chapters) > 1 and not scoped
    if needs_ai_route:
        route_prompt = LESSON_RAG_ROUTE_PROMPT.format(
            lesson_title=lesson.get("title") or "Untitled",
            chapters_json=json.dumps(
                [
                    {
                        "index": c["index"], "title": c["title"],
                        "description": c["description"],
                        "preview": c.get("preview", ""),
                        "keywords": c.get("keywords", []),
                    }
                    for c in chapters
                ],
                ensure_ascii=False,
            ),
            history_text=_format_history(history) or "（无）",
            question=question,
        )
        picked = parse_route(call_ai("route", route_prompt), chapters)
    else:
        picked = [c["index"] for c in chapters]
    picked_set = set(picked)
    allowed_ids = {
        cid for chapter in chapters if chapter["index"] in picked_set for cid in chapter["candidate_ids"]
    }
    pool = [c for c in candidates if c["id"] in allowed_ids] or candidates

    # 2) 本地排序 + 回答（一次格式修复重试；仍失败抛 RetrievalFormatError 由路由层释放积分）
    top = rank_candidates(effective_question, pool, timestamp_seconds=timestamp_seconds)

    def _ask(cands: list[dict]) -> dict:
        prompt = LESSON_RAG_ANSWER_PROMPT.format(
            lesson_title=lesson.get("title") or "Untitled",
            allow_external="是" if allow_external else "否",
            candidates_text=_format_candidates(cands, chapter_titles),
            history_text=_format_history(history) or "（无）",
            question=effective_question,
        )
        content = call_ai("answer", prompt)
        try:
            return validate_answer_payload(content, candidate_map, allow_external=allow_external)
        except RetrievalFormatError:
            repair = call_ai(
                "answer",
                prompt + "\n\n上一次输出未通过格式校验，请严格按 JSON 契约重新输出。",
            )
            return validate_answer_payload(repair, candidate_map, allow_external=allow_external)

    result = _ask(top)

    # 3) none → 用剩余候选做一次独立定向复核（与回答同级的一次修复重试；
    #    复核输出最终仍非法同样抛 RetrievalFormatError，由路由层释放积分）
    if result["coverage"] == "none" and not scoped:
        asked_ids = {c["id"] for c in top}
        remaining = [c for c in candidates if c["id"] not in asked_ids]
        if remaining:
            rest = rank_candidates(effective_question, remaining, limit=ABSENCE_CANDIDATES)
            absence_prompt = LESSON_RAG_ABSENCE_PROMPT.format(
                question=question,
                candidates_text=_format_candidates(rest, chapter_titles),
            )
            try:
                absence = validate_absence_payload(
                    call_ai("absence", absence_prompt), candidate_map)
            except RetrievalFormatError:
                absence = validate_absence_payload(
                    call_ai(
                        "absence",
                        absence_prompt + "\n\n上一次输出未通过格式校验，请严格按 JSON 契约重新输出。",
                    ),
                    candidate_map,
                )
            if absence["found"]:
                merged = {c["id"]: c for c in top}
                for cid in absence["candidate_ids"]:
                    merged[cid] = candidate_map[cid]
                result = _ask(list(merged.values()))

    anchors = [
        citation_anchor(
            candidate_map[cid],
            chapter_title=chapter_titles.get(cid, ""),
            question=effective_question,
            prefer_sentence_ids=exact_sentence_ids or matched_sentence_ids,
        )
        for cid in result["candidate_ids"]
    ]
    return {
        "answer": result["answer"],
        "coverage": result["coverage"],
        "citations": anchors,
        "unsupported": result["unsupported"],
        "external_knowledge_used": result["external_knowledge_used"],
    }
