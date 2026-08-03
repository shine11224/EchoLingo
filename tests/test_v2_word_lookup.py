import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


def test_token_filtering_avoids_noise():
    from webapp.services.v2_vocab import tokenize_for_lookup, should_highlight_token

    tokens = tokenize_for_lookup("I met John in LA and analyzed complex systems.")
    assert "analyzed" in tokens
    assert "complex" in tokens
    assert "i" not in tokens
    assert "john" not in tokens
    assert "la" not in tokens


def test_highlight_segments_uses_default_wordlist(monkeypatch):
    from webapp.services.v2_vocab import highlight_segments

    monkeypatch.setattr(
        "webapp.services.v2_vocab.load_default_intermediate_words",
        lambda: {"analyzed", "complex"},
    )
    monkeypatch.setattr(
        "webapp.services.v2_vocab.load_word_meanings",
        lambda: {"analyzed": "分析", "complex": "复杂的"},
    )
    segments = [{"index": 1, "text": "We analyzed complex systems."}]
    result = highlight_segments(segments)
    assert result[0]["highlighted_words"] == ["analyzed", "complex"]
    assert result[0]["word_meanings"] == {"analyzed": "分析", "complex": "复杂的"}


def test_highlight_segments_skips_lesson_hidden_words(monkeypatch):
    from webapp.services.v2_vocab import highlight_segments

    monkeypatch.setattr(
        "webapp.services.v2_vocab.load_default_intermediate_words",
        lambda: {"analyzed", "complex"},
    )
    monkeypatch.setattr(
        "webapp.services.v2_vocab.load_word_meanings",
        lambda: {"analyzed": "分析", "complex": "复杂的"},
    )

    segments = [{"index": 1, "text": "We analyzed complex systems."}]
    result = highlight_segments(segments, hidden_words={"complex"})

    assert result[0]["highlighted_words"] == ["analyzed"]
    assert result[0]["word_meanings"] == {"analyzed": "分析"}


def test_lookup_word_meaning_uses_loaded_meanings(monkeypatch):
    from webapp.services import v2_vocab

    monkeypatch.setattr(
        "webapp.services.v2_vocab.load_word_meanings",
        lambda: {"complex": "复杂的"},
    )
    v2_vocab._LOOKUP_CACHE.clear()

    result = v2_vocab.lookup_word_meaning("Complex!")
    assert result["word"] == "complex"
    assert result["meaning"] == "复杂的"
    assert result["found"] is True
    assert isinstance(result.get("phonetic"), str) and len(result["phonetic"]) > 0


def test_lookup_word_meaning_falls_back_to_inflection_and_common_words(monkeypatch):
    from webapp.services import v2_vocab

    monkeypatch.setattr(
        "webapp.services.v2_vocab.load_word_meanings",
        lambda: {"method": "方法"},
    )
    v2_vocab._LOOKUP_CACHE.clear()

    assert v2_vocab.lookup_word_meaning("methods")["meaning"] == "方法"
    assert v2_vocab.lookup_word_meaning("the")["meaning"] == "这个；那个"


def test_lookup_word_meaning_skips_cached_example_sentence(monkeypatch):
    from webapp.services import v2_vocab

    monkeypatch.setattr(
        v2_vocab,
        "load_word_meanings",
        lambda: {"specs": "我需要一副新眼镜。", "spec": "规格；规范"},
    )
    v2_vocab._LOOKUP_CACHE.clear()

    result = v2_vocab.lookup_word_meaning("specs")

    assert result["lemma"] == "spec"
    assert result["meaning"] == "规格；规范"


def test_lookup_word_meaning_uses_common_gloss_when_cache_is_an_example(monkeypatch):
    from webapp.services import v2_vocab

    monkeypatch.setattr(
        v2_vocab,
        "load_word_meanings",
        lambda: {"randomly": "中奖号码是由电脑随机选取的"},
    )
    v2_vocab._LOOKUP_CACHE.clear()

    result = v2_vocab.lookup_word_meaning("randomly")

    assert result["meaning"] == "随机地；任意地"


def test_local_dictionary_prefers_chinese_fallback_candidate(monkeypatch):
    from webapp.services import v2_vocab

    monkeypatch.setattr(
        v2_vocab.dict_service,
        "lookup_ecdict",
        lambda word: (
            "spectacles are eyeglasses."
            if word == "spectacles"
            else "spectacle noun 一副眼镜"
        ),
    )

    meaning = v2_vocab._lookup_local_dict_meaning(["spectacles", "spectacle"])

    assert "中文：一副眼镜" in meaning


def test_lookup_word_meaning_uses_local_dictionary_before_external_fallback(monkeypatch):
    from webapp.services import v2_vocab

    monkeypatch.setattr(v2_vocab, "load_word_meanings", lambda: {})
    monkeypatch.setattr(v2_vocab, "_lookup_local_dict_meaning", lambda candidates: "牛津释义")

    def fail_external(word):
        raise AssertionError("external fallback should not run when local dictionary hits")

    monkeypatch.setattr(v2_vocab, "_translate_word_fallback", fail_external)
    v2_vocab._LOOKUP_CACHE.clear()

    result = v2_vocab.lookup_word_meaning("coverage")
    assert result["word"] == "coverage"
    assert result["meaning"] == "牛津释义"
    assert result["found"] is True
    assert isinstance(result.get("phonetic"), str)

def test_lookup_word_meaning_does_not_call_external_fallback_by_default(monkeypatch):
    from webapp.services import v2_vocab

    monkeypatch.setattr(v2_vocab, "load_word_meanings", lambda: {})
    monkeypatch.setattr(v2_vocab, "_lookup_local_dict_meaning", lambda candidates: "")

    def fail_external(word):
        raise AssertionError("external fallback should not run on hover lookup")

    monkeypatch.setattr(v2_vocab, "_translate_word_fallback", fail_external)
    v2_vocab._LOOKUP_CACHE.clear()

    result = v2_vocab.lookup_word_meaning("unlistedword")
    assert result["word"] == "unlistedword"
    assert result["meaning"] == ""
    assert result["found"] is False
    assert isinstance(result.get("phonetic"), str)


def test_lookup_word_meaning_uses_external_fallback_when_enabled(monkeypatch):
    from webapp.services import v2_vocab

    monkeypatch.setattr(v2_vocab, "load_word_meanings", lambda: {})
    monkeypatch.setattr(v2_vocab, "_lookup_local_dict_meaning", lambda candidates: "")
    calls = []

    def external_fallback(word):
        calls.append(word)
        return "不可妥协的条件；不可商量的事项"

    monkeypatch.setattr(v2_vocab, "_translate_word_fallback", external_fallback)
    v2_vocab._LOOKUP_CACHE.clear()

    result = v2_vocab.lookup_word_meaning(
        "non-negotiables",
        allow_external_fallback=True,
    )

    assert calls == ["non-negotiables"]
    assert result["word"] == "nonnegotiables"
    assert result["lemma"] == "non-negotiable"
    assert result["meaning"] == "不可妥协的条件；不可商量的事项"
    assert result["found"] is True
    assert result["source"] == "external"
    assert isinstance(result.get("phonetic"), str)


def test_lookup_candidates_keep_hyphenated_and_plain_variants():
    from webapp.services import v2_vocab

    candidates = v2_vocab._lookup_candidates("non-negotiables")

    assert "non-negotiable" in candidates
    assert "nonnegotiable" in candidates


def test_hyphenated_local_dictionary_result_is_concise(monkeypatch):
    from webapp.services import v2_vocab

    raw = (
        "non-negotiable adjective 1 that cannot be discussed or changed "
        "不可谈判解决的；无法改变的 non-negotiable demands "
        "2 ( of a cheque, etc. 支票等 ) that cannot be changed for money "
        "只限本人使用的；禁止转让的"
    )
    monkeypatch.setattr(v2_vocab, "load_word_meanings", lambda: {})
    monkeypatch.setattr(
        v2_vocab.dict_service,
        "lookup_ecdict",
        lambda word: raw if word == "non-negotiable" else "",
    )
    monkeypatch.setattr(
        v2_vocab,
        "_translate_word_fallback",
        lambda word: (_ for _ in ()).throw(
            AssertionError("external fallback should not run after hyphenated local hit")
        ),
    )
    v2_vocab._LOOKUP_CACHE.clear()

    result = v2_vocab.lookup_word_meaning(
        "non-negotiables",
        allow_external_fallback=True,
    )

    assert result["meaning"] == "不可谈判解决的；无法改变的"
    assert result["lemma"] == "non-negotiable"
    assert result["source"] == "local"


def test_local_dictionary_meaning_is_summarized():
    from webapp.services import v2_vocab

    raw = (
        "coverage noun /pronunciation/ 1 the reporting of news and sport in newspapers "
        "and on the radio and television. 2 the amount of something that something provides. "
        "\u4e2d\u6587\u91ca\u4e49\uff1a\u65b0\u95fb\u62a5\u9053\uff1b\u8986\u76d6\u8303\u56f4\u3002 Example Bank: long noisy examples follow."
    )

    summary = v2_vocab._summarize_local_dict_meaning(raw)

    assert "\u4e2d\u6587\uff1a" in summary
    assert "\u82f1\u6587\uff1athe reporting of news and sport" in summary
    assert "Example Bank" not in summary
    assert len(summary) < 360
