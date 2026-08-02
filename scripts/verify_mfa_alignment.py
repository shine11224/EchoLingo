"""Run a bounded real-audio MFA check without changing a lesson cache."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import db  # noqa: E402
from webapp.services import mfa_alignment as mfa  # noqa: E402
from webapp.services.v2_translation import build_translation_units  # noqa: E402


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("lesson_id", type=int)
    parser.add_argument("--chunks", type=int, default=1)
    args = parser.parse_args()

    lesson = db.get_v2_lesson(args.lesson_id)
    if not lesson:
        raise SystemExit(f"Lesson {args.lesson_id} not found")
    audio = mfa._resolve_lesson_audio(lesson)
    units = build_translation_units(db.get_v2_subtitle_segments(args.lesson_id))
    chunks = mfa._build_chunks(units)[:max(1, args.chunks)]
    if not chunks:
        raise SystemExit("No subtitle chunks")

    work = ROOT / ".tmp" / f"mfa-verification-{args.lesson_id}"
    corpus = work / "corpus"
    aligned = work / "aligned"
    corpus.mkdir(parents=True, exist_ok=True)
    aligned.mkdir(parents=True, exist_ok=True)
    source_wav = work / "source-16k.wav"
    mfa._convert_to_alignment_wav(audio, source_wav)

    for index, chunk in enumerate(chunks, start=1):
        stem = f"chunk-{index:04d}"
        chunk["stem"] = stem
        mfa._extract_wav_chunk(
            source_wav,
            corpus / f"{stem}.wav",
            float(chunk["start"]),
            float(chunk["end"]) - float(chunk["start"]),
        )
        (corpus / f"{stem}.lab").write_text(
            mfa._alignment_text(chunk["units"]),
            encoding="utf-8",
        )

    command = mfa._mfa_command()
    if not command:
        raise SystemExit("MFA command is unavailable")
    mfa._run_mfa(command, corpus, aligned, work)

    summary = []
    for chunk in chunks:
        textgrid = mfa._find_textgrid(aligned, str(chunk["stem"]))
        tiers = mfa.parse_textgrid(textgrid)
        words = mfa._attach_phones(tiers["words"], tiers["phones"])
        for word in words:
            word["ipa"] = mfa.phones_to_ipa([phone["label"] for phone in word["phones"]])
        sentences = mfa.project_words_to_sentences(chunk["units"], words)
        summary.append(
            {
                "chunk": chunk["stem"],
                "word_count": len(words),
                "phone_count": len(tiers["phones"]),
                "sentence_count": len(sentences),
                "first_sentence": sentences[0] if sentences else None,
            }
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
