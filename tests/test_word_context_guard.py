"""词句对应守卫（2026-08-06 bug3）：收藏生词时句子必须包含该词（词族）。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


def test_sentence_contains_word_exact_and_inflection():
    import db

    assert db.sentence_contains_word("remember you need to conjugate it", "conjugate")
    assert db.sentence_contains_word("then we conjugated the verb", "conjugate")
    assert db.sentence_contains_word("she studies every day", "study")
    assert db.sentence_contains_word("tips to supercharge your learning.", "supercharge")


def test_sentence_contains_word_rejects_wrong_sentence():
    import db

    assert not db.sentence_contains_word(
        "vocabulary in this section we'll review collocations phrasal verbs",
        "conjugate",
    )
    assert not db.sentence_contains_word("Learning to code is so different in 2026.", "containerization")
    assert not db.sentence_contains_word("Best of luck in your coding journey.", "supercharge")
    # 短词不做词干匹配，不规则变形不命中
    assert not db.sentence_contains_word("I went hiking", "go")
    # 前缀过短的误配：virgin 不是 viral 词族
    assert not db.sentence_contains_word("Kind of exploring virgin territory.", "viral")
    # 已知残余误配：student/study 共享 4 字符前缀，轻量词干无法区分（误配方向是保留原句，无害）


def test_sentence_contains_word_phrase():
    import db

    assert db.sentence_contains_word("we will take off soon", "take off")
    assert not db.sentence_contains_word("we will take it soon", "take off")


def test_find_v2_sentence_containing_prefers_short_match(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    long_text = (
        "vocabulary note: conjugate is a verb. " + "padding " * 40
        + "so conjugate it with the subject"
    )
    short_text = "remember you need to still identify the verb and conjugate it"
    db.upsert_v2_sentence(long_text)
    db.upsert_v2_sentence(short_text)
    db.upsert_v2_sentence("collocations are fixed word pairs")

    found = db.find_v2_sentence_containing("conjugate")

    assert found == short_text


def test_find_v2_sentence_containing_returns_none_when_absent(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    db.upsert_v2_sentence("nothing relevant here")

    assert db.find_v2_sentence_containing("conjugate") is None
