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
| mdict-utils | MIT | MDX/MDD dictionary lookup |
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
- **Hunyuan translation model** — downloaded by the user; subject to the
  Tencent Hunyuan community license

## Acknowledgments

- The video → transcript → AI-notes workflow was partly inspired by
  [BiliNote](https://github.com/JefferyHcool/BiliNote) (MIT License).
- Dictionary files (OALD, Longman, Vocabulary.com) are user-provided and not
  distributed; see `docs/DICTIONARIES.md`.
