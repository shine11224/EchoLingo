# EchoLingo

**中文文档 → [README.zh-CN.md](README.zh-CN.md)**

**Local-first, self-hosted English learning workspace**: turn your own videos, podcasts and articles into interactive lessons — listen with hidden-original drills, retell with AI feedback, drill reusable sentence patterns, and review vocabulary. Local Whisper transcription, local translation, MDX dictionaries, and any OpenAI-compatible LLM.

本地优先、可自托管的英语学习工作台：把自己的视频 / 播客 / 文章变成交互式课程——隐藏原文精听、整句复述 + AI 对比、句式提取复用 + AI 批改、词汇复习一站完成。本地 Whisper 转写、本地翻译、MDX 词典，AI 兼容任意 OpenAI 接口。

## Why EchoLingo

- **Your data stays home** — lessons, vocab DB and caches live on your machine; no account, no subscription
- **Real materials, not canned courses** — learn from the content you actually care about
- **Input → output loop** — hidden-original listening drills → whole-sentence retelling with AI comparison → sentence-pattern reuse with AI correction → vocabulary memory stories
- **Pluggable AI** — DeepSeek, OpenAI, Groq, Ollama or any OpenAI-compatible endpoint; transcription, translation and dictionaries run fully local

## Screenshots

| | |
|---|---|
| ![Home — create lessons from real materials](docs/screenshots/home.png) | ![Lesson — bilingual subtitles, outline, AI companion](docs/screenshots/lesson.png) |
| ![Sentence library — retell with AI comparison, pattern drills](docs/screenshots/sentence-library.png) | ![Vocabulary workshop — context-first review cards](docs/screenshots/vocab.png) |

## How it works

1. **Create a lesson** from a YouTube/Bilibili link, an article URL, a local video/audio file, or pasted text. Subtitles are fetched or transcribed locally with faster-whisper, segmented into sentences, translated, and annotated with IPA and connected-speech notes.
2. **Study the lesson** sentence by sentence: loop playback, click any word for MDX dictionary entries (OALD / Longman), get AI hints, and save useful sentences and words with one click.
3. **Review in the sentence library**: hide the original and listen first, mark 听懂/听不懂, then speak a full-sentence retelling — AI compares your version with the original. Extract the reusable sentence pattern and drill it: write your own sentence, get AI correction with a revision and a more idiomatic alternative.
4. **Review vocabulary** with a familiarity lifecycle (unknown → fuzzy → known → mastered), AI-generated memory stories with chat, and exports to Markdown / HTML / Anki.

## Features

- **Multiple sources** — YouTube, Bilibili, article URLs, local video/audio, plain text/Markdown
- **Lesson generation** — subtitle fetching or local faster-whisper transcription, sentence segmentation, translation, IPA annotation, connected-speech and oral analysis
- **Interactive lesson pages** — per-sentence loop playback, MDX dictionary lookup (OALD / Longman), AI hints and practice
- **Sentence library** — collect sentences from lessons or AI output, listening practice with hidden original, spoken retelling with AI comparison, and pattern drills graded by AI with revision + idiomatic suggestions
- **Vocabulary system** — CEFR-graded word highlighting, personal vocab book with review lifecycle, AI memory stories with chat, and exports (Markdown / HTML / Anki)
- **Local-first** — lessons, vocab DB, and caches stay on your machine; AI features work with any OpenAI-compatible API (DeepSeek, OpenAI, Groq, Ollama, …)

## Quick Start

Requires Python 3.11+ and [ffmpeg](https://ffmpeg.org/) on PATH. See **System Requirements** below for hardware notes.

```bash
git clone https://github.com/shine11224/EchoLingo.git
cd EchoLingo
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux
pip install -r requirements.txt
```

Configure API keys:

```bash
cp .env.example .env          # then edit .env — AI_API_KEY is enough to start
```

Start the server:

```bash
python backend/fastapi_server.py
# open http://localhost:5173
```

## Usage guide

**First lesson**: paste a YouTube or Bilibili link on the home page and create a lesson. Short clips (2–10 min) work best for intensive study. Local files and article URLs work the same way.

**Lesson page**: click a sentence to loop it; click a word to look it up and add it to your vocab book; use the AI panel for translation, grammar hints and Q&A about the current sentence.

**Sentence library** (home page tab): every sentence you save lands here. Recommended daily flow:

1. **Listen** — original hidden by default; play the audio, then mark 听懂 / 听不懂
2. **Retell** — open 🎙 复述, speak the sentence from memory; AI compares your retelling with the original across accuracy, missing content, grammar and expression
3. **Pattern drill** — extract the sentence's reusable pattern, write your own sentence with it (or get an AI Chinese scenario prompt), then let AI grade it with a revision and a more idiomatic alternative — you can favorite those AI sentences too

**Vocabulary** (home page tab): review cards stay context-first — rate yourself before revealing the meaning. Generate an AI memory story from today's words when you want narrative reinforcement, and export any time (Markdown / HTML / Anki).

**Settings** (in-app): the settings page writes the same `.env` file — AI keys, Whisper model size, dictionary folder and wordlists can all be changed without editing files by hand.

## System Requirements

- **OS** — Windows 10/11, macOS 12+, or Linux
- **Runtime** — Python 3.11+, ffmpeg on PATH
- **RAM** — 8 GB minimum; 16 GB recommended when running local Whisper
- **GPU (optional)** — an NVIDIA GPU with ≥6 GB VRAM makes large-v3 transcription several times faster; CPU-only works fine with the base/medium models, or set `GROQ_API_KEY` for fast cloud transcription
- **Disk** — ~2 GB for the app, plus 1–3 GB per Whisper model and lesson caches
- **Microphone** — needed for the spoken-retell feature; a modern browser (Chrome/Edge recommended)

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

[PolyForm Noncommercial 1.0.0](LICENSE) — free for personal, educational and non-profit use; commercial use requires a separate license from the author. Third-party components remain under their own licenses, see [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
