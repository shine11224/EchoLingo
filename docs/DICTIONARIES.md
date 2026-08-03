# Dictionaries

## Built-in: ECDICT

EchoLingo's dictionary is [ECDICT](https://github.com/skywind3000/ECDICT) (MIT License) — a free English-Chinese database with ~770K entries, IPA phonetics, word frequency (BNC/COCA) and inflection data. It is **not bundled in the repo** (size); build it once:

```bash
python backend/build_ecdict.py
```

This downloads `ecdict.csv` and compiles `resources/ecdict/ecdict.db` (~90 MB). Everything works offline afterwards. Check `GET /health` — `"dicts": {"ecdict": true}` confirms it.

ECDICT powers:

- word lookup and AI word analysis in lesson and reading pages
- local word-family (inflection) expansion for wordlists
- fallback Chinese glosses when no translation service is configured
