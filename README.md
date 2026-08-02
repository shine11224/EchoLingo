# EchoLingo

**Local-first, self-hosted English learning workspace**: turn your own videos, podcasts and articles into interactive lessons — listen with hidden-original drills, retell with AI feedback, drill reusable sentence patterns, and review vocabulary. Local Whisper transcription, local translation, MDX dictionaries, and any OpenAI-compatible LLM.

本地优先、可自托管的英语学习工作台：把自己的视频 / 播客 / 文章变成交互式课程——隐藏原文精听、整句复述 + AI 对比、句式提取复用 + AI 批改、词汇复习一站完成。本地 Whisper 转写、本地翻译、MDX 词典，AI 兼容任意 OpenAI 接口。

## Why EchoLingo

- **Your data stays home** — lessons, vocab DB and caches live on your machine; no account, no subscription
- **Real materials, not canned courses** — learn from the content you actually care about
- **Input → output loop** — hidden-original listening drills → whole-sentence retelling with AI comparison → sentence-pattern reuse with AI correction → vocabulary memory stories
- **Pluggable AI** — DeepSeek, OpenAI, Groq, Ollama or any OpenAI-compatible endpoint; transcription, translation and dictionaries run fully local

## Features

- **Multiple sources** — YouTube, Bilibili, article URLs, local video/audio, plain text/Markdown
- **Lesson generation** — subtitle fetching or local faster-whisper transcription, sentence segmentation, translation, IPA annotation, connected-speech and oral analysis
- **Interactive lesson pages** — per-sentence loop playback, MDX dictionary lookup (OALD / Longman), AI hints and practice
- **Sentence library** — collect sentences from lessons or AI output, listening practice with hidden original, spoken retelling with AI comparison, and pattern drills graded by AI with revision + idiomatic suggestions
- **Vocabulary system** — CEFR-graded word highlighting, personal vocab book with review lifecycle, AI memory stories with chat, and exports (Markdown / HTML / Anki)
- **Local-first** — lessons, vocab DB, and caches stay on your machine; AI features work with any OpenAI-compatible API (DeepSeek, OpenAI, Groq, Ollama, …)

## Quick Start

Requires Python 3.11+ and [ffmpeg](https://ffmpeg.org/) on PATH.

```bash
git clone <this-repo>
cd echolingo
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

## Configuration

All settings live in `.env` (or the in-app Settings page, which writes the same file). Only `AI_API_KEY` is needed for AI features; everything else degrades gracefully:

- `AI_API_KEY` / `AI_BASE_URL` / `AI_MODEL` — any OpenAI-compatible chat API (default: DeepSeek)
- `GROQ_API_KEY` — optional, fast cloud transcription fallback (local Whisper works without it)
- `DICT_DIR` — optional, folder containing MDX dictionaries (auto-detects the Eudic dictionary folder); see `docs/DICTIONARIES.md`
- Third-party wordlists (BNC/COCA, BSL, NAWL…) are not bundled; see `docs/WORDLISTS.md` for download and build instructions

## Documentation

- `docs/WORDLISTS.md` — where to get wordlists and how to compile them
- `docs/DICTIONARIES.md` — MDX dictionary sources and configuration

## License

[Apache-2.0](LICENSE)
