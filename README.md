# English Learning Tool

Turn any English video, audio, or article into an interactive listening/reading lesson page — with subtitles, translation, IPA, word-level analysis, and vocabulary review.

把任意英语视频 / 音频 / 文章变成交互式精学页面：字幕、翻译、音标、逐词分析和词汇复习一站完成。

## Features

- **Multiple sources** — YouTube, Bilibili, article URLs, local video/audio, plain text/Markdown
- **Lesson generation** — subtitle fetching or local faster-whisper transcription, sentence segmentation, translation, IPA annotation, connected-speech and oral analysis
- **Interactive lesson pages** — per-sentence loop playback, word lookup, AI hints and practice, pronunciation assessment (optional Azure)
- **Vocabulary system** — CEFR-graded word highlighting (B1/B2/C1), domain wordlists, personal vocab book with review, memory stories, and exports (Markdown / HTML)
- **Local-first** — lessons, vocab DB, and caches stay on your machine; AI features work with any OpenAI-compatible API (DeepSeek, OpenAI, Groq, Ollama, …)

## Quick Start

Requires Python 3.11+ and [ffmpeg](https://ffmpeg.org/) on PATH.

```bash
git clone <this-repo>
cd english-learning-tool-public
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux
pip install -r requirements.txt
```

Configure API keys:

```bash
cp .env.example .env          # then edit .env
```

Start the server:

```bash
python backend/fastapi_server.py
# open http://localhost:5173
```

Verify the environment:

```bash
curl http://localhost:5173/health
```

## Generating a Lesson (CLI)

```bash
# Article (fastest way to try the pipeline)
python backend/main.py --article "https://example.com/article"

# Bilibili (audio by default; pick an episode of multi-part videos)
python backend/main.py --bilibili "https://www.bilibili.com/video/BVxxxxxxxxxxx" --bilibili-page 3

# YouTube
python backend/main.py --youtube "https://www.youtube.com/watch?v=VIDEO_ID" --youtube-cookies cookies.txt

# Local media (with or without subtitles; falls back to faster-whisper)
python backend/main.py --video-file sample.mp4 --transcript-file sample.vtt

# Plain text / Markdown (no audio)
python backend/main.py --text-file notes.md --text-title "My Reading"
```

Generated pages land in `output/` and appear on the server home page. You can also generate from the web UI.

## Configuration (`.env`)

| Variable | Required | Purpose |
|---|---|---|
| `AI_API_KEY` | Yes* | Any OpenAI-compatible API key (DeepSeek, OpenAI, Groq, Ollama…). *Without it, AI features fall back to mock/disabled. |
| `AI_BASE_URL` | No | API endpoint, default `https://api.deepseek.com` |
| `AI_MODEL` | No | Model name, default `deepseek-chat` |
| `GROQ_API_KEY` | No | Speeds up Whisper transcription for local media without subtitles |
| `AZURE_SPEECH_KEY` / `AZURE_SPEECH_REGION` | No | Pronunciation assessment (Azure free tier works) |
| `DICT_DIR` | No | Directory with MDX dictionaries for offline word lookup |

Legacy variables `DEEPSEEK_API_KEY` / `deepseek` / `DEEPSEEK` and `base_url_deepseek` are also recognized.

## Wordlists

CEFR/domain wordlists are built from third-party frequency lists (BNC/COCA, BSL, NAWL, FEL) that are **not bundled** for licensing reasons. The app runs fine without them — word highlighting simply stays off until you build the lists.

See [docs/WORDLISTS.md](docs/WORDLISTS.md) for download sources and the one-command build. You can also upload your own wordlists (.txt/.csv) from the web UI.

## Optional: MDX Dictionaries

Offline dictionaries (OALD, Longman, Vocabulary.com) in MDX format can be placed in a directory pointed to by `DICT_DIR`. These are copyrighted materials and are not distributed with this repo.

## Development

```bash
# Run the test suite
python -m pytest -q

# Smoke checks
python tests/smoke_generate.py --test-client
python tests/smoke_browser.py          # requires playwright browsers installed
```

Project layout:

```
backend/            # FastAPI server, generation pipeline, services
  fastapi_server.py #   web server entry (localhost:5173)
  main.py           #   CLI lesson-generation entry
  sources/          #   input adapters (youtube/bilibili/article/text/local)
  webapp/           #   routes, services, storage
frontend/           # Jinja templates + static JS
tests/              # pytest suite + smoke scripts
scripts/            # segmentation & MFA alignment QA tools
resources/wordlists/# wordlist sources (user-provided) & compiled lists
```

## License

[Apache-2.0](LICENSE)

## Notes

- Never commit `.env`, `cookies.txt`, vocab databases, or downloaded media — see `.gitignore`.
- Report security issues (e.g. leaked keys) by opening a private security advisory rather than a public issue.
