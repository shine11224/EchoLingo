# -*- coding: utf-8 -*-
"""
Rebuild CEFR wordlists from BNC/COCA 9K frequency list.

Source: bnc_coca_9k.csv
  columns: group, headword, total_freq, word_forms

Tier mapping (frequency-based, research-grounded):
  exclude : 1k + 2k  → too basic, don't highlight
  B1      : 3k + 4k  → learner target zone (2K-4K)
  B2      : 5k + 6k  → intermediate/advanced (4K-6K)
  C1      : 7k + 8k + 9k → low-frequency / challenging (6K-9K)

Domain wordlists (business/academic/fitness/medical) kept from existing sources.
"""

from __future__ import annotations
import csv
import json
import re
import sys
import io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

WORDLIST_DIR = Path(__file__).resolve().parents[1] / "resources" / "wordlists" / "wordlists"
OUT_DIR = WORDLIST_DIR / "compiled"
OUT_DIR.mkdir(exist_ok=True)

BNC_CSV = WORDLIST_DIR / "bnc_coca_9k.csv"

TIER_MAP = {
    "exclude": {"1k", "2k"},
    "b1":      {"3k", "4k"},
    "b2":      {"5k", "6k"},
    "c1":      {"7k", "8k", "9k"},
}


def parse_word_forms(forms_str: str) -> set[str]:
    """Extract word forms from 'form1 (freq1), form2 (freq2), ...' string."""
    words: set[str] = set()
    for part in forms_str.split("),"):
        # Each part looks like "word (12345" or "word (12345)"
        token = part.strip().split("(")[0].strip().lower()
        # Keep only clean alphabetic forms, min length 3
        if re.match(r"^[a-z][a-z'-]{1,}$", token) and len(token) >= 3:
            words.add(token)
    return words


def build_bnc_tiers() -> dict[str, set[str]]:
    tiers: dict[str, set[str]] = {k: set() for k in TIER_MAP}

    with open(BNC_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            group = row["group"].strip()
            headword = row["headword"].strip().lower()
            forms_str = row.get("word_forms", "").strip()

            # Determine which tier this group belongs to
            target_tier = None
            for tier, groups in TIER_MAP.items():
                if group in groups:
                    target_tier = tier
                    break
            if target_tier is None:
                continue

            # Add headword itself
            if len(headword) >= 3 and re.match(r"^[a-z][a-z'-]*$", headword):
                tiers[target_tier].add(headword)

            # Add all inflected forms
            tiers[target_tier].update(parse_word_forms(forms_str))

    return tiers


def save_json(words: set[str], path: Path, label: str) -> None:
    data = {"words": sorted(words)}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  Saved {label}: {len(words)} words → {path.name}")


def load_csv_words(csv_path: Path) -> set[str]:
    words: set[str] = set()
    for enc in ["utf-8", "latin-1", "cp1252"]:
        try:
            with open(csv_path, encoding=enc) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    for form in line.split(","):
                        form = form.strip().lower()
                        if form and re.match(r"^[a-z][a-z\-]*$", form) and len(form) >= 3:
                            words.add(form)
            break
        except UnicodeDecodeError:
            continue
    return words


def load_txt_words(txt_path: Path) -> set[str]:
    words: set[str] = set()
    for enc in ["utf-8", "latin-1"]:
        try:
            with open(txt_path, encoding=enc) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    word = re.split(r"[\s/,]", line)[0].strip().lower()
                    if word and re.match(r"^[a-z][a-z\-]*$", word) and len(word) >= 3:
                        words.add(word)
            break
        except UnicodeDecodeError:
            continue
    return words


def load_xlsx_words(xlsx_path: Path) -> set[str]:
    try:
        import openpyxl
    except ImportError:
        print("  [skip] openpyxl not installed")
        return set()
    words: set[str] = set()
    wb = openpyxl.load_workbook(str(xlsx_path), read_only=True, data_only=True)
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            for cell in row:
                if not cell:
                    continue
                for token in re.split(r"[\s,;/]+", str(cell)):
                    token = token.strip().lower()
                    if token and re.match(r"^[a-z][a-z\-]*$", token) and len(token) >= 3:
                        words.add(token)
    return words


def main() -> None:
    print(f"Building wordlists from BNC/COCA 9K: {BNC_CSV}\n")

    # ── CEFR tiers from BNC/COCA ─────────────────────────────────────
    print("Parsing BNC/COCA frequency tiers...")
    tiers = build_bnc_tiers()

    for tier, words in tiers.items():
        print(f"  {tier}: {len(words)} word forms")

    save_json(tiers["exclude"], OUT_DIR / "exclude_a1a2.json", "Exclude (1K-2K)")
    save_json(tiers["b1"],      OUT_DIR / "cefr_b1.json",     "B1 (3K-4K)")
    save_json(tiers["b2"],      OUT_DIR / "cefr_b2.json",     "B2 (5K-6K)")
    save_json(tiers["c1"],      OUT_DIR / "cefr_c1.json",     "C1 (7K-9K)")

    # ── Domain wordlists (unchanged) ─────────────────────────────────
    print("\nBuilding domain word sets...")

    bsl = load_csv_words(WORDLIST_DIR / "BSL_1.20_lemmatized_for_teaching.csv")
    save_json(bsl, OUT_DIR / "domain_business.json", "Business (BSL)")

    nawl = load_csv_words(WORDLIST_DIR / "NAWL_1.2_lemmatized_for_teaching.csv")
    save_json(nawl, OUT_DIR / "domain_academic.json", "Academic (NAWL)")

    fel = load_txt_words(WORDLIST_DIR / "FEL_1.0_alphabetized_description.txt")
    save_json(fel, OUT_DIR / "domain_fitness.json", "Fitness (FEL)")

    medical_path = WORDLIST_DIR / "Oral+English+Medical+Corpus.xlsx"
    if medical_path.exists():
        medical = load_xlsx_words(medical_path)
        save_json(medical, OUT_DIR / "domain_medical.json", "Medical")
    else:
        print("  [skip] Medical xlsx not found")

    # ── Summary ──────────────────────────────────────────────────────
    print("\n=== Done ===")
    for f in sorted(OUT_DIR.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        print(f"  {f.name}: {len(data['words'])} words")

    # ── Quick sanity check ───────────────────────────────────────────
    print("\n--- Sanity check (sample B2 words) ---")
    b2_sample = sorted(tiers["b2"])[:20]
    print(b2_sample)
    print("\n--- Sample C1 words ---")
    c1_sample = sorted(tiers["c1"])[:20]
    print(c1_sample)


if __name__ == "__main__":
    main()
