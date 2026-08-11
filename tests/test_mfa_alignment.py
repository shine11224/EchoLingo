import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from fastapi.testclient import TestClient


def test_parse_textgrid_and_project_existing_sentences(tmp_path):
    from webapp.services.mfa_alignment import (
        parse_textgrid,
        phones_to_ipa,
        project_words_to_sentences,
    )

    assert phones_to_ipa(["S", "OW1"]) == "ˈsoʊ"
    assert phones_to_ipa(["R", "IH0", "M", "EH1", "M", "B", "ER0"]) == "ɹɪˈmɛmbɚ"

    textgrid = tmp_path / "chunk.TextGrid"
    textgrid.write_text(
        """File type = "ooTextFile"
Object class = "TextGrid"

xmin = 0
xmax = 2
tiers? <exists>
size = 2
item []:
    item [1]:
        class = "IntervalTier"
        name = "words"
        xmin = 0
        xmax = 2
        intervals: size = 2
        intervals [1]:
            xmin = 0.10
            xmax = 0.55
            text = "hello"
        intervals [2]:
            xmin = 0.60
            xmax = 1.05
            text = "world"
    item [2]:
        class = "IntervalTier"
        name = "phones"
        xmin = 0
        xmax = 2
        intervals: size = 4
        intervals [1]:
            xmin = 0.10
            xmax = 0.30
            text = "HH"
        intervals [2]:
            xmin = 0.30
            xmax = 0.55
            text = "EH1"
        intervals [3]:
            xmin = 0.60
            xmax = 0.80
            text = "W"
        intervals [4]:
            xmin = 0.80
            xmax = 1.05
            text = "ER1"
""",
        encoding="utf-8",
    )

    tiers = parse_textgrid(textgrid)
    assert [item["label"] for item in tiers["words"]] == ["hello", "world"]
    aligned_words = []
    for word in tiers["words"]:
        phones = [
            phone for phone in tiers["phones"]
            if word["start"] <= (phone["start"] + phone["end"]) / 2 <= word["end"]
        ]
        aligned_words.append({**word, "phones": phones, "ipa": "test"})
    sentences = project_words_to_sentences(
        [
            {
                "index": 0,
                "text": "Hello world.",
                "start_seconds": 0.0,
                "end_seconds": 1.4,
            }
        ],
        aligned_words,
    )

    assert sentences[0]["boundary_confidence"] == "high"
    assert sentences[0]["start_seconds"] == 0.0
    assert sentences[0]["end_seconds"] == 1.19
    assert [word["text"] for word in sentences[0]["words"]] == ["Hello", "world"]
    assert sentences[0]["words"][0]["pause_after_ms"] == 50
    assert sentences[0]["words"][1]["pause_before_ms"] == 50


def test_project_words_preserves_numeric_tokens_without_false_pause():
    from webapp.services.mfa_alignment import project_words_to_sentences

    sentences = project_words_to_sentences(
        [
            {
                "index": 0,
                "text": "almost 15 years",
                "start_seconds": 0.0,
                "end_seconds": 1.0,
            }
        ],
        [
            {"label": "almost", "start": 0.0, "end": 0.4, "phones": [], "ipa": "ˈɔlmoʊst"},
            {"label": "15", "start": 0.4, "end": 0.7, "phones": [], "ipa": ""},
            {"label": "years", "start": 0.7, "end": 1.0, "phones": [], "ipa": "jɪrz"},
        ],
    )

    assert sentences[0]["coverage"] == 1.0
    assert [word["text"] for word in sentences[0]["words"]] == ["almost", "15", "years"]
    assert sentences[0]["words"][1]["ipa"] == "fɪfˈtin"
    assert sentences[0]["words"][0]["pause_after_ms"] == 0
    assert sentences[0]["words"][1]["pause_after_ms"] == 0


def test_cached_alignment_numeric_token_gets_ipa_fallback():
    from webapp.services.v2_intensive import _enrich_aligned_words

    words = _enrich_aligned_words([
        {"text": "15", "word": "15", "ipa": "", "start": 0.4, "end": 0.7},
    ])

    assert words[0]["ipa"] == "fɪfˈtin"


def test_intensive_document_uses_cached_mfa_timing_and_words(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.services import mfa_alignment

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(mfa_alignment, "OUTPUT_DIR", tmp_path / "output")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="local_audio",
        source_url="manual:mfa-test",
        title="MFA Test",
        lesson_mode="reading",
        media_url="/output/v2_assets/1/audio.wav",
        media_kind="local_audio",
    )
    db.replace_v2_subtitle_segments(
        lesson["id"],
        [
            {"index": 1, "start": 0.4, "end": 1.0, "text": "Hello"},
            {"index": 2, "start": 1.0, "end": 1.8, "text": "world."},
        ],
    )
    result_path = mfa_alignment.alignment_directory(lesson["id"]) / "alignment.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        json.dumps(
            {
                "lesson_id": lesson["id"],
                "status": "ready",
                "model": "english_us_arpa",
                "updated_at": "2026-07-27T00:00:00+00:00",
                "word_count": 2,
                "sentences": [
                    {
                        "key": 0,
                        "text": "Hello world.",
                        "start_seconds": 0.18,
                        "end_seconds": 1.64,
                        "coverage": 1.0,
                        "boundary_confidence": "high",
                        "pause_before_ms": 0,
                        "pause_after_ms": 240,
                        "words": [
                            {
                                "text": "Hello",
                                "word": "hello",
                                "start": 0.3,
                                "end": 0.8,
                                "ipa": "həˈloʊ",
                                "phones": [],
                            },
                            {
                                "text": "world",
                                "word": "world",
                                "start": 0.86,
                                "end": 1.5,
                                "ipa": "wɝld",
                                "phones": [],
                            },
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    response = client.get(f"/api/v2/lessons/{lesson['id']}/intensive")
    assert response.status_code == 200
    data = response.json()
    sentence = data["sentences"][0]
    assert sentence["timing_source"] == "mfa"
    assert sentence["start_seconds"] == 0.18
    assert sentence["end_seconds"] == 1.64
    assert sentence["aligned_words"][0]["ipa"] == "həˈloʊ"
    assert data["alignment"]["status"] == "ready"

    status = client.get(f"/api/v2/lessons/{lesson['id']}/alignment")
    assert status.status_code == 200
    assert status.json()["sentence_count"] == 1

    page = client.get(f"/workspace/{lesson['id']}/intensive")
    assert page.status_code == 200
    assert "MFA 原声对齐" in page.text
    assert "aligned_words" in page.text
    assert "原声短停顿候选" in page.text


def test_missing_alignment_keeps_subtitle_timing(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from webapp.services import mfa_alignment

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(mfa_alignment, "OUTPUT_DIR", tmp_path / "output")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="local_audio",
        source_url="manual:mfa-fallback",
        title="MFA Fallback",
        lesson_mode="reading",
    )
    db.replace_v2_subtitle_segments(
        lesson["id"],
        [{"index": 1, "start": 2.0, "end": 3.5, "text": "Fallback sentence."}],
    )

    response = client.get(f"/api/v2/lessons/{lesson['id']}/intensive")
    assert response.status_code == 200
    sentence = response.json()["sentences"][0]
    assert sentence["timing_source"] == "subtitle"
    assert sentence["start_seconds"] == 2.0
    assert sentence["end_seconds"] == 3.5
    assert sentence["aligned_words"] == []
