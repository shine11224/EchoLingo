# Third-Party Notices

EchoLingo is built on open-source software. Our own code is licensed under
[PolyForm Noncommercial 1.0.0](LICENSE); the components below remain under
their own licenses, which permit this usage. Runtime Python dependencies are
installed via pip and can be replaced or upgraded independently.

## Python dependencies

| Package | License | Used for |
|---|---|---|
| fastapi | MIT | HTTP API |
| uvicorn | BSD-3-Clause | ASGI server |
| jinja2 | BSD-3-Clause | HTML templates |
| requests | Apache-2.0 | HTTP client |
| beautifulsoup4 | MIT | article parsing |
| yt-dlp | Unlicense (public domain) | YouTube/Bilibili download & subtitles |
| youtube-transcript-api | MIT | YouTube subtitles |
| python-multipart | Apache-2.0 | file uploads |
| openai | Apache-2.0 | OpenAI-compatible LLM client |
| groq | Apache-2.0 | Groq transcription client |
| faster-whisper | MIT | local speech-to-text |
| pdfplumber | MIT | PDF text extraction |
| pypdfium2 | Apache-2.0 / BSD-3-Clause | PDF rendering |
| pytesseract | Apache-2.0 | OCR |
| openpyxl | MIT | spreadsheet import |
| playwright | Apache-2.0 | browser QA |
| edge-tts | **LGPLv3** | neural text-to-speech |

## LGPL note (edge-tts)

edge-tts is licensed under LGPLv3. It is used as an unmodified, separately
installed Python library (dynamic import at runtime). You may replace or
upgrade it independently of EchoLingo (`pip install -U edge-tts`), which
satisfies the LGPL replacement requirement. Its license text is available at
https://github.com/rany2/edge-tts

## External programs (user-installed, not distributed)

- **ffmpeg** — LGPL/GPL depending on build. EchoLingo invokes the ffmpeg/yt-dlp
  executables you install yourself as separate processes; no ffmpeg code or
  binaries are included in this repository.

## Models (downloaded at runtime, not distributed)

- **Whisper** (via faster-whisper / CTranslate2) — model weights MIT © OpenAI

## Built-in dictionary data (open license)

- **ECDICT** — the default dictionary database, © skywind3000,
  [MIT License](https://github.com/skywind3000/ECDICT). EchoLingo bundles a
  compiled SQLite copy at `resources/ecdict/ecdict.db` for offline use.

## Online services (user-configured, subject to their own terms)

EchoLingo calls these third-party services over the network; your use of them is
governed by each provider's terms of service:

- **DeepSeek API** — default LLM for word/sentence analysis (configurable to any
  OpenAI-compatible endpoint)
- **Groq API** — optional cloud Whisper transcription
- **MyMemory Translation API** (translated.net) — fallback machine translation
  for words/sentences when no other translator is configured; anonymous free
  usage with daily limits, subject to Translated's terms of service
- **Microsoft Edge neural voices** — the `edge-tts` library (LGPLv3, see above)
  uses an unofficial interface to Microsoft's proprietary online speech service;
  the generated audio is subject to Microsoft's terms, and use beyond personal,
  non-commercial purposes may violate them
- **YouTube** — playback via the YouTube IFrame Player API and downloading via
  yt-dlp are subject to YouTube's Terms of Service; only process content you
  have the rights to
- **Bilibili** — subtitle/audio access is subject to Bilibili's terms; only
  process content you have the rights to

## Third-party wordlists (not bundled)

The graded wordlists the app can compile locally are **not distributed** with
this repository (see `docs/WORDLISTS.md`). Their licenses:

- **Business Service List (BSL)** — CC BY-SA 4.0, © Browne & Culligan
- **New Academic Word List (NAWL)** — CC BY-SA 4.0, © Browne, Culligan & Phillips
- **Fitness English List (FEL)** — NGSL project (Browne & Culligan, 2020), free
  to use with attribution
- **BNC/COCA word-family frequency list** — free for research/teaching use;
  redistribution terms are unclear, which is why it is not bundled

Locally compiled JSON files derived from the CC BY-SA lists remain under
CC BY-SA 4.0 if you share them. `compiled/sentence_patterns.json`, the only
wordlist data shipped in this repo, is original and self-authored.

### Tencent HY-MT1.5 (local translation)

EchoLingo can drive a user-downloaded Tencent HY-MT1.5 GGUF model (e.g.
`models/HY-MT1.5-1.8B-Q4_K_M.gguf`) through a local llama.cpp server
(llama.cpp itself is MIT). **No model weights are distributed by this
repository** — users download them from Tencent's official releases.

Tencent HY is licensed under the Tencent HY Community License Agreement,
Copyright © 2025 Tencent. All Rights Reserved. The trademark rights of
"Tencent HY" are owned by Tencent or its affiliate.

Key terms of the
[Tencent HY Community License Agreement](https://github.com/Tencent-Hunyuan/HY-MT/blob/main/License.txt)
that apply to anyone using the model with EchoLingo:

- **Territory** — the license does NOT apply in the European Union, United
  Kingdom and South Korea; using the model there is unlicensed.
- **No model improvement** — you must not use the model or its outputs to
  improve any other AI model (other than Tencent HY derivatives).
- **Scale threshold** — offerings with more than 100 million monthly active
  users require a separate license from Tencent.
- **Acceptable Use Policy** — Exhibit A of the agreement applies to all use.

EchoLingo is an independent project by shine11224. Tencent is not affiliated
with, associated with, sponsoring, or endorsing EchoLingo; the translation
functionality is provided by each user running their own locally downloaded
model copy.

## Acknowledgments

- The video → transcript → AI-notes workflow was partly inspired by
  [BiliNote](https://github.com/JefferyHcool/BiliNote) (MIT License).
- Dictionary data comes from the MIT-licensed ECDICT and is distributed as a
  compiled SQLite database; see `docs/DICTIONARIES.md`.
