import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from webapp.services import dicts


@pytest.fixture()
def ecdict_tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "ecdict.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE words (word TEXT PRIMARY KEY COLLATE NOCASE, phonetic TEXT, "
        "definition TEXT, translation TEXT, pos TEXT, collins TEXT, oxford TEXT, "
        "tag TEXT, bnc TEXT, frq TEXT, exchange TEXT)"
    )
    conn.execute(
        "INSERT INTO words (word, phonetic, definition, translation, pos, collins, oxford, tag, bnc, frq) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("study", "ˈstʌdi", "the activity of learning\\ngaining knowledge", "学习；研究", "n.",
         "3", "1", "ielts toefl", "2201", "1523"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(dicts, "ECDICT_DB", db_path)
    monkeypatch.setattr(dicts, "_ECDICT_CONN", None)
    return db_path


def test_ecdict_lookup_formats_entry(ecdict_tmp_db):
    result = dicts.lookup_ecdict("Study")  # NOCASE
    assert "ˈstʌdi" in result
    assert "the activity of learning" in result
    assert "学习" in result
    assert "[n.]" in result


def test_ecdict_lookup_missing_returns_empty(ecdict_tmp_db):
    assert dicts.lookup_ecdict("nosuchword") == ""


def test_ecdict_missing_db_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(dicts, "ECDICT_DB", tmp_path / "absent.db")
    monkeypatch.setattr(dicts, "_ECDICT_CONN", None)
    assert dicts.lookup_ecdict("study") == ""


def test_ecdict_meta_returns_frequency_and_tags(ecdict_tmp_db):
    meta = dicts.lookup_ecdict_meta("Study")  # NOCASE
    assert meta["frq"] == 1523
    assert meta["bnc"] == 2201
    assert meta["collins"] == 3
    assert meta["oxford"] is True
    assert meta["tags"] == ["雅思", "托福"]
    assert dicts.format_ecdict_meta(meta) == "COCA #1523 · 牛津3000 · 柯林斯3★ · 雅思 · 托福"


def test_ecdict_meta_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(dicts, "ECDICT_DB", tmp_path / "absent.db")
    monkeypatch.setattr(dicts, "_ECDICT_CONN", None)
    assert dicts.lookup_ecdict_meta("study") == {}
    assert dicts.format_ecdict_meta({}) == ""
