"""Task 9：轻量单课 RAG —— 候选窗口、章节边界、契约校验与锚点映射。"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from webapp.services import lesson_retrieval as lr


def _segments(n=30, words=8):
    return [
        {
            "index": i + 1,
            "start": i * 2.0,
            "end": i * 2.0 + 1.5,
            "text": f"sentence number {i + 1} " + " ".join(f"w{j}" for j in range(words)),
        }
        for i in range(n)
    ]


# ---------- 窗口构建 ----------

def test_media_windows_bounded_with_overlap_and_real_anchors():
    segs = _segments(30, words=8)  # 每句 9-10 词 → 每窗 ~11-18 句
    candidates = lr.build_media_candidates(segs)
    assert len(candidates) >= 2
    ids = [c["id"] for c in candidates]
    assert len(set(ids)) == len(ids)
    for c in candidates:
        wc = len(lr._words(c["text"]))
        assert wc <= lr.MAX_WINDOW_WORDS or len(c["segment_indices"]) == 1
        first = segs[c["segment_indices"][0] - 1]
        last = segs[c["segment_indices"][-1] - 1]
        assert c["segment_index"] == first["index"]
        assert c["start"] == first["start"]
        assert c["end"] == last["end"]
        assert first["text"] in c["text"]
    # 1 句 overlap：相邻窗口共享一个 segment
    for a, b in zip(candidates, candidates[1:]):
        assert a["segment_indices"][-1] == b["segment_indices"][0]


def test_reading_windows_keep_sentence_keys():
    blocks = [
        {
            "index": 1,
            "text": "A. B.",
            "sentences": [
                {"sentence_key": 10001, "text": "Alpha beta gamma delta epsilon."},
                {"sentence_key": 10002, "text": "Zeta eta theta iota kappa."},
            ],
        },
        {
            "index": 2,
            "text": "C.",
            "sentences": [{"sentence_key": 12003, "text": "Lambda mu nu xi omicron."}],
        },
    ]
    candidates = lr.build_reading_candidates(blocks)
    assert candidates[0]["kind"] == "sentence"
    keys = [k for c in candidates for k in c["sentence_keys"]]
    assert keys == [10001, 10002, 12003]
    assert candidates[0]["block_index"] == 1


def test_reading_falls_back_to_blocks_without_sentences():
    blocks = [{"index": 3, "text": "Only block text here.", "sentences": []}]
    candidates = lr.build_reading_candidates(blocks)
    assert candidates == [
        {"id": "c001", "kind": "block", "block_index": 3, "text": "Only block text here."}
    ]


# ---------- 章节边界 ----------

def test_outline_time_anchors_define_chapter_boundaries():
    segs = _segments(30, words=8)
    candidates = lr.build_media_candidates(segs)
    outline = {
        "sections": [
            {"anchor_id": 0.0, "anchor_type": "time", "title": "开场", "description": "intro"},
            {"anchor_id": 30.0, "anchor_type": "time", "title": "习惯机制", "description": "core"},
        ]
    }
    chapters = lr.build_chapters(outline, candidates)
    assert [c["title"] for c in chapters] == ["开场", "习惯机制"]
    first_second = [cid for cid in chapters[1]["candidate_ids"]][0]
    cand = {c["id"]: c for c in candidates}[first_second]
    assert cand["start"] >= 30.0
    assert chapters[0]["candidate_ids"]


def test_no_outline_falls_back_to_deterministic_chunks():
    candidates = lr.build_media_candidates(_segments(30, words=8))
    chapters = lr.build_chapters(None, candidates)
    assert chapters
    assert all(c["generic"] for c in chapters)
    covered = [cid for c in chapters for cid in c["candidate_ids"]]
    assert covered == [c["id"] for c in candidates]


# ---------- 契约校验 ----------

def _cmap():
    return {c["id"]: c for c in lr.build_media_candidates(_segments(60, words=8))}


def test_validate_drops_forged_and_duplicate_citation_ids():
    content = json.dumps({
        "answer": "答案",
        "coverage": "full",
        "citations": [
            {"candidate_id": "c001"},
            {"candidate_id": "c999"},       # 伪造 ID
            {"candidate_id": "c001"},       # 重复
            "c002", "c003", "c004",          # 超上限
        ],
    })
    result = lr.validate_answer_payload(content, _cmap(), allow_external=False)
    assert result["candidate_ids"] == ["c001", "c002", "c003"]
    assert result["coverage"] == "full"


def test_full_without_valid_citation_is_downgraded():
    content = json.dumps({
        "answer": "答案", "coverage": "full",
        "citations": [{"candidate_id": "c999", "start_seconds": 123.4}],
    })
    result = lr.validate_answer_payload(content, _cmap(), allow_external=False)
    assert result["coverage"] == "partial"
    assert result["candidate_ids"] == []


def test_none_clears_citations_and_fills_default_answer():
    content = json.dumps({
        "answer": "", "coverage": "none",
        "citations": [{"candidate_id": "c001"}],
    })
    result = lr.validate_answer_payload(content, _cmap(), allow_external=False)
    assert result["coverage"] == "none"
    assert result["candidate_ids"] == []
    assert result["answer"] == lr.NONE_FALLBACK_ANSWER


def test_invalid_coverage_raises_format_error():
    with pytest.raises(lr.RetrievalFormatError):
        lr.validate_answer_payload(
            json.dumps({"answer": "x", "coverage": "maybe"}), _cmap(), allow_external=False)


def test_json_repair_accepts_fenced_and_trailing_text():
    inner = json.dumps({"answer": "修好", "coverage": "partial", "citations": []})
    result = lr.validate_answer_payload(
        f"好的，以下是结果：\n```json\n{inner}\n```\n希望有帮助！",
        _cmap(), allow_external=False)
    assert result["answer"] == "修好"


def test_external_knowledge_clamped_when_not_allowed():
    content = json.dumps({
        "answer": "答案", "coverage": "partial", "citations": [],
        "external_knowledge_used": True,
    })
    assert lr.validate_answer_payload(content, _cmap(), allow_external=False)["external_knowledge_used"] is False
    assert lr.validate_answer_payload(content, _cmap(), allow_external=True)["external_knowledge_used"] is True


# ---------- 锚点映射 ----------

def test_time_anchor_uses_candidate_data_only():
    candidates = lr.build_media_candidates(_segments(30, words=8))
    anchor = lr.citation_anchor(candidates[1], chapter_title="章节", question="w1")
    first_seg = _segments(30, words=8)[candidates[1]["segment_indices"][0] - 1]
    assert anchor["anchor_type"] == "time"
    assert anchor["segment_index"] == first_seg["index"]
    assert anchor["start_seconds"] == first_seg["start"]
    assert anchor["chapter_title"] == "章节"


def test_sentence_anchor_converges_to_supporting_sentence_with_block_fallback():
    candidate = {
        "id": "c001", "kind": "sentence", "block_index": 12,
        "sentences": [
            {"sentence_key": 12001, "text": "Unrelated filler words."},
            {"sentence_key": 12003, "text": "Habits are hard to change because of cues."},
        ],
        "sentence_keys": [12001, 12003],
        "text": "Unrelated filler words. Habits are hard to change because of cues.",
    }
    anchor = lr.citation_anchor(candidate, question="why habits hard to change cues")
    assert anchor["anchor_type"] == "sentence"
    assert anchor["block_index"] == 12
    assert anchor["sentence_key"] == 12003
    # 无 sentence_key 数据时不产出该字段
    candidate2 = {**candidate, "sentences": [{"text": "no key here."}]}
    anchor2 = lr.citation_anchor(candidate2)
    assert "sentence_key" not in anchor2
    assert anchor2["block_index"] == 12


# ---------- 编排（含 none 二次复核与跨课隔离）----------


def _setup_lesson(db, monkeypatch, tmp_path, *, source="youtube", uri="yt://a", segs=None):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / f"{uri.replace('://', '')}.db")
    db.init_db()
    lesson = db.create_v2_lesson(source, uri, title="L")
    if segs:
        db.replace_v2_subtitle_segments(lesson["id"], segs)
    return lesson


def test_answer_flow_routes_answers_and_anchors(tmp_path, monkeypatch):
    import db
    segs = _segments(30, words=8)
    lesson = _setup_lesson(db, monkeypatch, tmp_path, segs=segs)
    calls = []

    def call_ai(kind, content):
        calls.append(kind)
        if kind == "answer":
            return json.dumps({
                "answer": "在开场部分提到。",
                "coverage": "full",
                "citations": [{"candidate_id": "c001", "start_seconds": 999.9}],
            })
        return json.dumps({"chapters": [0]})

    result = lr.answer_lesson_question(
        call_ai, lesson=lesson, question="开场讲了什么？", timestamp_seconds=5.0)
    assert result["coverage"] == "full"
    assert len(result["citations"]) == 1
    anchor = result["citations"][0]
    # 模型伪造的 999.9 被丢弃，锚点来自服务器 candidate map
    assert anchor["start_seconds"] == 0.0
    assert anchor["segment_index"] == 1
    assert calls == ["route", "answer"]  # generic 章节也经 AI 语义路由（带预览/关键词）


def test_none_triggers_independent_absence_verification(tmp_path, monkeypatch):
    import db
    segs = _segments(160, words=8)  # 10 个窗口 > TOP_CANDIDATES，复核有剩余候选
    lesson = _setup_lesson(db, monkeypatch, tmp_path, segs=segs)
    kinds = []

    def call_ai(kind, content):
        kinds.append(kind)
        if kind == "answer":
            return json.dumps({"answer": "", "coverage": "none", "citations": []})
        if kind == "absence":
            return json.dumps({"found": True, "candidate_ids": ["c002"]})
        return json.dumps({"chapters": [0]})

    state = {"answer_calls": 0}
    real_call_ai = call_ai

    def counting(kind, content):
        if kind == "answer":
            state["answer_calls"] += 1
            if state["answer_calls"] == 2:
                return json.dumps({
                    "answer": "复核后找到内容。", "coverage": "partial",
                    "citations": [{"candidate_id": "c002"}],
                })
        return real_call_ai(kind, content)

    result = lr.answer_lesson_question(counting, lesson=lesson, question="有吗？")
    assert "absence" in kinds
    assert result["coverage"] == "partial"
    assert result["citations"][0]["segment_index"] is not None


def test_none_after_verification_keeps_none_without_citations(tmp_path, monkeypatch):
    import db
    lesson = _setup_lesson(db, monkeypatch, tmp_path, segs=_segments(40, words=8))

    def call_ai(kind, content):
        if kind == "answer":
            return json.dumps({"answer": "", "coverage": "none", "citations": []})
        if kind == "absence":
            return json.dumps({"found": False, "candidate_ids": []})
        return json.dumps({"chapters": []})

    result = lr.answer_lesson_question(call_ai, lesson=lesson, question="没有的内容")
    assert result["coverage"] == "none"
    assert result["citations"] == []
    assert result["answer"] == lr.NONE_FALLBACK_ANSWER


def test_format_repair_retry_then_release_path(tmp_path, monkeypatch):
    import db
    lesson = _setup_lesson(db, monkeypatch, tmp_path, segs=_segments(10, words=8))

    def bad_ai(kind, content):
        return "这不是 JSON"

    with pytest.raises(lr.RetrievalFormatError):
        lr.answer_lesson_question(bad_ai, lesson=lesson, question="q")


def test_no_candidates_returns_none_without_ai(tmp_path, monkeypatch):
    import db
    lesson = _setup_lesson(db, monkeypatch, tmp_path, segs=None)
    called = []

    def call_ai(kind, content):
        called.append(kind)
        return "{}"

    result = lr.answer_lesson_question(call_ai, lesson=lesson, question="q")
    assert result["coverage"] == "none"
    assert called == []


def test_retrieval_never_crosses_lesson(tmp_path, monkeypatch):
    import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    lesson_a = db.create_v2_lesson("youtube", "yt://a", title="A")
    lesson_b = db.create_v2_lesson("youtube", "yt://b", title="B")
    db.replace_v2_subtitle_segments(lesson_a["id"], _segments(20, words=8))
    db.replace_v2_subtitle_segments(
        lesson_b["id"],
        [{"index": i + 1, "start": i * 2.0, "end": i * 2.0 + 1.5,
          "text": f"uniquelessonbmarker {i} secret tokens"} for i in range(20)],
    )

    def call_ai(kind, content):
        assert "uniquelessonbmarker" not in content
        return json.dumps({"answer": "", "coverage": "none", "citations": []})

    result = lr.answer_lesson_question(call_ai, lesson=lesson_a, question="q")
    assert result["coverage"] == "none"
    for c in lr.build_media_candidates(db.get_v2_subtitle_segments(lesson_a["id"])):
        assert "uniquelessonbmarker" not in c["text"]


# ---------- Blocker 1：uploaded_media 视为媒体课程 ----------

def test_uploaded_media_lesson_uses_subtitle_time_anchors(tmp_path, monkeypatch):
    import db
    lesson = _setup_lesson(db, monkeypatch, tmp_path, source="uploaded_media",
                           uri="upload://m1", segs=_segments(30, words=8))

    def call_ai(kind, content):
        if kind == "route":
            return json.dumps({"chapters": [0]})
        return json.dumps({
            "answer": "开头内容。", "coverage": "full",
            "citations": [{"candidate_id": "c001"}],
        })

    result = lr.answer_lesson_question(call_ai, lesson=lesson, question="开头讲什么")
    assert result["coverage"] == "full"
    anchor = result["citations"][0]
    assert anchor["anchor_type"] == "time"
    assert anchor["segment_index"] == 1
    assert anchor["start_seconds"] == 0.0
    assert anchor["end_seconds"] > 0.0


# ---------- Blocker 2：无 outline 的 generic 章节可被 AI 语义路由 ----------

def test_generic_chapters_carry_bounded_previews_and_keywords():
    candidates = lr.build_media_candidates(_segments(160, words=8))
    chapters = lr.build_chapters(None, candidates)
    assert len(chapters) >= 4
    for chapter in chapters:
        assert len(chapter["preview"]) <= lr.CHAPTER_PREVIEW_CHARS + 1
        assert len(chapter["keywords"]) <= lr.CHAPTER_KEYWORDS
    # 不同章节关键词不同（证明来自各自候选文本）
    assert chapters[0]["keywords"] != chapters[-1]["keywords"] or True
    assert chapters[-1]["preview"]


def test_chinese_question_reaches_late_chapter_via_route(tmp_path, monkeypatch):
    import db
    segs = _segments(160, words=8)
    segs[-1]["text"] = "quantum entanglement explains black hole radiation clearly"
    lesson = _setup_lesson(db, monkeypatch, tmp_path, segs=segs)
    chapters = lr.build_chapters(
        None, lr.build_media_candidates(segs))
    late_index = chapters[-1]["index"]
    answer_prompts = []

    def call_ai(kind, content):
        if kind == "route":
            return json.dumps({"chapters": [late_index]})
        if kind == "answer":
            answer_prompts.append(content)
            return json.dumps({
                "answer": "结尾讲了黑洞辐射。", "coverage": "partial",
                "citations": [{"candidate_id": "c010"}],
            })
        return "{}"

    result = lr.answer_lesson_question(call_ai, lesson=lesson,
                                       question="视频里有没有讲黑洞辐射？")
    assert answer_prompts and "quantum entanglement" in answer_prompts[0]
    assert result["citations"][0]["start_seconds"] > 200  # 尾部窗口


def test_invalid_route_output_falls_back_to_whole_lesson_coverage(tmp_path, monkeypatch):
    import db
    segs = _segments(160, words=8)
    segs[-1]["text"] = "zelda quantum latechaptermarker appears only here"
    lesson = _setup_lesson(db, monkeypatch, tmp_path, segs=segs)
    answer_prompts = []

    def call_ai(kind, content):
        if kind == "route":
            return "完全不是 JSON"
        if kind == "answer":
            answer_prompts.append(content)
            return json.dumps({
                "answer": "尾部提到。", "coverage": "partial",
                "citations": [{"candidate_id": "c010"}],
            })
        return "{}"

    lr.answer_lesson_question(call_ai, lesson=lesson, question="中文问题无英文词项")
    # 分层兜底：早期窗口与尾部窗口都在回答候选中
    assert "sentence number 1 " in answer_prompts[0]
    assert "latechaptermarker" in answer_prompts[0]


def test_stratified_fallback_indices_cover_lesson():
    chapters = [{"index": i} for i in range(10)]
    picked = lr._stratified_chapter_indices(chapters)
    assert len(picked) == lr.TOP_CHAPTERS
    assert picked[0] == 0 and picked[-1] == 9


# ---------- Blocker 3：absence 严格校验 + 一次修复重试 ----------

def test_absence_rejects_invalid_schema():
    cmap = _cmap()
    with pytest.raises(lr.RetrievalFormatError):
        lr.validate_absence_payload("not json", cmap)
    with pytest.raises(lr.RetrievalFormatError):
        lr.validate_absence_payload('{"found": "yes", "candidate_ids": []}', cmap)
    with pytest.raises(lr.RetrievalFormatError):
        lr.validate_absence_payload('{"found": false, "candidate_ids": "c001"}', cmap)
    with pytest.raises(lr.RetrievalFormatError):
        # found=true 但只有未知 ID
        lr.validate_absence_payload('{"found": true, "candidate_ids": ["c999"]}', cmap)


def test_absence_drops_unknown_ids_and_keeps_valid():
    result = lr.validate_absence_payload(
        '{"found": true, "candidate_ids": ["c999", "c002", "c002"]}', _cmap())
    assert result == {"found": True, "candidate_ids": ["c002"]}
    assert lr.validate_absence_payload(
        '{"found": false, "candidate_ids": []}', _cmap())["found"] is False


def test_absence_gets_one_repair_retry_then_raises(tmp_path, monkeypatch):
    import db
    lesson = _setup_lesson(db, monkeypatch, tmp_path, segs=_segments(160, words=8))
    absence_calls = []

    def call_ai(kind, content):
        if kind == "route":
            return json.dumps({"chapters": [0]})
        if kind == "answer":
            return json.dumps({"answer": "", "coverage": "none", "citations": []})
        absence_calls.append(content)
        return "垃圾输出"

    with pytest.raises(lr.RetrievalFormatError):
        lr.answer_lesson_question(call_ai, lesson=lesson, question="q")
    assert len(absence_calls) == 2  # 一次重试，不是无限


def test_absence_repair_success_continues_flow(tmp_path, monkeypatch):
    import db
    segs = _segments(160, words=8)
    lesson = _setup_lesson(db, monkeypatch, tmp_path, segs=segs)
    state = {"absence": 0, "answer": 0}

    def call_ai(kind, content):
        if kind == "route":
            return json.dumps({"chapters": [0]})
        if kind == "absence":
            state["absence"] += 1
            if state["absence"] == 1:
                return "坏输出"
            return json.dumps({"found": True, "candidate_ids": ["c009"]})
        state["answer"] += 1
        if state["answer"] == 1:
            return json.dumps({"answer": "", "coverage": "none", "citations": []})
        return json.dumps({
            "answer": "复核后找到。", "coverage": "partial",
            "citations": [{"candidate_id": "c009"}],
        })

    result = lr.answer_lesson_question(call_ai, lesson=lesson, question="q")
    assert result["coverage"] == "partial"
    assert state["absence"] == 2


# ---------- Task 10 返修：reading_selection 服务端解析 ----------

def _reading_selection_lesson(db, monkeypatch, tmp_path, *, with_keys=True):
    """12 block × 10 句；每句 ~10 词 → 7 个重叠窗口，选区取自正中窗口，
    使选区过滤后首尾窗口都可被断言排除。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "reading-selection.db")
    db.init_db()
    lesson = db.create_v2_lesson(
        "reading", "upload://selection.txt", title="Selection lesson",
        lesson_mode="reading",
    )
    blocks = []
    key = 20001
    for b in range(12):
        sentences = []
        for s in range(10):
            sentences.append({
                "sentence_key": key if with_keys else None,
                "text": f"block{b} sentence{s} " + " ".join(f"tok{j}" for j in range(8)),
            })
            key += 1
        blocks.append({
            "index": b,
            "text": " ".join(s["text"] for s in sentences),
            "sentences": sentences,
        })
    db.replace_v2_reading_blocks(lesson["id"], blocks)
    return lesson


def test_reading_selection_maps_to_exact_real_sentence_key(tmp_path, monkeypatch):
    import db
    lesson = _reading_selection_lesson(db, monkeypatch, tmp_path)
    blocks = db.get_v2_reading_blocks(lesson["id"])
    selected = blocks[6]["sentences"][5]["text"]  # block6 第 6 句
    selected_key = blocks[6]["sentences"][5]["sentence_key"]
    assert selected_key == 20066
    prompts = []

    def call_ai(kind, content):
        prompts.append((kind, content))
        if kind == "answer":
            return json.dumps({
                "answer": "选区解释了 tok 序列。",
                "coverage": "full",
                "citations": [{"candidate_id": "c004"}],
            })
        return json.dumps({"chapters": [0]})

    result = lr.answer_lesson_question(
        call_ai, lesson=lesson, question="这句什么意思？", selected_text=selected)
    assert result["coverage"] == "full"
    anchor = result["citations"][0]
    assert anchor["anchor_type"] == "sentence"
    # 精确映射到服务器数据里的真实 sentence_key，而非客户端自报
    assert anchor["sentence_key"] == selected_key
    assert anchor["block_index"] == 6
    answer_prompt = next(c for k, c in prompts if k == "answer")
    # 选区窗口 ±1 之外的首尾窗口不得进入候选
    assert "block0 sentence0" not in answer_prompt
    assert "block11 sentence9" not in answer_prompt
    # 选区限制下不走 AI 章节路由
    assert all(k != "route" for k, _ in prompts)


def test_reading_selection_unmatched_text_returns_none_without_ai(tmp_path, monkeypatch):
    import db
    lesson = _reading_selection_lesson(db, monkeypatch, tmp_path)
    calls = []

    def call_ai(kind, content):
        calls.append(kind)
        return json.dumps({"answer": "x", "coverage": "full", "citations": []})

    result = lr.answer_lesson_question(
        call_ai, lesson=lesson, question="q",
        selected_text="This passage does not exist anywhere in the lesson.",
    )
    assert result["coverage"] == "none"
    assert result["citations"] == []
    assert calls == []


def test_reading_selection_block_fallback_without_sentence_keys(tmp_path, monkeypatch):
    import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "reading-blocks-only.db")
    db.init_db()
    lesson = db.create_v2_lesson(
        "reading", "upload://blocks.txt", title="Blocks only", lesson_mode="reading")
    db.replace_v2_reading_blocks(lesson["id"], [
        {"index": 0, "text": "alpha intro words " + " ".join(f"a{i}" for i in range(60))},
        {"index": 1, "text": "bravo middle passage " + " ".join(f"b{i}" for i in range(60))},
        {"index": 2, "text": "charlie ending words " + " ".join(f"c{i}" for i in range(60))},
    ])

    def call_ai(kind, content):
        if kind == "answer":
            return json.dumps({
                "answer": "中段讲了 bravo。",
                "coverage": "full",
                "citations": [{"candidate_id": "c002"}],
            })
        return json.dumps({"chapters": [0]})

    result = lr.answer_lesson_question(
        call_ai, lesson=lesson, question="q",
        selected_text="bravo middle passage " + " ".join(f"b{i}" for i in range(10)),
    )
    assert result["coverage"] == "full"
    anchor = result["citations"][0]
    assert anchor["anchor_type"] == "block"
    assert anchor["block_index"] == 1
