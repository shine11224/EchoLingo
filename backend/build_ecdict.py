"""Download ECDICT (MIT, skywind3000/ECDICT) and build the local SQLite dictionary.

Usage:
    python backend/build_ecdict.py            # download (if missing) + build
    python backend/build_ecdict.py --rebuild  # rebuild db from existing csv

Output:
    resources/ecdict/ecdict.csv  (source, ~66MB, gitignored)
    resources/ecdict/ecdict.db   (SQLite, gitignored, loaded by webapp/services/dicts.py)
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

CSV_URL = "https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv"
DATA_DIR = Path(__file__).resolve().parents[1] / "resources" / "ecdict"
CSV_PATH = DATA_DIR / "ecdict.csv"
DB_PATH = DATA_DIR / "ecdict.db"
COMPILED_DIR = Path(__file__).resolve().parents[1] / "resources" / "wordlists" / "wordlists" / "compiled"

FIELDS = ("word", "phonetic", "definition", "translation", "pos", "collins", "oxford", "tag", "bnc", "frq", "exchange")

BUILTIN_LISTS = [
    ("builtin_coca_2000", "常用词（COCA 前 2000）", "COCA2K", "exclude", "CAST(frq AS INTEGER) BETWEEN 1 AND 2000"),
    ("builtin_coca_5000", "中高频（COCA 2001-5000）", "COCA5K", "domain", "CAST(frq AS INTEGER) BETWEEN 2001 AND 5000"),
    ("builtin_oxford3000", "牛津 3000 核心词", "牛津3K", "domain", "oxford = 1"),
    ("builtin_cet4", "四级重点词", "四级", "domain", "' ' || tag || ' ' LIKE '% cet4 %'"),
    ("builtin_cet6", "六级重点词", "六级", "domain", "' ' || tag || ' ' LIKE '% cet6 %'"),
    ("builtin_ky", "考研重点词", "考研", "domain", "' ' || tag || ' ' LIKE '% ky %'"),
    ("builtin_ielts", "雅思重点词", "雅思", "domain", "' ' || tag || ' ' LIKE '% ielts %'"),
    ("builtin_toefl", "托福重点词", "托福", "domain", "' ' || tag || ' ' LIKE '% toefl %'"),
    ("builtin_gre", "GRE 重点词", "GRE", "domain", "' ' || tag || ' ' LIKE '% gre %'"),
]


def download(url: str = CSV_URL) -> None:
    import requests

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CSV_PATH.with_suffix(".csv.tmp")
    print(f"[ecdict] downloading {url}")
    with requests.get(url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length") or 0)
        done = 0
        with tmp.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r[ecdict] {done * 100 // total}%", end="", flush=True)
    print()
    tmp.replace(CSV_PATH)
    print(f"[ecdict] saved {CSV_PATH} ({CSV_PATH.stat().st_size / 1e6:.1f} MB)")


def build() -> None:
    if not CSV_PATH.exists():
        raise SystemExit(f"missing {CSV_PATH} — run without --rebuild first")
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE words ("
        "word TEXT PRIMARY KEY COLLATE NOCASE, "
        "phonetic TEXT, definition TEXT, translation TEXT, pos TEXT, "
        "collins TEXT, oxford TEXT, tag TEXT, bnc TEXT, frq TEXT, exchange TEXT)"
    )
    count = 0
    with CSV_PATH.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        batch = []
        for row in reader:
            batch.append(tuple((row.get(f) or "").strip() for f in FIELDS))
            if len(batch) >= 5000:
                conn.executemany("INSERT OR REPLACE INTO words VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch)
                count += len(batch)
                batch.clear()
                print(f"\r[ecdict] {count} words", end="", flush=True)
        if batch:
            conn.executemany("INSERT OR REPLACE INTO words VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch)
            count += len(batch)
    conn.commit()
    conn.execute("PRAGMA optimize")
    conn.close()
    print(f"\n[ecdict] built {DB_PATH} with {count} words ({DB_PATH.stat().st_size / 1e6:.1f} MB)")


def build_builtin_wordlists() -> list[str]:
    """Compile built-in frequency/exam wordlists from ecdict.db into compiled/."""
    import json

    from webapp.storage.wordlists import expand_with_local_word_families

    if not DB_PATH.exists():
        print("[ecdict] db missing, skip builtin wordlists")
        return []
    COMPILED_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    written = []
    try:
        for list_id, name, tag, kind, where in BUILTIN_LISTS:
            base = [row[0] for row in conn.execute(f"SELECT word FROM words WHERE {where}")]
            words = {w.lower() for w in base}
            families = expand_with_local_word_families(sorted(words))
            for family in families.values():
                words.update(family)
            payload = {
                "metadata": {
                    "name": name,
                    "type": kind,
                    "key": list_id,
                    "color": "domain-academic",
                    "tag": tag,
                    "builtin": True,
                },
                "words": sorted(words),
            }
            (COMPILED_DIR / f"{list_id}.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            written.append(list_id)
            print(f"[ecdict] {list_id}: {len(words)} words")
    finally:
        conn.close()
    return written


def ensure_builtin_wordlists() -> str:
    """Startup self-heal: regenerate builtin wordlists when missing and ecdict.db exists.

    Returns 'present' | 'regenerated' | 'no-db' | 'failed'. Never raises —
    a wordlist problem must not crash server startup.
    """
    expected = [f"{list_id}.json" for list_id, *_ in BUILTIN_LISTS]
    if all((COMPILED_DIR / name).exists() for name in expected):
        return "present"
    if not DB_PATH.exists():
        return "no-db"
    try:
        build_builtin_wordlists()
    except Exception as exc:
        print(f"[ecdict] builtin wordlist regeneration failed: {exc}")
        return "failed"
    return "regenerated"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rebuild", action="store_true", help="rebuild db from existing csv")
    args = parser.parse_args()
    if not args.rebuild and not CSV_PATH.exists():
        download()
    build()
    build_builtin_wordlists()


if __name__ == "__main__":
    sys.exit(main())
