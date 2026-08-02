import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


def test_highlight_reading_blocks_marks_candidates_without_autosave(monkeypatch):
    from webapp.services import v2_vocab

    monkeypatch.setattr(
        v2_vocab,
        "load_v2_wordlist_index",
        lambda: {
            "climate": {"word": "climate", "level": "ielts"},
            "migration": {"word": "migration", "level": "awl"},
        },
    )
    monkeypatch.setattr(
        v2_vocab,
        "load_word_meanings",
        lambda: {"climate": "气候"},
    )
    monkeypatch.setattr(v2_vocab, "_lookup_local_dict_meaning", lambda candidates: "")

    result = v2_vocab.highlight_reading_blocks([
        {"index": 1, "text": "Climate change affects migration patterns."}
    ])

    assert result["blocks"][0]["highlights"] == [
        {"word": "Climate", "normalized": "climate", "level": "ielts", "meaning": "气候", "start": 0, "end": 7},
        {"word": "migration", "normalized": "migration", "level": "awl", "meaning": "", "start": 23, "end": 32},
    ]
    assert result["candidate_count"] == 2


def test_highlight_reading_blocks_does_not_mark_basic_function_words(monkeypatch):
    from webapp.services import v2_vocab

    monkeypatch.setattr(
        v2_vocab,
        "load_v2_wordlist_index",
        lambda: {
            "the": {"word": "the", "level": "known"},
            "and": {"word": "and", "level": "known"},
            "urban": {"word": "urban", "level": "b1"},
            "resident": {"word": "resident", "level": "b1"},
        },
    )
    monkeypatch.setattr(v2_vocab, "load_word_meanings", lambda: {})
    monkeypatch.setattr(v2_vocab, "_lookup_local_dict_meaning", lambda candidates: "")

    result = v2_vocab.highlight_reading_blocks([
        {"index": 1, "text": "The urban residents and the parks."}
    ])

    assert result["blocks"][0]["highlights"] == [
        {"word": "urban", "normalized": "urban", "level": "b1", "meaning": "", "start": 4, "end": 9},
        {"word": "residents", "normalized": "resident", "level": "b1", "meaning": "", "start": 10, "end": 19},
    ]


def test_highlight_reading_blocks_skips_lesson_hidden_words(monkeypatch):
    from webapp.services import v2_vocab

    monkeypatch.setattr(
        v2_vocab,
        "load_v2_wordlist_index",
        lambda: {
            "urban": {"word": "urban", "level": "b1"},
            "migration": {"word": "migration", "level": "academic"},
        },
    )
    monkeypatch.setattr(v2_vocab, "load_word_meanings", lambda: {})
    monkeypatch.setattr(v2_vocab, "_lookup_local_dict_meaning", lambda candidates: "")

    result = v2_vocab.highlight_reading_blocks(
        [{"index": 1, "text": "Urban migration changed the city."}],
        hidden_words={"urban"},
    )

    assert result["blocks"][0]["highlights"] == [
        {"word": "migration", "normalized": "migration", "level": "academic", "meaning": "", "start": 6, "end": 15},
    ]


def test_concise_gloss_trims_verbose_dictionary_entries():
    from webapp.services import v2_vocab

    assert v2_vocab._concise_gloss("") == ""
    assert v2_vocab._concise_gloss("气候") == "气候"
    verbose = "中文：使生效；贯彻；执行；实施 carry something out to implement；实行变革\n英文：implement verb ..."
    assert v2_vocab._concise_gloss(verbose) == "使生效；贯彻；执行"
    long_single = "一个非常长的没有分号的释义内容超过限制"
    assert v2_vocab._concise_gloss(long_single).endswith("…")
    assert len(v2_vocab._concise_gloss(long_single)) == 14
    assert v2_vocab._concise_gloss("我需要一副新眼镜。") == ""
    assert v2_vocab._concise_gloss("中奖号码是由电脑随机选取的；我的手机好像随时自动关机") == ""
    assert v2_vocab._concise_gloss("完成 achieve The first part of the plan") == "完成"
    assert v2_vocab._concise_gloss("使）隔离，孤立，脱离 isolate somebody") == "使隔离，孤立，脱离"


def test_explicit_lookup_prefers_chinese_fallback_over_english_local_entry(monkeypatch):
    from webapp.services import v2_vocab

    meanings = {}
    v2_vocab.clear_vocab_caches()
    monkeypatch.setattr(v2_vocab, "load_word_meanings", lambda: meanings)
    monkeypatch.setattr(
        v2_vocab,
        "_lookup_local_dict_meaning",
        lambda candidates: "英文：massively adverb; to a very large degree or extent",
    )
    monkeypatch.setattr(v2_vocab, "_translate_word_fallback", lambda word: "大量地；大幅度地")

    local = v2_vocab.lookup_word_meaning("massively")
    translated = v2_vocab.lookup_word_meaning("massively", allow_external_fallback=True)

    assert local["source"] == "local"
    assert translated["source"] == "external"
    assert translated["meaning"] == "大量地；大幅度地"
    v2_vocab.clear_vocab_caches()


def test_highlight_gloss_rejects_example_sentence_and_fills_missing_meaning(monkeypatch):
    from webapp.services import v2_vocab

    monkeypatch.setattr(
        v2_vocab,
        "load_v2_wordlist_index",
        lambda: {
            "specs": {"word": "specs", "level": "ielts"},
            "spectacles": {"word": "spectacles", "level": "ielts"},
        },
    )
    monkeypatch.setattr(
        v2_vocab,
        "load_word_meanings",
        lambda: {
            "specs": "我需要一副新眼镜。",
            "spec": "规格；规范",
        },
    )
    monkeypatch.setattr(
        v2_vocab,
        "_lookup_local_dict_meaning",
        lambda candidates: "中文：眼镜" if "spectacle" in candidates else "",
    )

    result = v2_vocab.highlight_reading_blocks([
        {"index": 1, "text": "Specs differ from spectacles."}
    ])

    assert [item["meaning"] for item in result["blocks"][0]["highlights"]] == [
        "规格；规范",
        "眼镜",
    ]


def test_load_v2_wordlist_index_does_not_mix_lookup_meaning_lists(monkeypatch):
    from webapp.services import v2_vocab

    v2_vocab.clear_vocab_caches()
    monkeypatch.setattr(v2_vocab, "load_default_intermediate_words", lambda: {"urban"})
    monkeypatch.setattr(v2_vocab, "_load_compiled_word_set", lambda filename: set())
    monkeypatch.setattr(v2_vocab, "load_word_meanings", lambda: {"the": "", "and": "", "have": ""})

    index = v2_vocab.load_v2_wordlist_index()

    assert "urban" in index
    assert "the" not in index
    assert "and" not in index
    assert "have" not in index
    v2_vocab.clear_vocab_caches()


def test_load_v2_wordlist_index_is_cached_until_cleared(monkeypatch):
    from webapp.services import v2_vocab

    v2_vocab.clear_vocab_caches()
    calls = {"count": 0}

    def fake_load(filename):
        calls["count"] += 1
        return {"academic"} if filename == "domain_academic.json" else set()

    monkeypatch.setattr(v2_vocab, "load_default_intermediate_words", lambda: {"urban"})
    monkeypatch.setattr(v2_vocab, "_load_compiled_word_set", fake_load)

    first = v2_vocab.load_v2_wordlist_index()
    second = v2_vocab.load_v2_wordlist_index()

    assert first is second
    assert calls["count"] == 2

    v2_vocab.clear_vocab_caches()
    refreshed = v2_vocab.load_v2_wordlist_index()
    assert refreshed is not first
    assert calls["count"] == 4
    v2_vocab.clear_vocab_caches()


def test_mdx_lookup_reuses_reader_and_finds_duplicate_entries(monkeypatch):
    from types import SimpleNamespace

    from mdict_utils import reader as mdict_reader
    from webapp.services import dicts

    fake_reader = SimpleNamespace(
        _key_list=[
            (0, b"apple"),
            (5, b"banana"),
            (11, b"banana"),
            (18, b"carrot"),
        ]
    )
    loads = []
    monkeypatch.setattr(
        mdict_reader,
        "MDX",
        lambda path, encoding, substyle, passcode: loads.append(path) or fake_reader,
    )
    monkeypatch.setattr(
        mdict_reader,
        "get_record",
        lambda reader, key, offset, length: f"{offset}:{length}",
    )
    dicts._MDX_CACHE.clear()
    dicts._MDX_KEY_INDEX_CACHE.clear()

    assert dicts._lookup_mdx("fake.mdx", "banana") == "5:6\n---\n11:7"
    assert dicts._lookup_mdx("fake.mdx", "banana") == "5:6\n---\n11:7"
    assert dicts._lookup_mdx("fake.mdx", "missing") == ""
    assert loads == ["fake.mdx"]
    dicts._MDX_CACHE.clear()
    dicts._MDX_KEY_INDEX_CACHE.clear()
