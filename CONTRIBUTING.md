# Contributing

Thanks for your interest! This project is a local-first English learning tool — contributions of bug fixes, new content sources, and learning-flow improvements are welcome.

## Development setup

Requires Python 3.11+ and ffmpeg on PATH.

```bash
git clone <this-repo>
cd english-learning-tool-public
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux
pip install -r requirements.txt
cp .env.example .env          # fill in your API keys
python backend/fastapi_server.py
```

## Running tests

```bash
# Full suite (fast, no network required; AI-dependent paths use mocks)
python -m pytest -q

# Smoke checks
python tests/smoke_generate.py --test-client
python tests/smoke_browser.py          # needs: playwright install chromium
```

Please keep the suite green: run `python -m pytest -q` before submitting. If your change touches the home/lesson pages, note that several tests assert UI contracts (DOM ids and JS function names in `frontend/templates/*.html`) — update the contract and the test together.

## Project layout

- `backend/` — FastAPI server (`fastapi_server.py`), CLI lesson generation (`main.py`), input adapters (`sources/`), routes/services/storage (`webapp/`)
- `frontend/` — Jinja templates and static JS (no build step)
- `tests/` — pytest suite + smoke scripts
- `scripts/` — segmentation and MFA-alignment QA tools
- `resources/wordlists/` — wordlist sources (user-provided) and compiled lists; see `docs/WORDLISTS.md`

## Conventions

- **Prompts** for AI features live in `backend/prompts.py` — change prompts only there.
- **Compiled wordlists** under `resources/wordlists/wordlists/compiled/` are build artifacts of `backend/build_wordlists_bnc.py`; don't edit them by hand.
- **No secrets, ever**: `.env`, `cookies.txt`, `vocab.db`, and media files must not be committed (see `SECURITY.md`).
- Keep dependencies minimal — propose new packages in an issue before adding them to `requirements.txt`.

## Pull requests

1. Fork and create a branch from `main` (`feat/...` or `fix/...`).
2. Keep PRs focused: one feature or fix per PR.
3. Include test coverage for behavior changes; update `README.md` / `docs/` if user-facing behavior changes.
4. Commit messages: conventional style (`feat:`, `fix:`, `chore:`, `docs:`) is appreciated.

## Reporting bugs / requesting features

Open an issue with:

- **Bugs**: what you did, what happened, what you expected, and the relevant server log or browser console error.
- **Features**: the learning scenario you're trying to improve — concrete examples (a video type, a course format) help a lot.
