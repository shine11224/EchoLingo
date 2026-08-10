"""Dictionary and IPA helpers shared by FastAPI routes."""
from __future__ import annotations

import re
import threading
from pathlib import Path

ECDICT_DB = Path(__file__).resolve().parents[3] / "resources" / "ecdict" / "ecdict.db"
# 随库分发的紧凑释义库（build_compact_gloss.py 生成）：全量 ECDICT 缺失时兜底角标级释义
COMPACT_GLOSS_DB = Path(__file__).resolve().parents[3] / "resources" / "ecdict" / "gloss_compact.db"

_ECDICT_CONN = None
_ECDICT_LOCK = threading.Lock()
_COMPACT_CONN = None


def _get_compact_conn():
    global _COMPACT_CONN
    if _COMPACT_CONN is not None:
        return _COMPACT_CONN
    with _ECDICT_LOCK:
        if _COMPACT_CONN is None and COMPACT_GLOSS_DB.exists():
            import sqlite3

            _COMPACT_CONN = sqlite3.connect(str(COMPACT_GLOSS_DB), check_same_thread=False)
    return _COMPACT_CONN


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


def word_in_dict(word: str) -> bool:
    """词是否被词典覆盖（全量 ECDICT 优先，缺失时查 compact 释义库）。

    供 PDF 后处理做「粘连词是否该拆」之类的存在性判断。"""
    key = word.strip().lower()
    if not key:
        return False
    conn = _get_ecdict_conn()
    if conn is None:
        compact = _get_compact_conn()  # 内部用同一把锁，须在 with 块外获取
        if compact is None:
            return False
    else:
        compact = None
    try:
        with _ECDICT_LOCK:
            target = conn if conn is not None else compact
            table = "words" if conn is not None else "gloss"
            row = target.execute(f"SELECT 1 FROM {table} WHERE word = ?", (key,)).fetchone()
            return row is not None
    except Exception:
        return False


def lookup_ecdict_translation(word: str) -> str:
    """仅取 ECDICT 中文释义的首个义项（角标级短文本）；缺失返回 ''。

    全量 ECDICT 不在时回退随库分发的 compact 释义库（已预取首义项）。"""
    conn = _get_ecdict_conn()
    if conn is None:
        compact = _get_compact_conn()
        if compact is None:
            return ""
        try:
            with _ECDICT_LOCK:
                row = compact.execute(
                    "SELECT zh FROM gloss WHERE word = ?",
                    (word.strip().lower(),),
                ).fetchone()
        except Exception:
            return ""
        return str(row[0])[:40] if row and row[0] else ""
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


def lookup_ecdict_phonetic(word: str) -> str:
    """取 ECDICT 音标（不含斜杠）；缺失返回 ''。依次尝试原词小写、撇号所有格词干、
    简单复数/三单去 s——用于逐词音标行兜底（whisper/Groq 对齐无音素数据）。"""
    conn = _get_ecdict_conn()
    if conn is None:
        return ""

    def query(candidate: str) -> str:
        if not candidate:
            return ""
        try:
            with _ECDICT_LOCK:
                row = conn.execute(
                    "SELECT phonetic FROM words WHERE word = ?",
                    (candidate,),
                ).fetchone()
        except Exception:
            return ""
        return str(row[0]).strip() if row and row[0] else ""

    base = re.sub(r"[^a-zA-Z'-]", "", str(word or "")).lower()
    if not base:
        return ""
    candidates = [base]
    if "'" in base:
        candidates.append(base.split("'", 1)[0])          # it's → it, don't → don
    if base.endswith("s") and len(base) > 3:
        candidates.append(base[:-1])                       # ideas → idea
    if base.endswith("ies") and len(base) > 4:
        candidates.append(base[:-3] + "y")                 # studies → study
    for candidate in candidates:
        phonetic = query(candidate)
        if phonetic:
            return phonetic
    return ""


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


def ecdict_meta_map(words: list[str]) -> dict[str, dict]:
    """Batch frequency + exam-tag metadata for words; missing words are omitted.

    Returns {word_lower: {"frq": int | None, "exam_tags": [label, ...]}}.
    """
    conn = _get_ecdict_conn()
    if conn is None or not words:
        return {}
    result: dict[str, dict] = {}
    try:
        with _ECDICT_LOCK:
            for start in range(0, len(words), 500):
                batch = words[start:start + 500]
                placeholders = ",".join("?" for _ in batch)
                for word, frq, tag in conn.execute(
                    f"SELECT word, frq, tag FROM words WHERE word IN ({placeholders})", batch
                ):
                    result[word.lower()] = {
                        "frq": _as_int(frq),
                        "exam_tags": [
                            _TAG_LABELS[t] for t in str(tag or "").split() if t in _TAG_LABELS
                        ],
                    }
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


def eng_ipa(word: str) -> str:
    """eng_to_ipa 程序化音标（标准音，无真实音变）；库缺失或词不可转返回 ''。"""
    token = re.sub(r"[^a-zA-Z'-]", "", str(word or "")).lower()
    if not token:
        return ""
    try:
        import eng_to_ipa as ipa_lib

        raw = str(ipa_lib.convert(token) or "").strip()
    except Exception:
        return ""
    if not raw or "*" in raw:  # *word* = eng_to_ipa 未收录，原样回显无价值
        return ""
    return raw
