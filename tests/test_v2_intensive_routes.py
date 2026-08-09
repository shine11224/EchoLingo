import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from fastapi.testclient import TestClient


def test_reading_split_recovers_missing_punctuation_at_capitals_but_not_i():
    from webapp.services.v2_intensive import _split_reading_text

    text = (
        "But people also used the river for fishing, as the water then was relatively clean, "
        "and they would also go on boat trips up and down the river just for pleasure, as a "
        "relaxing escape from the noise and bustle of the city streets But as industries "
        "developed and populations increased city rivers suffered The rising number of people "
        "meant there was a huge increase in the amount of sewage discharged into the rivers "
        "Rivers had always been used for this purpose, but when the number of inhabitants was "
        "so small, that wasn't such a problem."
    )

    assert _split_reading_text(text) == [
        "But people also used the river for fishing, as the water then was relatively clean, "
        "and they would also go on boat trips up and down the river just for pleasure, as a "
        "relaxing escape from the noise and bustle of the city streets",
        "But as industries developed and populations increased city rivers suffered",
        "The rising number of people meant there was a huge increase in the amount of sewage "
        "discharged into the rivers",
        "Rivers had always been used for this purpose, but when the number of inhabitants was "
        "so small, that wasn't such a problem.",
    ]
    assert _split_reading_text(
        "People often say that learning takes time and I agree with them Today we practise."
    ) == [
        "People often say that learning takes time and I agree with them",
        "Today we practise.",
    ]
    assert _split_reading_text(
        "The long journey continued for many hours before New York finally appeared."
    ) == [
        "The long journey continued for many hours before New York finally appeared."
    ]


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
        },
    )

    assert result.status_code == 200
    prompt = calls[0]["messages"][0]["content"]
    assert "重点词汇：consolidate, schemas" in prompt
    assert "多个重点词彼此适配时，尽量全部覆盖" in prompt


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
    assert "data.example_sentence || data.corrected" in page.text
    assert "action: isExample ? 'example' : 'correct'" in page.text
    assert "user_answer: userAnswer" in page.text
    assert "submitPractice" in page.text
    assert "/api/phonetics" in page.text
    assert "/api/oral-analysis" in page.text
    assert "/api/hint" in page.text
    assert "/api/practice" in page.text


def test_mastered_word_is_not_highlighted_in_future_intensive_lessons(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
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
    assert "first" not in response.json()["sentences"][0]["highlighted_words"]


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


def test_mastered_word_is_not_highlighted_in_future_intensive_lessons(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
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
    highlighted_words = response.json()["sentences"][0]["highlighted_words"]
    assert "first" not in highlighted_words
    assert "sentence" in highlighted_words
