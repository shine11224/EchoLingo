"""Dictionary and IPA helpers shared by FastAPI routes."""
from __future__ import annotations

import re
import threading
from pathlib import Path

ECDICT_DB = Path(__file__).resolve().parents[3] / "resources" / "ecdict" / "ecdict.db"

_ECDICT_CONN = None
_ECDICT_LOCK = threading.Lock()


def _get_ecdict_conn():
    global _ECDICT_CONN
    if _ECDICT_CONN is not None:
        return _ECDICT_CONN
    with _ECDICT_LOCK:
        if _ECDICT_CONN is None and ECDICT_DB.exists():
            import sqlite3

            _ECDICT_CONN = sqlite3.connect(str(ECDICT_DB), check_same_thread=False)
    return _ECDICT_CONN


def lookup_ecdict(word: str) -> str:
    """Look up the bundled open ECDICT (MIT) SQLite; '' when db or entry missing."""
    conn = _get_ecdict_conn()
    if conn is None:
        return ""
    try:
        with _ECDICT_LOCK:
            row = conn.execute(
                "SELECT phonetic, definition, translation, pos FROM words WHERE word = ?",
                (word.strip(),),
            ).fetchone()
    except Exception:
        return ""
    if not row:
        return ""
    phonetic, definition, translation, pos = row
    parts = []
    if phonetic:
        parts.append(f"/{phonetic}/")
    if pos:
        parts.append(f"[{pos}]")
    if definition:
        parts.append(definition.replace("\\n", " ").replace("\n", " ").strip())
    if translation:
        parts.append(translation.replace("\\n", " ").replace("\n", " ").strip())
    return re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()[:1200]


def lookup_ecdict_translation(word: str) -> str:
    """仅取 ECDICT 中文释义的首个义项（角标级短文本）；缺失返回 ''。"""
    conn = _get_ecdict_conn()
    if conn is None:
        return ""
    try:
        with _ECDICT_LOCK:
            row = conn.execute(
                "SELECT translation FROM words WHERE word = ?",
                (word.strip().lower(),),
            ).fetchone()
    except Exception:
        return ""
    if not row or not row[0]:
        return ""
    first = str(row[0]).replace("\\n", "\n").split("\n", 1)[0].strip()
    return first[:40]


_TAG_LABELS = {
    "zk": "中考",
    "gk": "高考",
    "cet4": "四级",
    "cet6": "六级",
    "ky": "考研",
    "toefl": "托福",
    "ielts": "雅思",
    "gre": "GRE",
}


def _as_int(value) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


def lookup_ecdict_meta(word: str) -> dict:
    """Return frequency/exam metadata for a word from ECDICT; {} when unavailable."""
    conn = _get_ecdict_conn()
    if conn is None:
        return {}
    try:
        with _ECDICT_LOCK:
            row = conn.execute(
                "SELECT frq, bnc, collins, oxford, tag FROM words WHERE word = ?",
                (word.strip(),),
            ).fetchone()
    except Exception:
        return {}
    if not row:
        return {}
    frq, bnc, collins, oxford, tag = row
    return {
        "frq": _as_int(frq),
        "bnc": _as_int(bnc),
        "collins": _as_int(collins),
        "oxford": str(oxford or "").strip() == "1",
        "tags": [_TAG_LABELS[t] for t in str(tag or "").split() if t in _TAG_LABELS],
    }


def format_ecdict_meta(meta: dict) -> str:
    """Render metadata as a compact display line, e.g. 'COCA #1523 · 牛津3000'.
    考试标签（四级/雅思/GRE 等）按 may 要求不展示；tags 数据保留在 lookup 结果中。"""
    if not meta:
        return ""
    parts = []
    if meta.get("frq"):
        parts.append(f"COCA #{meta['frq']}")
    if meta.get("oxford"):
        parts.append("牛津3000")
    if meta.get("collins"):
        parts.append(f"柯林斯{meta['collins']}★")
    return " · ".join(parts)


def ecdict_frq_map(words: list[str]) -> dict[str, int]:
    """Batch COCA frequency ranks (frq) for words; missing words/ranks are omitted."""
    conn = _get_ecdict_conn()
    if conn is None or not words:
        return {}
    result: dict[str, int] = {}
    try:
        with _ECDICT_LOCK:
            for start in range(0, len(words), 500):
                batch = words[start:start + 500]
                placeholders = ",".join("?" for _ in batch)
                for word, frq in conn.execute(
                    f"SELECT word, frq FROM words WHERE word IN ({placeholders})", batch
                ):
                    rank = _as_int(frq)
                    if rank is not None:
                        result[word.lower()] = rank
    except Exception:
        pass
    return result


ipa_ready = False


def init_ipa() -> None:
    global ipa_ready
    try:
        import eng_to_ipa  # noqa: F401
        from phonetics_processor import annotate  # noqa: F401
        ipa_ready = True
    except ImportError:
        ipa_ready = False


init_ipa()


def strip_ipa_asterisks(raw: str) -> str:
    return re.sub(r"\*([^*]+)\*", r"\1", raw)
