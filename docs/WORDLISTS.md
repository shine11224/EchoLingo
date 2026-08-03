# Wordlists

## Built-in wordlists (no downloads needed)

`python backend/build_ecdict.py` compiles nine built-in wordlists from the local ECDICT database (MIT) into `resources/wordlists/wordlists/compiled/`:

| List | Content |
|---|---|
| 常用词（COCA 前 2000） | top-2000 COCA frequency — used to hide common words |
| 中高频（COCA 2001-5000） | COCA ranks 2001–5000 |
| 牛津 3000 核心词 | Oxford 3000 core vocabulary |
| 四级 / 六级 / 考研 重点词 | CET-4 / CET-6 / postgraduate-exam focus words |
| 雅思 / 托福 / GRE 重点词 | IELTS / TOEFL / GRE focus words |

Every list includes attested inflections (plurals, verb forms, comparatives) from ECDICT's exchange data. Enable or disable them on the lesson page like any user-uploaded wordlist; the exam lists are "focus word" lists, not complete official syllabi.

## Optional third-party graded lists

The app can additionally highlight words by difficulty (CEFR B1/B2/C1) and domain (business, academic, fitness, medical). These graded lists are **compiled from third-party frequency lists that are not bundled with this repository** for licensing reasons.

Everything works without them — the built-in lists above already cover highlighting.

## Quick build

1. Download the source lists (links below) and place them in `resources/wordlists/wordlists/`:

   | Expected filename | Source | License |
   |---|---|---|
   | `bnc_coca_9k.csv` | BNC/COCA word-family frequency list (search "BNC COCA 25000 word family list" — the 9K teaching subset, columns: `group, headword, total_freq, word_forms`) | Free for research/teaching use; redistribution terms unclear — that is why it is not bundled |
   | `BSL_1.20_lemmatized_for_teaching.csv` | Business Service List — https://www.victoria.ac.nz/lals/centres-and-institutes/call-research/resources/business-service-list | CC BY-SA 4.0 (Browne & Culligan) |
   | `NAWL_1.2_lemmatized_for_teaching.csv` | New Academic Word List — https://www.victoria.ac.nz/lals/centres-and-institutes/call-research/resources | CC BY-SA 4.0 (Browne, Culligan & Phillips) |
   | `FEL_1.0_alphabetized_description.txt` | Fitness English List — https://www.newgeneralservicelist.com/fitness-english-list | NGSL project, free to use with attribution (Browne & Culligan, 2020) |

   Note on the CC BY-SA lists: the JSON files you compile locally (`compiled/domain_business.json`, `compiled/domain_academic.json`) are derivatives — if you ever share them, CC BY-SA 4.0 requires the same attribution and license. Keeping them on your own machine for personal use needs nothing further.

2. Build the compiled JSON lists:

   ```bash
   python backend/build_wordlists_bnc.py
   ```

   This writes `resources/wordlists/wordlists/compiled/`:
   `exclude_a1a2.json`, `cefr_b1.json`, `cefr_b2.json`, `cefr_c1.json`, `domain_*.json`.

   Tier mapping is frequency-based: 1K–2K → excluded (too basic), 3K–4K → B1, 5K–6K → B2, 7K–9K → C1.

3. Restart the server — no other configuration needed.

## Custom wordlists

You can upload your own `.txt` or `.csv` wordlists from the web UI (Resources page). They are compiled to `compiled/user_<name>.json` and can be toggled on/off independently.

## Bundled data

`compiled/sentence_patterns.json` is an original, self-authored sentence-pattern library (e.g. "What I think is that…" with usage explanations) and is distributed with this repo.
