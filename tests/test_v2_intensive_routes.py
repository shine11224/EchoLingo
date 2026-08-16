import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from fastapi.testclient import TestClient


def test_capital_split_recovers_missing_punctuation_but_keeps_proper_nouns():
    from analyzer import SentenceAnalyzer

    split = SentenceAnalyzer._split_capital_boundaries
    text = (
        "But people also used the river for fishing, as the water then was relatively clean, "
        "and they would also go on boat trips up and down the river just for pleasure, as a "
        "relaxing escape from the noise and bustle of the city streets But as industries "
        "developed and populations increased city rivers suffered The rising number of people "
        "meant there was a huge increase in the amount of sewage discharged into the rivers "
        "Rivers had always been used for this purpose, but when the number of inhabitants was "
        "so small, that wasn't such a problem."
    )

    assert split(text) == [
        "But people also used the river for fishing, as the water then was relatively clean, "
        "and they would also go on boat trips up and down the river just for pleasure, as a "
        "relaxing escape from the noise and bustle of the city streets.",
        "But as industries developed and populations increased city rivers suffered.",
        "The rising number of people meant there was a huge increase in the amount of sewage "
        "discharged into the rivers.",
        "Rivers had always been used for this purpose, but when the number of inhabitants was "
        "so small, that wasn't such a problem.",
    ]
    # "I" 句中恒大写，不构成句界
    assert split(
        "People often say that learning takes time and I agree with them Today we practise."
    ) == [
        "People often say that learning takes time and I agree with them.",
        "Today we practise.",
    ]
    # New York 连续大写专名不切
    assert split(
        "The long journey continued for many hours before New York finally appeared."
    ) == [
        "The long journey continued for many hours before New York finally appeared."
    ]
    # the Swedish brand：冠词后形容词性专名不切
    assert split(
        "One firm changed the market and the Swedish brand grew faster than the rest today."
    ) == [
        "One firm changed the market and the Swedish brand grew faster than the rest today."
    ]


def test_capital_split_covers_proper_noun_followed_by_function_word():
    """ASR 丢标点 + 前词是专名：They/This 等句中恒小写功能词大写仍判句界。

    2026-08-14 云端 lesson 55（20T2S4）实测粘句："Oatly They" / "services Companies"。
    """
    from analyzer import SentenceAnalyzer

    split = SentenceAnalyzer._split_capital_boundaries
    glued = (
        "Now there are many brands available but one company which had early success "
        "was the Swedish brand Oatly They attracted a lot of attention with a media campaign "
        "which used provocation as a way of getting their message across effectively"
    )
    assert split(glued) == [
        "Now there are many brands available but one company which had early success "
        "was the Swedish brand Oatly.",
        "They attracted a lot of attention with a media campaign which used provocation "
        "as a way of getting their message across effectively",
    ]

    glued2 = (
        "In return for free samples many influencers will post content about a product "
        "although there are influencers with hundreds of thousands of followers who can "
        "command large fees for their services Companies which sell vegan produce were pioneers"
    )
    assert split(glued2) == [
        "In return for free samples many influencers will post content about a product "
        "although there are influencers with hundreds of thousands of followers who can "
        "command large fees for their services.",
        "Companies which sell vegan produce were pioneers",
    ]
    # 专名连用 + 功能词：United Kingdom 不切，They 处切
    assert split(
        "The delegation from the trade council visited the United Kingdom They returned home satisfied."
    ) == [
        "The delegation from the trade council visited the United Kingdom.",
        "They returned home satisfied.",
    ]
    # 幂等：已切过的文本（句末专名）二次应用不再切出 "Oatly." 碎片
    once = split(glued)
    assert SentenceAnalyzer._split_capital_boundaries(once[0]) == [once[0]]
    assert SentenceAnalyzer._split_capital_boundaries(once[1]) == [once[1]]


def test_translation_unit_split_applies_capital_rule_with_interpolated_timing():
    from webapp.services.v2_translation import _split_source_segments

    glued = (
        "Now there are many brands available but one company which had early success "
        "was the Swedish brand Oatly They attracted a lot of attention with a media campaign"
    )
    pieces = _split_source_segments([
        {"index": 30, "start": 276.8, "end": 296.5, "text": glued},
    ])

    assert [p["text"] for p in pieces] == [
        "Now there are many brands available but one company which had early success "
        "was the Swedish brand Oatly.",
        "They attracted a lot of attention with a media campaign",
    ]
    # 时间轴插值：边界连续、整体不超界
    assert pieces[0]["start"] == 276.8
    assert pieces[0]["end"] == pieces[1]["start"]
    assert pieces[1]["end"] == 296.5


def test_oral_analysis_is_persisted_and_reused_by_intensive_and_sentence_library(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.fastapi_routes import ai

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    calls = []
    analysis = {
        "pattern_name": "take ... into account",
        "cefr_level": "B2",
        "function_cn": "列举评估因素。",
        "ielts_reuse": "可用于解释评价标准。",
        "template": "X takes A, B, and C into account.",
        "template_example": "The review takes cost and quality into account.",
    }

    def create(**kwargs):
        calls.append(kwargs)
        message = SimpleNamespace(content=json.dumps(analysis, ensure_ascii=False))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(
        ai.ai_config,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:shared-analysis",
        title="Shared Analysis",
        lesson_mode="reading",
    )
    db.replace_v2_reading_blocks(
        lesson["id"],
        [{"index": 0, "text": "The review takes cost and quality into account."}],
    )
    saved = db.save_v2_phase_b_sentence(
        lesson["id"], -10001, 0, 0,
        "The review takes cost and quality into account.",
    )

    first = client.post(
        "/api/oral-analysis",
        json={"english": saved["text"], "sentence_id": saved["sentence_id"], "persist": True},
    )
    second = client.post(
        "/api/oral-analysis",
        json={"english": saved["text"], "sentence_id": saved["sentence_id"], "persist": True},
    )

    assert first.status_code == 200
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert len(calls) == 1
    stored = db.get_v2_sentence_pattern(saved["sentence_id"])
    assert stored["analysis"]["pattern_name"] == "take ... into account"
    assert stored["pattern_template"] == analysis["template"]
    intensive = client.get(f"/api/v2/lessons/{lesson['id']}/intensive").json()
    assert intensive["sentences"][0]["sentence_id"] == saved["sentence_id"]
    assert intensive["sentences"][0]["oral_analysis"]["cefr_level"] == "B2"
    library = client.get("/api/v2/lessons/sentence-review").json()["sentences"][0]
    assert library["pattern"]["analysis"]["template"] == analysis["template"]

    db.save_v2_sentence_pattern_scenario(
        saved["sentence_id"], "旧句式对应的迁移提示。"
    )
    analysis["template"] = "X considers A, B, and C."
    refreshed = client.post(
        "/api/oral-analysis",
        json={
            "english": saved["text"],
            "sentence_id": saved["sentence_id"],
            "persist": True,
            "force": True,
        },
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["cached"] is False
    assert len(calls) == 2
    updated = db.get_v2_sentence_pattern(saved["sentence_id"])
    assert updated["pattern_template"] == analysis["template"]
    assert updated["scenario_cn"] == ""


def test_hint_prompt_prioritizes_all_highlighted_vocabulary(monkeypatch):
    from fastapi_server import create_app
    from webapp.fastapi_routes import ai

    calls = []
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="团队需要整合这些数据模式。"))]
    )

    def create(**kwargs):
        calls.append(kwargs)
        return response

    monkeypatch.setattr(
        ai.ai_config,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )

    result = TestClient(create_app()).post(
        "/api/hint",
        json={
            "english": "We consolidate the schemas.",
            "pattern_template": "We need to ...",
            "vocab": ["consolidate", "schemas"],
            "previous_hints": [
                "团队需要整合这些数据模式。",
                "学校应该统一这套登记规则。",
            ],
        },
    )

    assert result.status_code == 200
    prompt = calls[0]["messages"][0]["content"]
    assert "重点词汇：consolidate, schemas" in prompt
    assert "多个重点词彼此适配时，尽量全部覆盖" in prompt
    assert "- 团队需要整合这些数据模式。" in prompt
    assert "- 学校应该统一这套登记规则。" in prompt
    assert "不得与其中任何一句相同或近义改写" in prompt


def test_listening_retell_analysis_compares_meaning_and_blind_spots(monkeypatch):
    from fastapi_server import create_app
    from webapp.fastapi_routes import ai

    calls = []
    payload = {
        "overall_cn": "核心意思保留，但漏掉评价维度。",
        "meaning_preserved": True,
        "accuracy_score": 72,
        "matched_content": ["核心动作"],
        "missed_content": ["评价维度"],
        "knowledge_gaps": ["take into account 搭配"],
        "listening_blind_spots": ["弱读"],
        "next_step_cn": "关注列举内容。",
    }

    def create(**kwargs):
        calls.append(kwargs)
        message = SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(
        ai.ai_config,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    response = TestClient(create_app()).post(
        "/api/listening-retell-analysis",
        json={
            "english": "The assessment takes fluency and accuracy into account.",
            "retelling": "The assessment checks fluency.",
        },
    )

    assert response.status_code == 200
    assert response.json()["accuracy_score"] == 72
    prompt = calls[0]["messages"][0]["content"]
    assert "The assessment takes fluency and accuracy into account." in prompt
    assert "The assessment checks fluency." in prompt
    assert "不把合理同义改写误判为错误" in prompt
    assert "语言知识盲区" in prompt


def test_listening_retell_analysis_retries_one_empty_ai_response(monkeypatch):
    from fastapi_server import create_app
    from webapp.fastapi_routes import ai

    payload = {
        "overall_cn": "第二次返回成功。",
        "meaning_preserved": True,
        "accuracy_score": 88,
        "matched_content": ["核心意思"],
        "missed_content": [],
        "knowledge_gaps": [],
        "listening_blind_spots": [],
        "next_step_cn": "继续保持。",
    }
    contents = iter(["", json.dumps(payload, ensure_ascii=False)])
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        message = SimpleNamespace(content=next(contents))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(
        ai.ai_config,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    response = TestClient(create_app()).post(
        "/api/listening-retell-analysis",
        json={"english": "Standard sentence.", "retelling": "My retelling."},
    )

    assert response.status_code == 200
    assert response.json()["accuracy_score"] == 88
    assert len(calls) == 2


def test_ai_ipa_with_base_does_not_require_the_optional_nobase_prompt(monkeypatch):
    from webapp.fastapi_routes import ai

    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content='{"results":[{"index":1,"natural_ipa":"həˈloʊ wɜrld ↘"}]}'
                )
            )
        ]
    )
    completions = SimpleNamespace(create=lambda **_: response)
    monkeypatch.setattr(
        ai.ai_config,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )

    result = ai._do_phonetics({
        "sentences": ["Hello world."],
        "mode": "ai_ipa",
        "base": True,
    })

    assert result["results"][0]["natural"] == "həˈloʊ wɜrld ↘"
    assert result["results"][0]["source"] == "ai_ipa"


def test_interactive_ai_ipa_is_word_aligned_fast_and_cached(monkeypatch):
    from webapp.fastapi_routes import ai

    calls = []
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="""{"results":[{"index":1,"words":[
                        [0,"aɪ",false,false,false,""],
                        [1,"kən",true,true,false,""],
                        [2,"æsk",false,false,true,"↘"]
                    ]}]}"""
                )
            )
        ]
    )

    def create(**kwargs):
        calls.append(kwargs)
        return response

    monkeypatch.setattr(
        ai.ai_config,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    ai._IPA_PROOFREAD_CACHE.clear()
    request = {
        "sentences": ["I can ask."],
        "mode": "ai_ipa",
        "structured": True,
        "base_words": [[
            {"word": "I", "ipa": "aɪ"},
            {"word": "can", "ipa": "kæn"},
            {"word": "ask", "ipa": "æsk"},
        ]],
    }

    first = ai._do_phonetics(request)["results"][0]
    second = ai._do_phonetics(request)["results"][0]

    assert first["source"] == "ai_ipa"
    assert [item["word"] for item in first["word_annotations"]] == ["I", "can", "ask"]
    assert [item["ipa"] for item in first["word_annotations"]] == ["aɪ", "kən", "æsk"]
    assert first["word_annotations"][1]["link_to_next"] is True
    assert first["word_annotations"][2]["punctuation_after"] == "."
    assert second["cached"] is True
    assert len(calls) == 1
    assert calls[0]["max_tokens"] == 240
    assert calls[0]["timeout"] == 45
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}


def test_intensive_document_contains_all_sentences_and_saved_tags(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.services import v2_vocab

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(
        v2_vocab,
        "load_v2_wordlist_index",
        lambda: {"quixotic": {"level": "ielts"}, "zephyr": {"level": "ielts"}},
    )
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:intensive-test",
        title="Intensive Test",
        lesson_mode="reading",
    )
    db.replace_v2_reading_blocks(
        lesson["id"],
        [
            {"index": 0, "text": "Quixotic sentence. Second sentence!"},
            {"index": 1, "text": "Zephyr sentence?"},
        ],
    )
    saved = db.save_v2_phase_b_sentence(
        lesson_id=lesson["id"],
        segment_index=-10001,
        start_seconds=0,
        end_seconds=0,
        text="Quixotic sentence.",
    )
    db.replace_v2_sentence_tags(
        saved["sentence_id"],
        [{"category": "structure", "name": "长难句"}],
    )
    response = client.get(f"/api/v2/lessons/{lesson['id']}/intensive")

    assert response.status_code == 200
    data = response.json()
    assert data["lesson"]["id"] == lesson["id"]
    assert [item["text"] for item in data["sentences"]] == [
        "Quixotic sentence.",
        "Second sentence!",
        "Zephyr sentence?",
    ]
    assert data["sentences"][0]["key"] == -10001
    assert data["sentences"][0]["saved"] is True
    assert data["sentences"][0]["tags"][0]["name"] == "长难句"
    assert data["sentences"][0]["highlighted_words"] == ["quixotic"]
    assert data["sentences"][1]["saved"] is False
    assert data["sentences"][1]["highlighted_words"] == []
    assert data["sentences"][2]["highlighted_words"] == ["zephyr"]

    page = client.get(f"/workspace/{lesson['id']}/intensive")
    assert page.status_code == 200
    assert 'id="intensive-sentence-list"' in page.text
    assert 'data-filter="saved"' in page.text
    assert 'data-filter="untagged"' in page.text
    assert 'data-filter="vocabulary"' in page.text
    assert 'id="count-vocabulary"' in page.text
    assert 'id="intensive-word-card"' in page.text
    assert 'id="tag-filter-list"' in page.text
    assert "analyzeIntensiveWord" in page.text
    assert "openIntensiveWordCard" in page.text
    assert "hasDeepWordAnalysis" in page.text
    assert "const hasSessionCache = wordAnalysisCache.has(cacheKey)" in page.text
    assert "if (hasSessionCache || hasDeepWordAnalysis(cached))" in page.text
    assert "toggleIntensiveWordSaved" in page.text
    assert "data-toggle-word-save" in page.text
    assert "addIntensiveWordToReview" in page.text
    assert "data-add-word-review" in page.text
    assert "source: 'intensive'" in page.text
    assert "＋ 加入复习本" in page.text
    assert "data-speak-example" in page.text
    assert "data-save-example" in page.text
    assert "speakIntensiveExample" in page.text
    assert "saveIntensiveExample" in page.text
    assert "/api/v2/lessons/sentence-review/manual" in page.text
    assert "async function syncIntensiveHighlightedWords" in page.text
    assert "await syncIntensiveHighlightedWords();" in page.text
    assert "highlighted-words/sync" in page.text
    assert "sentence.translation" in page.text
    assert 'class="inline-translation"' in page.text
    assert "/api/known-words" in page.text
    assert "data-speak-word" in page.text
    assert '/static/natural-tts.js' in page.text
    assert "NaturalTTS.speak" in page.text
    assert "playIntensiveWord(speakWord.dataset.speakWord)" in page.text
    assert "function playIntensiveWord(word)" in page.text
    assert "sentence?.aligned_words?.find" not in page.text
    assert "NaturalTTS.speak(word" in page.text
    assert "playIntensiveSentence" in page.text
    assert "hasOriginalMedia" in page.text
    assert "initIntensiveYouTube" in page.text
    assert "new YT.Player('intensive-youtube-host'" in page.text
    assert "intensiveYouTubePlayer.seekTo(range.start, true)" in page.text
    assert "intensiveYouTubePlayer.playVideo()" in page.text
    assert "intensiveYouTubePlayer?.pauseVideo()" in page.text
    assert "mode: 'rule'" in page.text
    assert "word_annotations" in page.text
    assert "structured: false" in page.text
    assert "已复用校对缓存" in page.text
    assert "AI 优化音标" not in page.text
    assert "data-load-pronunciation" not in page.text
    assert "MFA 原声对齐" in page.text
    assert "isLikelyPronunciationLink" not in page.text
    assert 'class="aligned-pronunciation"' in page.text
    assert 'data-view-mode="card"' in page.text
    assert "navigateSentenceCard" in page.text
    assert 'data-toggle-tag-editor="${sentence.key}"' in page.text
    assert "openTagEditorKeys" in page.text
    assert 'data-play-sentence="${sentence.key}"' in page.text
    assert 'data-sentence-speed-select="${sentence.key}"' in page.text
    assert "/api/v2/lessons/${LESSON_ID}/word" in page.text
    assert "saveSentenceTags" in page.text
    assert "/api/v2/lessons/${LESSON_ID}/intensive" in page.text
    assert "const filterTags = new Map();" in page.text
    assert "tagsForCategory" in page.text
    assert "updateTagNameOptions" in page.text
    assert "data-tag-custom" in page.text
    assert "data-practice-mic" in page.text
    assert "MediaRecorder" in page.text
    assert "/api/transcribe" in page.text
    assert 'class="v1-study-stack"' in page.text
    assert "loadPronunciationGuide" in page.text
    assert "generateOralAnalysis" in page.text
    assert "generatePracticeHint" in page.text
    assert "vocab: practiceWords(sentence)" in page.text
    assert "previous_hints: previousHints" in page.text
    assert "practiceHintHistory" in page.text
    assert "button.textContent = '💡 换一换'" in page.text
    assert "if (input) input.value = '';" in page.text
    assert "renderPracticeSentence" in page.text
    assert "linkifyEnglish(text, key)" in page.text
    assert 'data-speak-example="${encoded}"' in page.text
    assert "data.example_sentence || data.corrected" in page.text
    assert "isExample ? 'practice_example' : 'practice_evaluation'" in page.text
    assert "action: isExample ? 'example' : 'correct'" in page.text
    assert "submitPractice" in page.text
    assert "/api/phonetics" in page.text
    assert "/api/oral-analysis" in page.text
    assert "/api/hint" in page.text
    assert "/api/practice" in page.text


def test_reading_intensive_uses_timed_sentence_units_with_translation(tmp_path, monkeypatch):
    """TTS 已生成的 reading 课程：精读句单元必须与翻译/TTS 同源（block.sentences），

    不能用启发式重切——重切会把 "(Bornmann and Mutz, 2015)" 在数字前断开，
    产生从未被翻译的碎片单元导致中文丢失。"""
    import db
    from fastapi_server import create_app
    from webapp.services import v2_vocab

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(v2_vocab, "load_v2_wordlist_index", lambda: {})
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="reading_upload",
        source_url="manual:intensive-timed",
        title="Intensive Timed",
        lesson_mode="reading",
    )
    full_sentence = (
        "The rise of preprint servers and open-access repositories has expanded "
        "scientific discourse, fostering cross-disciplinary discovery (Bornmann and Mutz, 2015), "
        "but also burdening researchers with reconciling scattered findings."
    )
    db.replace_v2_reading_blocks(
        lesson["id"],
        [
            {
                "index": 0,
                "text": full_sentence,
                "start_seconds": 0.0,
                "end_seconds": 12.5,
                "sentences": [
                    {
                        "index": 0,
                        "text": full_sentence,
                        "start_seconds": 0.0,
                        "end_seconds": 12.5,
                    }
                ],
            }
        ],
    )
    db.upsert_v2_sentence(full_sentence, translation="预印本服务器的兴起拓展了科学交流。")

    response = client.get(f"/api/v2/lessons/{lesson['id']}/intensive")
    assert response.status_code == 200
    data = response.json()
    assert [item["text"] for item in data["sentences"]] == [full_sentence]
    assert data["sentences"][0]["translation"] == "预印本服务器的兴起拓展了科学交流。"
    assert data["sentences"][0]["start_seconds"] == 0.0
    assert data["sentences"][0]["end_seconds"] == 12.5



def test_mastered_word_is_not_highlighted_in_future_intensive_lessons(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.services import v2_vocab

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    # 词表索引与本测试无关：私有环境存在用户 BNC 词表、公开干净检出没有，须隔离
    monkeypatch.setattr(v2_vocab, "load_v2_wordlist_index", lambda: {})
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:mastered-intensive",
        title="Mastered Intensive",
        lesson_mode="reading",
    )
    db.replace_v2_reading_blocks(
        lesson["id"],
        [{"index": 0, "text": "First sentence."}],
    )
    db.activate_word_review(
        word="first",
        source="manual",
        lemma="first",
        display_text="first",
        target_type="word",
    )
    db.set_review_word_lifecycle("first", mastered=True)

    response = client.get(f"/api/v2/lessons/{lesson['id']}/intensive")

    assert response.status_code == 200
    assert response.json()["sentences"][0]["highlighted_words"] == []


def test_media_intensive_reuses_playback_sentence_units_with_contiguous_timing(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="local_audio",
        source_url="manual:segmentation-test",
        title="Segmented listening",
        lesson_mode="reading",
    )
    db.replace_v2_subtitle_segments(
        lesson["id"],
        [
            {"index": 7, "start": 10.0, "end": 12.0, "text": "First"},
            {"index": 8, "start": 12.0, "end": 14.0, "text": "sentence."},
            {"index": 9, "start": 14.0, "end": 16.0, "text": "Second sentence! Final zorchword"},
        ],
    )
    db.upsert_word("zorchword", "2026-07-27", level="v2", analysis={"basic_meaning": "手动收藏词"})
    db.save_v2_lesson_word(lesson["id"], "zorchword", "Final zorchword")
    db.upsert_v2_sentence("First sentence.", translation="第一句话。")
    saved = db.save_v2_phase_b_sentence(
        lesson_id=lesson["id"],
        segment_index=0,
        start_seconds=10.0,
        end_seconds=14.0,
        text="First sentence.",
    )
    db.replace_v2_sentence_tags(
        saved["sentence_id"],
        [{"category": "structure", "name": "完整句"}],
    )

    response = client.get(f"/api/v2/lessons/{lesson['id']}/intensive")

    assert response.status_code == 200
    document = response.json()
    sentences = document["sentences"]
    assert [item["text"] for item in sentences] == [
        "First sentence.",
        "Second sentence!",
        "Final zorchword",
    ]
    assert [item["key"] for item in sentences] == [0, 1, 2]
    assert sentences[0]["saved"] is True
    assert sentences[0]["translation"] == "第一句话。"
    assert sentences[0]["tags"][0]["name"] == "完整句"
    assert sentences[2]["highlighted_words"] == ["zorchword"]
    assert document["lesson_words"] == ["zorchword"]
    assert sentences[0]["start_seconds"] == 10.0
    assert sentences[0]["end_seconds"] == sentences[1]["start_seconds"]
    assert sentences[1]["end_seconds"] == sentences[2]["start_seconds"]
    assert sentences[2]["end_seconds"] == 16.0


def test_intensive_export_writes_homepage_html_and_lesson_metadata(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.fastapi_routes import output as output_routes
    from webapp.services import v2_intensive_export

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(v2_intensive_export, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(output_routes, "OUTPUT_DIR", tmp_path / "output")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:intensive-export-test",
        title="Persistent Intensive",
        lesson_mode="reading",
    )
    db.replace_v2_reading_blocks(
        lesson["id"],
        [{"index": 0, "text": "First sentence. Second sentence!"}],
    )

    response = client.post(f"/api/v2/lessons/{lesson['id']}/intensive-export")

    assert response.status_code == 200
    data = response.json()
    assert data["workspace_url"] == f"/workspace/{lesson['id']}/intensive"
    assert data["export_url"] == f"/output/v2-intensive-{lesson['id']}.html?download=1"
    assert data["download_url"] == data["export_url"]
    assert data["sentence_count"] == 2
    exported = tmp_path / "output" / f"v2-intensive-{lesson['id']}.html"
    assert exported.is_file()
    html = exported.read_text(encoding="utf-8")
    assert f"const LESSON_ID = {lesson['id']};" in html
    assert f'href="/workspace/{lesson["id"]}"' in html

    legacy = client.get(
        f"/output/v2-intensive-{lesson['id']}.html",
        follow_redirects=False,
    )
    assert legacy.status_code == 307
    assert legacy.headers["location"] == f"/workspace/{lesson['id']}/intensive"
    assert legacy.headers["cache-control"] == "no-store"

    download = client.get(data["download_url"])
    assert download.status_code == 200
    assert "attachment" in download.headers["content-disposition"]

    live = client.get(data["workspace_url"])
    assert live.status_code == 200
    assert live.headers["cache-control"] == "no-store, max-age=0"

    homepage_lessons = client.get("/api/lessons").json()
    assert all(item["filename"] != exported.name for item in homepage_lessons)
