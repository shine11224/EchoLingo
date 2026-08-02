import os
import sys
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


def test_vocab_story_uses_fast_chat_model_on_official_deepseek(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.fastapi_routes import vocab

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(vocab.ai_config, "AI_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setattr(vocab.ai_config, "AI_MODEL", "deepseek-v4-pro")
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        content = json.dumps({
            "story_content": "A curator used an analogy to explain an obscure pattern.",
            "used_words": [],
            "review_questions": [],
        })
        choice = SimpleNamespace(
            message=SimpleNamespace(content=content),
            finish_reason="stop",
        )
        return SimpleNamespace(choices=[choice])

    monkeypatch.setattr(
        vocab.ai_config,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    client = TestClient(create_app())

    response = client.post(
        "/api/vocab-story",
        json={"words": ["analogy", "obscure"], "learner_level": "B2", "force_new": True},
    )

    assert response.status_code == 200
    assert captured["model"] == "deepseek-chat"
    assert captured["max_tokens"] >= 2500
    assert captured["timeout"] <= 90
    assert response.json()["story"].startswith("A curator")
    history = client.get("/api/vocab-story/history")
    assert history.status_code == 200
    assert history.json()["total"] == 1
    assert history.json()["stories"][0]["words"] == ["analogy", "obscure"]
    assert history.json()["stories"][0]["learner_level"] == "B2"


def test_story_history_preserves_regenerated_versions_for_same_word_set(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    first = json.dumps({"story_content": "First story."})
    second = json.dumps({"story_content": "Second story."})

    db.save_story_history("2026-08-01|analogy", ["analogy"], first, "2026-08-01", learner_level="B1")
    db.save_story_history("2026-08-01|analogy", ["analogy"], second, "2026-08-01", learner_level="B2")

    history = db.list_story_history()
    assert len(history) == 2
    assert {item["story"] for item in history} == {first, second}
    assert {item["learner_level"] for item in history} == {"B1", "B2"}


def test_story_history_paginates_and_deletes_without_cache_resurrection(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    first = json.dumps({"story_content": "First story."})
    second = json.dumps({"story_content": "Second story."})
    db.save_story("cache-first", ["first"], first, "2026-08-01")
    db.save_story_history("cache-first", ["first"], first, "2026-08-01")
    db.save_story_history("cache-second", ["second"], second, "2026-08-01")
    client = TestClient(create_app())

    page = client.get("/api/vocab-story/history?page=2&page_size=1")
    assert page.status_code == 200
    assert page.json()["total"] == 2
    assert page.json()["page"] == 2
    assert page.json()["pages"] == 2
    assert len(page.json()["stories"]) == 1

    cached_row = next(item for item in db.list_story_history(limit=10) if item["cache_key"] == "cache-first")
    deleted = client.delete(f"/api/vocab-story/history/{cached_row['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert db.get_story("cache-first") is None
    db.init_db()
    assert all(item["id"] != cached_row["id"] for item in db.list_story_history(limit=10))
    assert client.delete(f"/api/vocab-story/history/{cached_row['id']}").status_code == 404


def test_vocab_log_exposes_legacy_youtube_original_audio_for_context(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.fastapi_routes import vocab

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    monkeypatch.setattr(vocab, "OUTPUT_DIR", output_dir)
    sentence = "An obscure role deserves a clear explanation."
    segments = json.dumps([{"text": sentence, "start": 4.25, "end": 7.5}])
    (output_dir / "legacy.html").write_text(
        f'<script>const segments = {segments}; const sourceType = "youtube"; '
        'const youtubeId = "video12345";</script>',
        encoding="utf-8",
    )
    db.init_db()
    db.upsert_lesson(
        "legacy.html", "Legacy Course", "youtube", "https://youtu.be/video12345",
        1, 8, "2026-08-01",
    )
    db.activate_word_review("obscure", source="manual")
    db.add_context("obscure", "Legacy Course", sentence)

    response = TestClient(create_app()).get("/vocab-log")

    assert response.status_code == 200
    assert response.json()["obscure"]["contexts"][0]["audio"] == {
        "kind": "youtube",
        "video_id": "video12345",
        "start": 4.25,
        "end": 7.5,
    }


def test_vocab_review_tags_persist_normalize_and_return_in_log(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    db.activate_word_review("obscure", source="manual")
    client = TestClient(create_app())

    response = client.patch(
        "/api/vocab-review/obscure/tags",
        json={"tags": ["面试", "表达", "面试", "x" * 21]},
    )

    assert response.status_code == 200
    assert response.json()["tags"] == ["面试", "表达"]
    assert client.get("/vocab-log").json()["obscure"]["tags"] == ["面试", "表达"]
    assert client.patch("/api/vocab-review/missing/tags", json={"tags": ["test"]}).status_code == 404


def test_vocab_story_reports_truncated_ai_output(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.fastapi_routes import vocab

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")

    def create(**_kwargs):
        choice = SimpleNamespace(
            message=SimpleNamespace(content=""),
            finish_reason="length",
        )
        return SimpleNamespace(choices=[choice])

    monkeypatch.setattr(
        vocab.ai_config,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    client = TestClient(create_app())

    response = client.post(
        "/api/vocab-story",
        json={"words": ["analogy"], "force_new": True},
    )

    assert response.status_code == 502
    assert "截断" in response.json()["error"]


def test_story_interaction_supports_word_lookup_and_cached_translation(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.fastapi_routes import vocab

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(
        vocab,
        "lookup_word_meaning",
        lambda word, allow_external_fallback=False: {
            "word": word.lower(),
            "meaning": "精心策划的",
            "phonetic": "kjʊˈreɪtɪd",
            "found": True,
            "source": "compiled",
        },
    )
    translated = []
    monkeypatch.setattr(
        vocab,
        "_translate_story_selection",
        lambda text: translated.append(text) or "她精心策划了这次展览。",
    )
    client = TestClient(create_app())

    lookup = client.get("/api/story-word-meaning/curated")
    saved = client.post(
        "/api/vocab-review/activate",
        json={"word": "curated", "source": "story", "meaning": "精心策划的"},
    )
    first = client.post("/api/story-translate", json={"text": "She curated the exhibition."})
    second = client.post("/api/story-translate", json={"text": "She curated the exhibition."})

    assert lookup.status_code == 200
    assert lookup.json()["meaning"] == "精心策划的"
    assert lookup.json()["in_review_book"] is False
    assert saved.status_code == 200
    assert saved.json()["source"] == "story"
    assert first.status_code == 200
    assert first.json()["translation"] == "她精心策划了这次展览。"
    assert second.json()["translation"] == first.json()["translation"]
    assert translated == ["She curated the exhibition."]


def test_story_chat_keeps_selection_and_recent_dialogue_context(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.fastapi_routes import vocab

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(vocab.ai_config, "AI_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setattr(vocab.ai_config, "AI_MODEL", "deepseek-v4-pro")
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        choice = SimpleNamespace(
            message=SimpleNamespace(content="这里强调的是策展人主动筛选内容。"),
            finish_reason="stop",
        )
        return SimpleNamespace(choices=[choice])

    monkeypatch.setattr(
        vocab.ai_config,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    client = TestClient(create_app())

    response = client.post(
        "/api/story-chat",
        json={
            "story": "Maya curated an exhibition.",
            "selection": "curated an exhibition",
            "message": "这里为什么用 curated？",
            "history": [
                {"role": "user", "content": "故事讲了什么？"},
                {"role": "assistant", "content": "讲了一次展览。"},
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["answer"].startswith("这里强调")
    assert captured["model"] == "deepseek-chat"
    assert any("curated an exhibition" in item["content"] for item in captured["messages"])
    assert any(item["content"] == "故事讲了什么？" for item in captured["messages"])


def test_manual_review_activation_excludes_unactivated_dictionary_words(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    today = "2026-07-24"
    db.upsert_word("background", today)
    db.upsert_word("background", today)

    response = client.post(
        "/api/vocab-review/activate",
        json={"word": "focused", "source": "manual", "meaning": "专注的"},
    )

    assert response.status_code == 200
    assert response.json()["in_review_book"] is True
    review_words = client.get("/vocab-log").json()
    assert set(review_words) == {"focused"}
    assert review_words["focused"]["review_source"] == "manual"
    assert review_words["focused"]["cached_analysis"]["basic_meaning"] == "专注的"
    rejected = client.post(
        "/api/vocab-review/activate",
        json={"target": "clicked", "source": "lookup"},
    )
    assert rejected.status_code == 400
    assert db.is_word_in_review("clicked") is False


def test_analyze_word_persists_deep_analysis_without_incrementing_review_count(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.fastapi_routes import ai

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    db.activate_word_review(
        "decay",
        source="manual",
        analysis={"basic_meaning": "腐烂"},
    )
    monkeypatch.setattr(
        ai.dict_service,
        "lookup_all",
        lambda _word: {"oald": "", "longman": "", "vocab": ""},
    )

    generated = {
        "en_definition": "the gradual destruction of something",
        "collocations": ["tooth decay"],
        "examples": ["The fruit began to decay."],
    }

    def create(**_kwargs):
        message = SimpleNamespace(content=json.dumps(generated, ensure_ascii=False))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(
        ai.ai_config,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    client = TestClient(create_app())

    response = client.post(
        "/analyze-word",
        json={"word": "decay", "sentence": "", "target_type": "word"},
    )

    assert response.status_code == 200
    assert response.json()["basic_meaning"] == "腐烂"
    assert response.json()["en_definition"] == generated["en_definition"]
    stored = client.get("/vocab-log").json()["decay"]
    assert stored["count"] == 1
    assert stored["cached_analysis"]["basic_meaning"] == "腐烂"
    assert stored["cached_analysis"]["collocations"] == ["tooth decay"]


def test_vocab_review_supports_word_phrase_familiarity_archive_and_mastery(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())

    word = client.post(
        "/api/vocab-review/activate",
        json={
            "word": "uses",
            "lemma": "use",
            "display_text": "uses",
            "target_type": "word",
            "source": "manual",
        },
    )
    phrase = client.post(
        "/api/vocab-review/activate",
        json={
            "word": "  Take   Off ",
            "target_type": "phrase",
            "source": "manual",
        },
    )
    assert word.status_code == 200
    assert word.json()["word"] == "use"
    assert word.json()["display_text"] == "uses"
    assert phrase.json()["word"] == "take off"
    assert phrase.json()["target_type"] == "phrase"

    familiar = client.patch(
        "/api/vocab-review/use/familiarity",
        json={"familiarity": "fuzzy"},
    )
    assert familiar.status_code == 200
    assert familiar.json()["familiarity"] == "fuzzy"

    archived = client.patch(
        "/api/vocab-review/use/lifecycle",
        json={"archived": True},
    )
    assert archived.status_code == 200
    assert archived.json()["archived"] is True
    assert "use" not in client.get("/vocab-log").json()
    assert client.get("/vocab-log?include_archived=true").json()["use"]["archived"] is True
    assert "use" in db.get_review_word_set()

    mastered = client.patch(
        "/api/vocab-review/use/lifecycle",
        json={"mastered": True},
    )
    assert mastered.status_code == 200
    assert mastered.json()["mastered"] is True
    assert db.is_word_in_review("uses", lemma="use") is False
    assert db.get_mastered_review_targets() == {"use"}
    assert "use" not in db.get_review_word_set()

    restored = client.patch(
        "/api/vocab-review/use/lifecycle",
        json={"mastered": False, "archived": False},
    )
    assert restored.status_code == 200
    assert restored.json()["mastered"] is False
    assert db.is_word_in_review("use") is True


def test_successful_sentence_correction_does_not_implicitly_activate_vocabulary(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.fastapi_routes import ai

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")

    captured = {}

    def create(**kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        message = SimpleNamespace(content=json.dumps({
            "verdict": "accepted",
            "target_used_correctly": True,
            "key_issue": "",
            "explanation": "表达自然。",
            "naturalness_analysis": "focus on learning 可以理解，但 stay focused on 更像日常口语。",
            "improvement_points": ["优先使用常见口语搭配。", "保留原句的核心意思。"],
            "revised_sentence": "I focus on learning.",
            "idiomatic_suggestion": "I stay focused on learning.",
        }))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(
        ai.ai_config,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    client = TestClient(create_app())

    response = client.post(
        "/api/practice",
        json={
            "hint_cn": "我专注于学习。",
            "user_answer": "I focus on learning.",
            "english": "I focus on learning.",
            "vocab": ["Focus", "learning", "focus"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "correction"
    assert payload["naturalness_analysis"].startswith("focus on learning")
    assert payload["improvement_points"] == [
        "优先使用常见口语搭配。",
        "保留原句的核心意思。",
    ]
    assert '"naturalness_analysis"' in captured["prompt"]
    assert '"improvement_points"' in captured["prompt"]
    assert db.get_review_words() == {}
    assert len(db.list_v2_practice_attempts()) == 1


def test_practice_rejects_empty_input_and_does_not_record_ai_failure(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.fastapi_routes import ai

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    calls = []

    def create(**_kwargs):
        calls.append(True)
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        ai.ai_config,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    client = TestClient(create_app())

    empty = client.post("/api/practice", json={"user_answer": "   "})
    assert empty.status_code == 400
    assert calls == []
    assert db.list_v2_practice_attempts() == []

    failed = client.post(
        "/api/practice",
        json={
            "practice_type": "phrase",
            "target": "take off",
            "user_answer": "The plane take off yesterday.",
            "input_method": "keyboard",
        },
    )
    assert failed.status_code == 500
    assert db.list_v2_practice_attempts() == []


def test_practice_example_allows_empty_input_without_recording_attempt(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.fastapi_routes import ai

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")

    def create(**_kwargs):
        message = SimpleNamespace(content=json.dumps({
            "example_sentence": "The plane took off right on time."
        }))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(
        ai.ai_config,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    client = TestClient(create_app())
    response = client.post(
        "/api/practice",
        json={
            "action": "example",
            "practice_type": "phrase",
            "target": "take off",
            "scenario_cn": "飞机准时起飞了。",
            "user_answer": "",
        },
    )
    assert response.status_code == 200
    assert response.json()["mode"] == "example"
    assert response.json()["recorded_as_attempt"] is False
    assert db.list_v2_practice_attempts() == []
    history = client.get("/api/practice/history")
    assert history.status_code == 200
    assert history.json()["total"] == 0


def test_practice_history_returns_complete_correction_record(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.fastapi_routes import ai

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")

    def create(**_kwargs):
        message = SimpleNamespace(content=json.dumps({
            "verdict": "needs_revision",
            "target_used_correctly": False,
            "key_issue": "break down 不能这样作不及物谓语使用。",
            "explanation": "第二句缺少主语，且搭配不自然。",
            "naturalness_analysis": "Always breakdown at first 不像完整口语句，且 breakdown 在此是名词。",
            "improvement_points": ["补出主语。", "把 breakdown 换成 struggle。"],
            "revised_sentence": "I always break down at first.",
            "idiomatic_suggestion": "I always struggle at first.",
        }))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(
        ai.ai_config,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    client = TestClient(create_app())
    sentence = db.upsert_v2_sentence(
        "For me, the best analogy is learning to ride a bike.",
        translation="对我来说，最好的比方是学骑自行车。",
    )

    corrected = client.post(
        "/api/practice",
        json={
            "practice_type": "word",
            "target_type": "word",
            "target": "analogy",
            "sentence_id": sentence["id"],
            "scenario_cn": "对我来说最好的比方是，学外语就像学骑车。",
            "hint_text": "使用 analogy 说明一个学习体验。",
            "source_context": "For me, the best analogy is learning to ride a bike.",
            "user_answer": "For me, the best analogy is learning a language. Always breakdown at first.",
            "input_method": "voice",
            "hint_used": True,
        },
    )
    assert corrected.status_code == 200

    history = client.get(
        "/api/practice/history",
        params={"target": "analogy", "sentence_id": sentence["id"], "practice_type": "word"},
    )
    assert history.status_code == 200
    payload = history.json()
    assert payload["page"] == 1
    assert payload["page_size"] == 20
    assert payload["total"] == 1
    assert payload["total_pages"] == 1
    assert payload["filters"] == {
        "target": "analogy",
        "sentence_id": sentence["id"],
        "practice_type": "word",
    }
    item = payload["items"][0]
    assert item["target"] == "analogy"
    assert item["target_type"] == "word"
    assert item["sentence_id"] == sentence["id"]
    assert item["practice_type"] == "word"
    assert item["scenario_cn"] == "对我来说最好的比方是，学外语就像学骑车。"
    assert item["hint_text"] == "使用 analogy 说明一个学习体验。"
    assert item["source_context"] == "For me, the best analogy is learning to ride a bike."
    assert item["user_answer"].startswith("For me, the best analogy")
    assert item["status"] == "needs_revision"
    assert item["error_summary"] == "break down 不能这样作不及物谓语使用。"
    assert item["revised_sentence"] == "I always break down at first."
    assert item["idiomatic_suggestion"] == "I always struggle at first."
    assert item["feedback"]["explanation"] == "第二句缺少主语，且搭配不自然。"
    assert item["naturalness_analysis"].startswith("Always breakdown")
    assert item["improvement_points"] == ["补出主语。", "把 breakdown 换成 struggle。"]
    assert item["input_method"] == "voice"
    assert item["hint_used"] is True
    assert item["counts_as_completion"] is True
    assert item["created_at"]


def test_practice_history_migrates_existing_attempts_without_losing_feedback(tmp_path, monkeypatch):
    import sqlite3

    import db
    from fastapi_server import create_app

    database = tmp_path / "legacy-vocab.db"
    monkeypatch.setattr(db, "DB_PATH", database)
    with sqlite3.connect(database) as conn:
        conn.execute(
            """
            CREATE TABLE v2_practice_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                practice_type TEXT NOT NULL,
                target TEXT NOT NULL DEFAULT '',
                sentence_id INTEGER,
                lesson_id INTEGER,
                user_input TEXT NOT NULL,
                input_method TEXT NOT NULL DEFAULT 'keyboard',
                hint_used INTEGER NOT NULL DEFAULT 0,
                scenario_cn TEXT NOT NULL DEFAULT '',
                verdict TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            INSERT INTO v2_practice_attempts
                (practice_type, target, user_input, verdict, result_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "phrase",
                "break down",
                "I breakdown at first.",
                "needs_revision",
                json.dumps({
                    "status_label": "建议修改",
                    "target_used_correctly": False,
                    "key_issue": "break down 应分开拼写。",
                    "explanation": "这里需要使用动词短语。",
                    "revised_sentence": "I break down at first.",
                    "idiomatic_suggestion": "I struggle at first.",
                }, ensure_ascii=False),
                "2026-07-30T09:00:00+08:00",
            ),
        )

    client = TestClient(create_app())
    response = client.get("/api/practice/history", params={"target": "break down"})

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["target_type"] == "phrase"
    assert item["error_summary"] == "break down 应分开拼写。"
    assert item["revised_sentence"] == "I break down at first."
    assert item["idiomatic_suggestion"] == "I struggle at first."


def test_practice_history_filters_and_paginates_newest_first(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.fastapi_routes import ai

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")

    def create(**_kwargs):
        message = SimpleNamespace(content=json.dumps({
            "verdict": "accepted",
            "target_used_correctly": True,
            "key_issue": "",
            "explanation": "表达成立。",
            "revised_sentence": "",
            "idiomatic_suggestion": "",
        }))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(
        ai.ai_config,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    client = TestClient(create_app())
    submitted = [
        ("analogy", "First analogy answer."),
        ("contrast", "A contrasting answer."),
        ("analogy", "Second analogy answer."),
        ("analogy", "Third analogy answer."),
    ]
    attempt_ids = []
    for target, answer in submitted:
        response = client.post(
            "/api/practice",
            json={
                "practice_type": "word",
                "target": target,
                "user_answer": answer,
            },
        )
        assert response.status_code == 200
        attempt_ids.append(response.json()["attempt_id"])

    first_page = client.get(
        "/api/practice/history",
        params={"target": "ANALOGY", "practice_type": "word", "page": 1, "page_size": 2},
    ).json()
    second_page = client.get(
        "/api/practice/history",
        params={"target": "analogy", "practice_type": "word", "page": 2, "page_size": 2},
    ).json()

    assert first_page["total"] == 3
    assert first_page["total_pages"] == 2
    assert [item["id"] for item in first_page["items"]] == [attempt_ids[3], attempt_ids[2]]
    assert [item["id"] for item in second_page["items"]] == [attempt_ids[0]]
    invalid = client.get("/api/practice/history", params={"page_size": 101})
    assert invalid.status_code == 400


def test_init_db_removes_lookup_only_review_membership(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    db.activate_word_review("clicked", source="lookup")
    db.activate_word_review("chosen", source="manual")

    db.init_db()

    assert db.is_word_in_review("clicked") is False
    assert db.is_word_in_review("chosen") is True
    assert "clicked" in db.get_all_words()
