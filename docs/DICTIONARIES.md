# Dictionaries (MDX)

The app can look up words offline in MDX dictionaries (via `mdict-utils`) to enrich word analysis — no API cost per lookup.

**These dictionaries are copyrighted and are NOT distributed with this repository.** Obtain them for personal use only.

## Sources

| Dictionary | Expected filename | Source |
|---|---|---|
| Oxford Advanced Learner's Dictionary 9th (OALD 9) | `oald9.mdx` (+ `oald9.mdd`, `oald9.1.mdd`, `oald9.2.mdd`) | [FreeMdict Forum — 牛津九 online 精装版](https://forum.freemdict.com/t/topic/3796) (「牛津9OL版 2020.01.01」MDX/MDD) |
| Longman Dictionary of Contemporary English 6th | `LongmanDictionaryOfContemporaryEnglish6thEnEn.mdx` | search [FreeMdict Forum](https://forum.freemdict.com/) |
| Vocabulary.com Dictionary | `Vocabulary.com Dictionary.mdx` (+ `.mdd`) | search [FreeMdict Forum](https://forum.freemdict.com/) |

Filenames must match exactly — the app looks them up by name.

## Setup

1. Put the `.mdx`/`.mdd` files in one directory.
2. Point `DICT_DIR` at it in `.env`:

   ```ini
   DICT_DIR=C:\path\to\your\dicts
   ```

   If `DICT_DIR` is empty, the app falls back to the Eudic (欧路词典) dictionary folder:
   `~/AppData/Roaming/Francochinois/eudic/dict` — so if you already use Eudic with these dictionaries imported, no configuration is needed.

3. Restart the server and check `GET /health` — the `dicts` section shows which dictionaries were found:

   ```json
   "dicts": {"oald": true, "longman": true, "vocab": true}
   ```

Missing dictionaries are skipped gracefully — the app works without them, word analysis just falls back to AI-only content.
