"""Compact 释义库：构建脚本与 dicts 回退的闭环测试。"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import build_compact_gloss
from webapp.services import dicts


@pytest.fixture
def compact_env(tmp_path, monkeypatch):
    ecdict_db = tmp_path / "ecdict.db"
    compact_db = tmp_path / "gloss_compact.db"
    wordlists_dir = tmp_path / "compiled"
    wordlists_dir.mkdir()
    (wordlists_dir / "builtin_test.json").write_text(
        '{"metadata": {}, "words": ["rareword"]}', encoding="utf-8"
    )
    conn = sqlite3.connect(str(ecdict_db))
    conn.execute(
        "CREATE TABLE words (word TEXT, phonetic TEXT, definition TEXT, translation TEXT,"
        " pos TEXT, collins INTEGER, oxford INTEGER, tag TEXT, bnc INTEGER, frq INTEGER, exchange TEXT)"
    )
    # 频率词：进 compact
    conn.execute(
        "INSERT INTO words VALUES ('synthesis', '', '', 'n. 综合, 组织\\n[化] 合成', 'n.', 1, 0, '', 100, 100, '')"
    )
    # 无频率标记的普通词：不进 compact
    conn.execute(
        "INSERT INTO words VALUES ('obscureterm', '', '', 'n. 晦涩词', 'n.', 0, 0, '', 0, 0, '')"
    )
    # 无频率标记但在内置词表：进 compact
    conn.execute(
        "INSERT INTO words VALUES ('rareword', '', '', 'adj. 罕见的\\nadv. 罕见地', 'adj.', 0, 0, '', 0, 0, '')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(build_compact_gloss, "ECDICT_DB", ecdict_db)
    monkeypatch.setattr(build_compact_gloss, "COMPACT_DB", compact_db)
    monkeypatch.setattr(build_compact_gloss, "WORDLISTS_DIR", wordlists_dir)
    monkeypatch.setattr(dicts, "ECDICT_DB", ecdict_db)
    monkeypatch.setattr(dicts, "COMPACT_GLOSS_DB", compact_db)
    monkeypatch.setattr(dicts, "_ECDICT_CONN", None)
    monkeypatch.setattr(dicts, "_COMPACT_CONN", None)
    return {"ecdict": ecdict_db, "compact": compact_db}


def test_build_compact_gloss_filters_by_freq_and_wordlist(compact_env):
    result = build_compact_gloss.build_compact_gloss()
    assert result["entries"] == 2
    conn = sqlite3.connect(str(compact_env["compact"]))
    rows = dict(conn.execute("SELECT word, zh FROM gloss").fetchall())
    conn.close()
    assert rows == {"synthesis": "n. 综合, 组织", "rareword": "adj. 罕见的"}


def test_lookup_falls_back_to_compact_when_full_ecdict_missing(compact_env):
    build_compact_gloss.build_compact_gloss()
    compact_env["ecdict"].unlink()  # 全量 ECDICT 缺失 → 走 compact
    assert dicts.lookup_ecdict_translation("Synthesis") == "n. 综合, 组织"
    assert dicts.lookup_ecdict_translation("obscureterm") == ""
    assert dicts.lookup_ecdict_translation("nonexistent") == ""


def test_lookup_prefers_full_ecdict_when_present(compact_env):
    build_compact_gloss.build_compact_gloss()
    # 全量 ECDICT 在：无频率标记的词也能查到（不走 compact）
    assert dicts.lookup_ecdict_translation("obscureterm") == "n. 晦涩词"
    assert dicts.lookup_ecdict_translation("synthesis") == "n. 综合, 组织"
