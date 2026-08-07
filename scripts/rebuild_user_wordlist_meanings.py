"""
Rebuild Chinese meanings in compiled user wordlists from the bundled
ECDICT (MIT) SQLite — replaces the retired MDX (OALD9/Longman) pipeline.

Usage: python scripts/rebuild_user_wordlist_meanings.py [--dry-run]
"""
import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
COMPILED_DIR = BASE_DIR / "resources" / "wordlists" / "wordlists" / "compiled"
ECDICT_DB = BASE_DIR / "resources" / "ecdict" / "ecdict.db"


def concise_meaning(translation: str, limit: int = 40) -> str:
    """Turn an ECDICT translation cell into a compact gloss."""
    text = str(translation or "").replace("\\n", "\n")
    parts = [p.strip() for p in text.split("\n") if p.strip()]
    gloss = "; ".join(parts)
    gloss = re.sub(r"\s+", " ", gloss).strip()
    return gloss[:limit].rstrip(";, ")


def ecdict_meaning(conn: sqlite3.Connection, word: str) -> str:
    row = conn.execute(
        "SELECT translation FROM words WHERE word = ?", (word,)
    ).fetchone()
    if row and row[0]:
        return concise_meaning(row[0])
    # inflected form fallback: strip trailing s/es and retry
    for stem in (re.sub(r"(es|s)$", "", word),):
        if stem and stem != word:
            row = conn.execute(
                "SELECT translation FROM words WHERE word = ?", (stem,)
            ).fetchone()
            if row and row[0]:
                return concise_meaning(row[0])
    return ""


def rebuild(dry_run: bool = False) -> None:
    if not ECDICT_DB.exists():
        print(f"ERROR: ECDICT db not found at {ECDICT_DB}")
        sys.exit(1)
    conn = sqlite3.connect(str(ECDICT_DB))

    for json_path in sorted(COMPILED_DIR.glob("user_*.json")):
        data = json.loads(json_path.read_text(encoding="utf-8"))
        raw_words = data.get("words", [])
        if isinstance(raw_words, dict):
            words = list(raw_words.keys())
        else:
            words = [str(w).strip().lower() for w in raw_words]
        if not words:
            continue

        rebuilt = {}
        found = 0
        for w in words:
            meaning = ecdict_meaning(conn, w)
            if meaning:
                found += 1
            rebuilt[w] = {"basic_meaning": meaning}

        samples = [(w, v["basic_meaning"]) for w, v in sorted(rebuilt.items()) if v["basic_meaning"]]
        print(f"  {json_path.name}: {found}/{len(words)} with ECDICT meanings")
        for w, m in samples[:5]:
            print(f"    {w}: {m}")

        if not dry_run:
            data["words"] = rebuilt
            json_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"    → written")
    print("Done")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    rebuild(dry_run=args.dry_run)
