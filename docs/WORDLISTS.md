# Wordlists

The app highlights words by difficulty (CEFR B1/B2/C1) and domain (business, academic, fitness, medical). These graded lists are **compiled from third-party frequency lists that are not bundled with this repository** for licensing reasons.

Everything works without them — the app just skips word-level highlighting until the lists are built.

## Quick build

1. Download the source lists (links below) and place them in `resources/wordlists/wordlists/`:

   | Expected filename | Source |
   |---|---|
   | `bnc_coca_9k.csv` | BNC/COCA 9K frequency word-family list (search "BNC COCA 25000 word family list" — the 9K teaching subset, columns: `group, headword, total_freq, word_forms`) |
   | `BSL_1.20_lemmatized_for_teaching.csv` | Business Service List — https://www.victoria.ac.nz/lals/centres-and-institutes/call-research/resources/business-service-list |
   | `NAWL_1.2_lemmatized_for_teaching.csv` | New Academic Word List — https://www.victoria.ac.nz/lals/centres-and-institutes/call-research/resources |
   | `FEL_1.0_alphabetized_description.txt` | Fitness English List (search "FEL fitness English word list") |
   | `Oral+English+Medical+Corpus.xlsx` | Optional — medical spoken-English corpus |

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
