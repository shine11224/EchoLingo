import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

build_ecdict = pytest.importorskip("build_ecdict")


@pytest.mark.skipif(not build_ecdict.DB_PATH.exists(), reason="ecdict.db not built")
def test_builtin_wordlists_generated_with_inflections(tmp_path, monkeypatch):
    monkeypatch.setattr(build_ecdict, "COMPILED_DIR", tmp_path)
    written = build_ecdict.build_builtin_wordlists()

    assert "builtin_oxford3000" in written
    assert "builtin_coca_2000" in written

    oxford = json.loads((tmp_path / "builtin_oxford3000.json").read_text(encoding="utf-8"))
    words = set(oxford["words"])
    assert "apple" in words
    assert oxford["metadata"]["tag"] == "牛津3K"
    assert oxford["metadata"]["builtin"] is True

    ielts = json.loads((tmp_path / "builtin_ielts.json").read_text(encoding="utf-8"))
    ielts_words = set(ielts["words"])
    # 词形扩展：列表中应包含屈折变形（如 studies/studied 类）
    assert any(w.endswith("ies") or w.endswith("ied") for w in ielts_words)

    coca = json.loads((tmp_path / "builtin_coca_2000.json").read_text(encoding="utf-8"))
    assert coca["metadata"]["type"] == "exclude"
    # 2000 基础词 + 屈折扩展；frq 是 TEXT 列，若忘 CAST 会按字符串比较多匹配上万词
    assert 2000 < len(coca["words"]) < 12000

    coca5 = json.loads((tmp_path / "builtin_coca_5000.json").read_text(encoding="utf-8"))
    assert 3000 < len(coca5["words"]) < 20000


def test_wordlists_config_includes_virtual_vocab_lists(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    db.activate_word_review("study", source="manual", analysis={"basic_meaning": "学习"})

    client = TestClient(create_app())
    configs = client.get("/api/wordlists/config").json()
    by_id = {c["id"]: c for c in configs}
    assert by_id["my_vocab"]["type"] == "domain"
    assert by_id["my_vocab"]["count"] == 1
    assert by_id["my_mastered"]["type"] == "exclude"

    assert client.get("/wordlists/my_vocab").json()["words"] == ["study"]


def test_my_mastered_wordlist_includes_known_words(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    db.add_known_word("apple", "2026-08-03")
    db.activate_word_review("study", source="manual")

    client = TestClient(create_app())
    mastered = client.get("/wordlists/my_mastered").json()["words"]
    assert "apple" in mastered
    assert "study" not in mastered  # 未掌握的生词不在已掌握列表
