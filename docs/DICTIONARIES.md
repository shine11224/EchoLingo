# Dictionaries

## Built-in: ECDICT

EchoLingo's dictionary is [ECDICT](https://github.com/skywind3000/ECDICT) (MIT License) — a free English-Chinese database with ~770K entries, IPA phonetics, word frequency (BNC/COCA) and inflection data. A ready-to-use `resources/ecdict/ecdict.db` is bundled with EchoLingo, so a fresh clone works immediately.

To refresh the database from the current upstream CSV, run:

```bash
python backend/build_ecdict.py
```

This downloads `ecdict.csv` and replaces `resources/ecdict/ecdict.db` (~90 MB). The downloaded CSV remains ignored by Git. Check `GET /health` — `"dicts": {"ecdict": true}` confirms the bundled or rebuilt database is available.

ECDICT powers:

- word lookup and AI word analysis in lesson and reading pages
- local word-family (inflection) expansion for wordlists
- fallback Chinese glosses when no translation service is configured
