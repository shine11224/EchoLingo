"""Build the compact gloss dictionary shipped with the repo.

The full ECDICT sqlite (`resources/ecdict/ecdict.db`, ~90MB) is downloaded
data and never shipped. Public deployments still need short Chinese glosses
for lookup-mode badges and saved-word backfill, so this script extracts a
small subset into `resources/ecdict/gloss_compact.db`:

  word  -> first Chinese sense (single line, <= 40 chars)

Coverage: every ECDICT entry carrying a frequency tag (BNC / COCA / Collins /
Oxford) plus every word in the bundled builtin wordlists — the words a learner
is realistically going to save. Rare jargon without any frequency tag stays
exclusive to the full ECDICT (private/dev deployments).

Usage:
    python backend/build_compact_gloss.py
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
ECDICT_DB = PROJECT_ROOT / "resources" / "ecdict" / "ecdict.db"
COMPACT_DB = PROJECT_ROOT / "resources" / "ecdict" / "gloss_compact.db"
BUILTIN_GLOBS = ("builtin_*.json",)
WORDLISTS_DIR = PROJECT_ROOT / "resources" / "wordlists" / "wordlists" / "compiled"


def _first_sense(translation: str) -> str:
    first = str(translation or "").replace("\\n", "\n").split("\n", 1)[0].strip()
    return first[:40]


def _builtin_words() -> set[str]:
    words: set[str] = set()
    for pattern in BUILTIN_GLOBS:
        for path in sorted(WORDLISTS_DIR.glob(pattern)):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            for word in data.get("words") or []:
                word = str(word).strip().lower()
                if word:
                    words.add(word)
    return words


def build_compact_gloss() -> dict:
    if not ECDICT_DB.exists():
        raise FileNotFoundError(f"full ECDICT not found: {ECDICT_DB}")
    keep = _builtin_words()
    src = sqlite3.connect(str(ECDICT_DB))
    try:
        # 单趟扫描：频率词（BNC/COCA/Collins/Oxford 任一标记）全收；内置词表词无标记也收
        rows = src.execute(
            "SELECT word, translation,"
            " (bnc > 0 OR frq > 0 OR collins > 0 OR oxford > 0) AS has_freq"
            " FROM words WHERE translation IS NOT NULL AND translation != ''"
        ).fetchall()
    finally:
        src.close()

    selected: dict[str, str] = {}
    for word, translation, has_freq in rows:
        key = str(word).strip().lower()
        if not key or (not has_freq and key not in keep):
            continue
        sense = _first_sense(translation)
        if sense:
            selected[key] = sense

    COMPACT_DB.parent.mkdir(parents=True, exist_ok=True)
    if COMPACT_DB.exists():
        COMPACT_DB.unlink()
    dst = sqlite3.connect(str(COMPACT_DB))
    try:
        dst.execute("CREATE TABLE gloss (word TEXT PRIMARY KEY, zh TEXT NOT NULL)")
        dst.executemany(
            "INSERT INTO gloss (word, zh) VALUES (?, ?)",
            sorted(selected.items()),
        )
        dst.execute("CREATE INDEX IF NOT EXISTS idx_gloss_word ON gloss(word)")
        dst.commit()
        dst.execute("VACUUM")
    finally:
        dst.close()
    return {"entries": len(selected), "path": str(COMPACT_DB)}


def main() -> None:
    result = build_compact_gloss()
    size_mb = COMPACT_DB.stat().st_size / 1024 / 1024
    print(f"compact gloss: {result['entries']} entries, {size_mb:.1f}MB -> {result['path']}")


if __name__ == "__main__":
    main()
