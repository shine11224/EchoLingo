"""Export v2 saved review items to a self-contained HTML folder."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

import db
from webapp.services.natural_tts import is_current_tts_audio, synthesize_natural_speech
from webapp.storage import user_assets
from webapp.storage.lessons import OUTPUT_DIR


def export_review_html(lesson_id: int) -> dict:
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise ValueError(f"Lesson {lesson_id} not found")

    export_dir = user_assets.user_output_subdir(
        "v2_exports", str(lesson_id), fallback=OUTPUT_DIR / "v2_exports" / str(lesson_id)
    )
    audio_dir = export_dir / "audio"
    export_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)

    words = _lesson_words(lesson)
    sentences = db.get_v2_phase_b_sentences(lesson_id)
    sentence_rows = []
    for idx, item in enumerate(sentences, start=1):
        text = str(item.get("text") or "").strip()
        audio_name = f"sentence-{idx}.wav"
        audio_path = audio_dir / audio_name
        if text and not is_current_tts_audio(audio_path, text):
            synthesize_sentence_audio(text, audio_path)
        sentence_rows.append({**item, "audio": f"audio/{audio_name}"})

    html_text = _render_review_html(lesson, words, sentence_rows)
    html_path = export_dir / "review.html"
    html_path.write_text(html_text, encoding="utf-8")

    return {
        "ok": True,
        "lesson_id": lesson_id,
        "export_url": f"/output/v2_exports/{lesson_id}/review.html",
        "word_count": len(words),
        "sentence_count": len(sentence_rows),
    }


def synthesize_sentence_audio(text: str, output_path: Path) -> None:
    synthesize_natural_speech(text, output_path)


def _lesson_words(lesson: dict) -> list[dict]:
    lesson_id = int(lesson["id"])
    lesson_words = db.get_v2_lesson_words(lesson_id)
    rows = []
    for entry in lesson_words:
        word = str(entry.get("word") or "")
        if word.startswith("__"):
            continue
        rows.append({
            "word": word,
            "meaning": _meaning_from_entry(entry),
            "context": str(entry.get("sentence") or ""),
        })
    return rows


def _meaning_from_entry(entry: dict) -> str:
    raw = entry.get("cached_analysis")
    if not raw:
        return ""
    try:
        analysis = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return ""
    if not isinstance(analysis, dict):
        return ""
    meaning = str(analysis.get("basic_meaning") or analysis.get("meaning") or "").strip()
    if meaning:
        return meaning
    vocab = analysis.get("vocabulary")
    if isinstance(vocab, list):
        for item in vocab:
            if isinstance(item, dict) and item.get("meaning"):
                return str(item["meaning"]).strip()
    return ""


def _render_review_html(lesson: dict, words: list[dict], sentences: list[dict]) -> str:
    title = html.escape(str(lesson.get("title") or "Review Export"))
    word_items = "\n".join(
        "<tr>"
        f"<td>{html.escape(item['word'])}</td>"
        f"<td>{html.escape(item.get('meaning') or '')}</td>"
        f"<td>{html.escape(item.get('context') or '')}</td>"
        "</tr>"
        for item in words
    ) or '<tr><td colspan="3" class="empty">No saved words for this lesson.</td></tr>'
    sentence_items = "\n".join(
        "<section class=\"sentence-card\">"
        f"<p>{html.escape(str(item.get('text') or ''))}</p>"
        f"{_render_tag_list(item.get('tags') or [])}"
        f"<audio controls preload=\"metadata\" src=\"{html.escape(item['audio'])}\"></audio>"
        "</section>"
        for item in sentences
    ) or '<div class="empty">No saved sentences yet.</div>'
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} · Review</title>
  <style>
    body {{ margin: 0; font-family: Arial, sans-serif; background: #f6f7f4; color: #202421; }}
    main {{ max-width: 920px; margin: 0 auto; padding: 32px 20px 48px; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; }}
    h2 {{ margin-top: 28px; font-size: 18px; }}
    .meta {{ color: #687069; font-size: 13px; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #d9ded8; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #e6ebe5; text-align: left; vertical-align: top; }}
    th {{ background: #eef2ed; font-size: 13px; }}
    .sentence-card {{ background: #fff; border: 1px solid #d9ded8; border-radius: 8px; padding: 14px; margin-bottom: 12px; }}
    .sentence-card p {{ margin: 0 0 10px; line-height: 1.65; }}
    .tags {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 10px; }}
    .tag {{ border: 1px solid #d9ded8; border-radius: 999px; padding: 3px 8px; background: #f6f7f4; font-size: 12px; color: #3f4842; }}
    audio {{ width: 100%; }}
    .empty {{ color: #687069; padding: 14px; }}
  </style>
</head>
<body>
  <main>
    <h1>{title}</h1>
    <div class="meta">Saved review export · words: {len(words)} · sentences: {len(sentences)}</div>
    <h2>Saved Words</h2>
    <table>
      <thead><tr><th>Word</th><th>Meaning</th><th>Context</th></tr></thead>
      <tbody>{word_items}</tbody>
    </table>
    <h2>Saved Sentences</h2>
    {sentence_items}
  </main>
</body>
</html>
"""


def _render_tag_list(tags: list[dict]) -> str:
    if not tags:
        return ""
    return '<div class="tags">' + "".join(
        f"<span class=\"tag\">{html.escape(str(item.get('name') or ''))}</span>"
        for item in tags
    ) + "</div>"
