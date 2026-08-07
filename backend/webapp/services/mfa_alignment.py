"""Optional Montreal Forced Aligner integration for media lessons.

MFA is deliberately an enhancement layer: failures never replace subtitle text
or block lesson creation.  The cached result only projects existing complete
sentences back onto word/phone timestamps from the original audio.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import db
from webapp.services.v2_translation import build_translation_units
from webapp.storage.lessons import OUTPUT_DIR


_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)*")
_TIER_RE = re.compile(
    r"item\s+\[\d+\]:\s*(.*?)(?=\n\s*item\s+\[\d+\]:|\Z)",
    re.DOTALL,
)
_INTERVAL_RE = re.compile(
    r"intervals\s+\[\d+\]:\s*"
    r"xmin\s*=\s*([0-9.eE+-]+)\s*"
    r"xmax\s*=\s*([0-9.eE+-]+)\s*"
    r'text\s*=\s*"(.*?)"',
    re.DOTALL,
)
_SILENCE_LABELS = {"", "<eps>", "sil", "sp", "spn", "<unk>"}
_RUNNING: set[int] = set()
_RUNNING_LOCK = threading.Lock()
_MFA_PROCESS_LOCK = threading.Lock()
_NUMBER_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
)
_NUMBER_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")

_ARPA_TO_IPA = {
    "AA": "ɑ", "AE": "æ", "AH": "ʌ", "AO": "ɔ", "AW": "aʊ", "AY": "aɪ",
    "B": "b", "CH": "tʃ", "D": "d", "DH": "ð", "EH": "ɛ", "ER": "ɝ",
    "EY": "eɪ", "F": "f", "G": "ɡ", "HH": "h", "IH": "ɪ", "IY": "i",
    "JH": "dʒ", "K": "k", "L": "l", "M": "m", "N": "n", "NG": "ŋ",
    "OW": "oʊ", "OY": "ɔɪ", "P": "p", "R": "ɹ", "S": "s", "SH": "ʃ",
    "T": "t", "TH": "θ", "UH": "ʊ", "UW": "u", "V": "v", "W": "w",
    "Y": "j", "Z": "z", "ZH": "ʒ",
}
_ARPA_VOWELS = {
    "AA", "AE", "AH", "AO", "AW", "AY", "EH", "ER", "EY",
    "IH", "IY", "OW", "OY", "UH", "UW",
}
_ONSET_CLUSTERS = {
    ("P", "R"), ("B", "R"), ("T", "R"), ("D", "R"), ("K", "R"), ("G", "R"),
    ("F", "R"), ("TH", "R"), ("SH", "R"), ("P", "L"), ("B", "L"), ("K", "L"),
    ("G", "L"), ("F", "L"), ("V", "L"), ("S", "L"), ("S", "M"), ("S", "N"),
    ("S", "P"), ("S", "T"), ("S", "K"), ("S", "W"), ("K", "W"), ("G", "W"),
    ("T", "W"), ("D", "W"), ("P", "Y"), ("B", "Y"), ("T", "Y"), ("D", "Y"),
    ("K", "Y"), ("G", "Y"), ("F", "Y"), ("V", "Y"), ("M", "Y"), ("N", "Y"),
    ("S", "P", "R"), ("S", "T", "R"), ("S", "K", "R"), ("S", "P", "L"),
    ("S", "K", "L"), ("S", "K", "W"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def alignment_directory(lesson_id: int) -> Path:
    return OUTPUT_DIR / "v2_alignments" / str(int(lesson_id))


def _result_path(lesson_id: int) -> Path:
    return alignment_directory(lesson_id) / "alignment.json"


def _status_path(lesson_id: int) -> Path:
    return alignment_directory(lesson_id) / "status.json"


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(
        path.suffix + f".{os.getpid()}-{threading.get_ident()}.tmp"
    )
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def load_lesson_alignment(lesson_id: int) -> dict | None:
    result = _read_json(_result_path(lesson_id))
    if not result or result.get("status") != "ready":
        return None
    return result


def get_alignment_status(lesson_id: int) -> dict:
    status = _read_json(_status_path(lesson_id))
    result = load_lesson_alignment(lesson_id)
    status_name = str((status or {}).get("status") or "")
    if status_name in {"queued", "running", "failed"}:
        return {
            "lesson_id": int(lesson_id),
            "status": status_name,
            "error": str((status or {}).get("error") or ""),
            "updated_at": str((status or {}).get("updated_at") or ""),
            "cached_alignment_available": bool(result),
            "mfa_available": mfa_available(),
        }
    if result:
        return {
            "lesson_id": int(lesson_id),
            "status": "ready",
            "sentence_count": len(result.get("sentences") or []),
            "word_count": int(result.get("word_count") or 0),
            "model": result.get("model") or "english_us_arpa",
            "updated_at": result.get("updated_at") or "",
            "mfa_available": mfa_available(),
        }
    return {
        "lesson_id": int(lesson_id),
        "status": str((status or {}).get("status") or "not_started"),
        "error": str((status or {}).get("error") or ""),
        "updated_at": str((status or {}).get("updated_at") or ""),
        "mfa_available": mfa_available(),
    }


def mfa_available() -> bool:
    return bool(_mfa_command())


def enqueue_lesson_alignment(lesson_id: int, *, force: bool = False) -> dict:
    lesson_id = int(lesson_id)
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise ValueError("Lesson not found")
    if not _is_media_lesson(lesson):
        raise ValueError("MFA alignment is only available for media lessons")
    if load_lesson_alignment(lesson_id) and not force:
        return get_alignment_status(lesson_id)
    with _RUNNING_LOCK:
        if lesson_id in _RUNNING:
            return get_alignment_status(lesson_id)
        _RUNNING.add(lesson_id)
    _write_json(
        _status_path(lesson_id),
        {"lesson_id": lesson_id, "status": "queued", "updated_at": _now(), "error": ""},
    )
    db.spawn_with_db_context(
        _alignment_worker, lesson_id, force,
        name=f"mfa-alignment-{lesson_id}",
    )
    return get_alignment_status(lesson_id)


def _alignment_worker(lesson_id: int, force: bool) -> None:
    try:
        run_lesson_alignment(lesson_id, force=force)
    except Exception as exc:
        try:
            _write_json(
                _status_path(lesson_id),
                {
                    "lesson_id": int(lesson_id),
                    "status": "failed",
                    "updated_at": _now(),
                    "error": str(exc),
                },
            )
        except Exception:
            pass
    finally:
        with _RUNNING_LOCK:
            _RUNNING.discard(int(lesson_id))


def run_lesson_alignment(lesson_id: int, *, force: bool = False) -> dict:
    lesson_id = int(lesson_id)
    if not force:
        cached = load_lesson_alignment(lesson_id)
        if cached:
            return cached
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise ValueError("Lesson not found")
    status_path = _status_path(lesson_id)
    try:
        _write_json(
            status_path,
            {
                "lesson_id": lesson_id,
                "status": "running",
                "updated_at": _now(),
                "error": "",
            },
        )
        command = _mfa_command()
        if not command:
            raise RuntimeError(
                "MFA is unavailable; install the english-mfa environment or set MFA_COMMAND"
            )
        audio_path = _resolve_lesson_audio(lesson)
        units = build_translation_units(db.get_v2_subtitle_segments(lesson_id))
        units = [unit for unit in units if str(unit.get("text") or "").strip()]
        if not units:
            raise RuntimeError("Lesson has no complete subtitle sentences to align")
        work_dir = alignment_directory(lesson_id)
        corpus_dir = work_dir / "corpus"
        aligned_dir = work_dir / "aligned"
        source_wav = work_dir / "source-16k.wav"
        for target in (corpus_dir, aligned_dir):
            if target.exists():
                shutil.rmtree(target)
            target.mkdir(parents=True, exist_ok=True)
        _convert_to_alignment_wav(audio_path, source_wav)
        chunks = _build_chunks(units)
        for chunk_index, chunk in enumerate(chunks, start=1):
            stem = f"chunk-{chunk_index:04d}"
            chunk["stem"] = stem
            _extract_wav_chunk(
                source_wav,
                corpus_dir / f"{stem}.wav",
                float(chunk["start"]),
                float(chunk["end"]) - float(chunk["start"]),
            )
            (corpus_dir / f"{stem}.lab").write_text(
                _alignment_text(chunk["units"]),
                encoding="utf-8",
            )
        _run_mfa(command, corpus_dir, aligned_dir, work_dir)
        aligned_words: list[dict] = []
        for chunk in chunks:
            textgrid = _find_textgrid(aligned_dir, str(chunk["stem"]))
            tiers = parse_textgrid(textgrid)
            words = _attach_phones(tiers["words"], tiers["phones"])
            offset = float(chunk["start"])
            for word in words:
                word["start"] = round(float(word["start"]) + offset, 4)
                word["end"] = round(float(word["end"]) + offset, 4)
                for phone in word["phones"]:
                    phone["start"] = round(float(phone["start"]) + offset, 4)
                    phone["end"] = round(float(phone["end"]) + offset, 4)
                word["ipa"] = phones_to_ipa([phone["label"] for phone in word["phones"]])
            aligned_words.extend(words)
        sentences = project_words_to_sentences(units, aligned_words)
        result = {
            "lesson_id": lesson_id,
            "status": "ready",
            "model": os.environ.get("MFA_ACOUSTIC_MODEL", "english_us_arpa"),
            "updated_at": _now(),
            "audio_path": str(audio_path),
            "sentence_count": len(sentences),
            "word_count": len(aligned_words),
            "sentences": sentences,
        }
        _write_json(_result_path(lesson_id), result)
        _write_json(
            status_path,
            {
                "lesson_id": lesson_id,
                "status": "ready",
                "updated_at": result["updated_at"],
                "error": "",
            },
        )
        return result
    except Exception as exc:
        _write_json(
            status_path,
            {
                "lesson_id": lesson_id,
                "status": "failed",
                "updated_at": _now(),
                "error": str(exc),
            },
        )
        raise


def _is_media_lesson(lesson: dict) -> bool:
    return str(lesson.get("source_type") or "") in {
        "youtube", "bilibili", "local", "local_audio", "local_video",
    }


def _resolve_lesson_audio(lesson: dict) -> Path:
    media_url = str(lesson.get("media_url") or "").strip()
    if media_url:
        if media_url.startswith("/output/"):
            candidate = (OUTPUT_DIR / media_url.removeprefix("/output/")).resolve()
        else:
            candidate = Path(media_url).expanduser().resolve()
        if candidate.exists() and candidate.is_file():
            return candidate
    if str(lesson.get("source_type") or "") == "youtube":
        from sources.youtube import download_youtube_audio

        return download_youtube_audio(str(lesson.get("source_url") or ""))
    raise RuntimeError("No local media is available for MFA alignment")


def _mfa_command() -> list[str]:
    configured = str(os.environ.get("MFA_COMMAND") or "").strip()
    if configured:
        return shlex.split(configured, posix=os.name != "nt")
    candidates = [
        Path.home() / "miniconda3" / "envs" / "english-mfa" / "Scripts" / "mfa.exe",
        Path.home() / "anaconda3" / "envs" / "english-mfa" / "Scripts" / "mfa.exe",
        Path.home() / ".conda" / "envs" / "english-mfa" / "bin" / "mfa",
        Path.home() / "miniconda3" / "envs" / "english-mfa" / "bin" / "mfa",
    ]
    conda = shutil.which("conda")
    if conda and any(candidate.exists() for candidate in candidates):
        return [conda, "run", "-n", "english-mfa", "mfa"]
    executable = shutil.which("mfa")
    if executable:
        return [executable]
    for candidate in candidates:
        if candidate.exists():
            return [str(candidate)]
    return []


def _ffmpeg_command() -> str:
    configured = str(os.environ.get("FFMPEG_PATH") or "").strip()
    if configured and Path(configured).exists():
        return configured
    conda_ffmpeg = Path(sys.executable).parent / "Library" / "bin" / "ffmpeg.exe"
    if conda_ffmpeg.exists():
        return str(conda_ffmpeg)
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    raise RuntimeError("FFmpeg is unavailable")


def _run_process(command: list[str], *, timeout: int) -> None:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "Command failed").strip()
        raise RuntimeError(detail[-3000:])


def _convert_to_alignment_wav(source: Path, target: Path) -> None:
    if target.exists() and target.stat().st_size > 0:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    _run_process(
        [
            _ffmpeg_command(), "-y", "-i", str(source),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(target),
        ],
        timeout=1800,
    )


def _extract_wav_chunk(source: Path, target: Path, start: float, duration: float) -> None:
    _run_process(
        [
            _ffmpeg_command(), "-y", "-ss", f"{max(0, start):.3f}", "-i", str(source),
            "-t", f"{max(0.1, duration):.3f}", "-c", "copy", str(target),
        ],
        timeout=300,
    )


def _build_chunks(units: list[dict], *, target_seconds: float = 45.0,
                  maximum_seconds: float = 60.0) -> list[dict]:
    chunks: list[dict] = []
    bucket: list[dict] = []
    for unit in units:
        start = float(unit.get("start_seconds", unit.get("start", 0)) or 0)
        end = float(unit.get("end_seconds", unit.get("end", start)) or start)
        if bucket:
            bucket_start = float(
                bucket[0].get("start_seconds", bucket[0].get("start", 0)) or 0
            )
            if end - bucket_start > maximum_seconds:
                chunks.append(_chunk_payload(bucket))
                bucket = []
        bucket.append(unit)
        bucket_start = float(
            bucket[0].get("start_seconds", bucket[0].get("start", 0)) or 0
        )
        if end - bucket_start >= target_seconds:
            chunks.append(_chunk_payload(bucket))
            bucket = []
    if bucket:
        chunks.append(_chunk_payload(bucket))
    return chunks


def _chunk_payload(units: list[dict]) -> dict:
    start = float(units[0].get("start_seconds", units[0].get("start", 0)) or 0)
    end = float(units[-1].get("end_seconds", units[-1].get("end", start)) or start)
    return {"start": max(0.0, start), "end": max(start + 0.1, end), "units": list(units)}


def _alignment_text(units: list[dict]) -> str:
    text = " ".join(str(unit.get("text") or "").strip() for unit in units)
    text = re.sub(r"\[[^\]]+\]", " ", text)
    return " ".join(text.split())


def _run_mfa(command: list[str], corpus_dir: Path, aligned_dir: Path, work_dir: Path) -> None:
    dictionary = os.environ.get("MFA_DICTIONARY_MODEL", "english_us_arpa")
    acoustic = os.environ.get("MFA_ACOUSTIC_MODEL", "english_us_arpa")
    g2p = os.environ.get("MFA_G2P_MODEL", "english_us_arpa")
    mfa_root = Path(
        os.environ.get("MFA_ROOT_DIR") or (OUTPUT_DIR.parent / ".cache" / "mfa-root")
    )
    mfa_root.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["MFA_ROOT_DIR"] = str(mfa_root)
    numba_cache = mfa_root / "numba-cache"
    numba_cache.mkdir(parents=True, exist_ok=True)
    environment["NUMBA_CACHE_DIR"] = str(numba_cache)
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    executable = Path(command[0])
    if executable.suffix.lower() == ".exe" and executable.parent.name.lower() == "scripts":
        environment_root = executable.parent.parent
        environment["PATH"] = os.pathsep.join(
            [
                str(environment_root),
                str(environment_root / "Scripts"),
                str(environment_root / "Library" / "bin"),
                environment.get("PATH", ""),
            ]
        )
    full_command = [
        *command, "align", str(corpus_dir), dictionary, acoustic, str(aligned_dir),
        "--output_format", "long_textgrid",
        "--g2p_model_path", g2p,
        "--single_speaker",
        "--num_jobs", str(max(1, min(4, (os.cpu_count() or 2) // 2))),
        "--clean", "--overwrite",
    ]
    with _MFA_PROCESS_LOCK:
        completed = subprocess.run(
            full_command,
            check=False,
            capture_output=True,
            text=True,
            timeout=int(os.environ.get("MFA_ALIGNMENT_TIMEOUT_SECONDS", "7200")),
            encoding="utf-8",
            errors="replace",
            env=environment,
            cwd=str(work_dir),
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "MFA alignment failed").strip()
        raise RuntimeError(detail[-5000:])


def _find_textgrid(output_dir: Path, stem: str) -> Path:
    candidates = sorted(output_dir.rglob(f"{stem}.TextGrid"))
    if not candidates:
        candidates = sorted(output_dir.rglob(f"{stem}.textgrid"))
    if not candidates:
        raise RuntimeError(f"MFA did not produce a TextGrid for {stem}")
    return candidates[0]


def parse_textgrid(path: Path) -> dict[str, list[dict]]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    tiers: dict[str, list[dict]] = {"words": [], "phones": []}
    for block in _TIER_RE.findall(raw):
        name_match = re.search(r'name\s*=\s*"([^"]+)"', block)
        if not name_match:
            continue
        name = name_match.group(1).strip().lower()
        kind = "words" if name.endswith("words") or name == "word" else ""
        if name.endswith("phones") or name == "phone":
            kind = "phones"
        if not kind:
            continue
        for start, end, label in _INTERVAL_RE.findall(block):
            clean_label = label.replace('""', '"').strip()
            if clean_label.casefold() in _SILENCE_LABELS:
                continue
            tiers[kind].append(
                {"start": float(start), "end": float(end), "label": clean_label}
            )
    if not tiers["words"]:
        raise RuntimeError(f"TextGrid has no word tier: {path.name}")
    return tiers


def _attach_phones(words: list[dict], phones: list[dict]) -> list[dict]:
    attached: list[dict] = []
    for word in words:
        word_start = float(word["start"])
        word_end = float(word["end"])
        word_phones = [
            dict(phone)
            for phone in phones
            if word_start - 0.001 <= (float(phone["start"]) + float(phone["end"])) / 2
            <= word_end + 0.001
        ]
        attached.append({**word, "phones": word_phones})
    return attached


def phones_to_ipa(phones: list[str]) -> str:
    parsed: list[tuple[str, str]] = []
    for phone in phones:
        match = re.fullmatch(r"([A-Za-z]+)([012]?)", str(phone).strip())
        if not match:
            continue
        base, stress = match.groups()
        base = base.upper()
        if base in _ARPA_TO_IPA:
            parsed.append((base, stress))
    stress_markers: dict[int, str] = {}
    vowel_indexes = [index for index, (base, _) in enumerate(parsed) if base in _ARPA_VOWELS]
    for index, (base, stress) in enumerate(parsed):
        if base not in _ARPA_VOWELS or stress not in {"1", "2"}:
            continue
        previous_vowels = [value for value in vowel_indexes if value < index]
        if not previous_vowels:
            boundary = 0
        else:
            previous_vowel = previous_vowels[-1]
            consonants = [value[0] for value in parsed[previous_vowel + 1:index]]
            onset_length = 0
            for size in range(min(3, len(consonants)), 0, -1):
                if size == 1 or tuple(consonants[-size:]) in _ONSET_CLUSTERS:
                    onset_length = size
                    break
            boundary = index - onset_length
        marker = "ˈ" if stress == "1" else "ˌ"
        if marker == "ˈ" or boundary not in stress_markers:
            stress_markers[boundary] = marker
    rendered: list[str] = []
    for index, (base, stress) in enumerate(parsed):
        if index in stress_markers:
            rendered.append(stress_markers[index])
        ipa = _ARPA_TO_IPA.get(base, "")
        if not ipa:
            continue
        if base == "AH" and stress == "0":
            ipa = "ə"
        elif base == "ER" and stress == "0":
            ipa = "ɚ"
        rendered.append(ipa)
    return "".join(rendered)


def _normal_word(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _integer_to_english(value: int) -> str:
    if value < 20:
        return _NUMBER_ONES[value]
    if value < 100:
        tens, remainder = divmod(value, 10)
        return " ".join(part for part in (_NUMBER_TENS[tens], _integer_to_english(remainder) if remainder else "") if part)
    if value < 1000:
        hundreds, remainder = divmod(value, 100)
        return " ".join(part for part in (f"{_NUMBER_ONES[hundreds]} hundred", _integer_to_english(remainder) if remainder else "") if part)
    if value < 1_000_000:
        thousands, remainder = divmod(value, 1000)
        return " ".join(part for part in (f"{_integer_to_english(thousands)} thousand", _integer_to_english(remainder) if remainder else "") if part)
    return ""


def numeric_token_ipa(token: str) -> str:
    if not token.isdigit():
        return ""
    spoken = _integer_to_english(int(token))
    if not spoken:
        return ""
    try:
        import eng_to_ipa as ipa_lib

        ipa = str(ipa_lib.convert(spoken) or "").replace("*", "").strip()
        if " " not in spoken and ipa.count("ˈ") > 1:
            final_stress = ipa.rfind("ˈ")
            ipa = ipa[:final_stress].replace("ˈ", "") + ipa[final_stress:]
        return ipa
    except Exception:
        return ""


def project_words_to_sentences(units: list[dict], aligned_words: list[dict]) -> list[dict]:
    def projected_word(expected_token: str, observed_index: int) -> dict:
        word = aligned_words[observed_index]
        pause_before = (
            max(0.0, float(word["start"]) - float(aligned_words[observed_index - 1]["end"]))
            if observed_index > 0
            else 0.0
        )
        pause_after = (
            max(0.0, float(aligned_words[observed_index + 1]["start"]) - float(word["end"]))
            if observed_index < len(aligned_words) - 1
            else 0.0
        )
        return {
            "text": expected_token,
            "word": str(word.get("label") or ""),
            "start": float(word["start"]),
            "end": float(word["end"]),
            "pause_before_ms": round(pause_before * 1000),
            "pause_after_ms": round(pause_after * 1000),
            "ipa": str(word.get("ipa") or numeric_token_ipa(expected_token)),
            "phones": word.get("phones") or [],
        }

    expected: list[tuple[int, str, str]] = []
    for unit_index, unit in enumerate(units):
        for token in _WORD_RE.findall(str(unit.get("text") or "")):
            normalized = _normal_word(token)
            if normalized:
                expected.append((unit_index, token, normalized))
    observed = [
        (index, word, _normal_word(word.get("label", "")))
        for index, word in enumerate(aligned_words)
        if _normal_word(word.get("label", ""))
    ]
    matcher = SequenceMatcher(
        None,
        [item[2] for item in expected],
        [item[2] for item in observed],
        autojunk=False,
    )
    expected_to_observed: dict[int, int] = {}
    for expected_start, observed_start, size in matcher.get_matching_blocks():
        for offset in range(size):
            expected_to_observed[expected_start + offset] = observed[observed_start + offset][0]
    expected_by_unit: dict[int, list[int]] = {}
    for expected_index, (unit_index, _, _) in enumerate(expected):
        expected_by_unit.setdefault(unit_index, []).append(expected_index)

    sentences: list[dict] = []
    for unit_index, unit in enumerate(units):
        expected_indexes = expected_by_unit.get(unit_index, [])
        observed_indexes = [
            expected_to_observed[index]
            for index in expected_indexes
            if index in expected_to_observed
        ]
        mapped_pairs = [
            (expected[index][1], aligned_words[expected_to_observed[index]])
            for index in expected_indexes
            if index in expected_to_observed
        ]
        mapped = [word for _, word in mapped_pairs]
        coverage = len(mapped) / max(1, len(expected_indexes))
        subtitle_start = float(unit.get("start_seconds", unit.get("start", 0)) or 0)
        subtitle_end = float(
            unit.get("end_seconds", unit.get("end", subtitle_start)) or subtitle_start
        )
        use_alignment = bool(mapped) and coverage >= 0.55
        start = max(0.0, float(mapped[0]["start"]) - 0.12) if use_alignment else subtitle_start
        end = float(mapped[-1]["end"]) + 0.14 if use_alignment else subtitle_end
        first_observed = observed_indexes[0] if observed_indexes else -1
        last_observed = observed_indexes[-1] if observed_indexes else -1
        pause_before = 0.0
        pause_after = 0.0
        if first_observed > 0:
            pause_before = max(
                0.0,
                float(aligned_words[first_observed]["start"])
                - float(aligned_words[first_observed - 1]["end"]),
            )
        if 0 <= last_observed < len(aligned_words) - 1:
            pause_after = max(
                0.0,
                float(aligned_words[last_observed + 1]["start"])
                - float(aligned_words[last_observed]["end"]),
            )
        confidence = "high" if coverage >= 0.85 else "medium" if use_alignment else "fallback"
        key = int(unit.get("index", unit_index))
        sentences.append(
            {
                "key": key,
                "text": " ".join(str(unit.get("text") or "").split()),
                "start_seconds": round(start, 4),
                "end_seconds": round(max(start + 0.05, end), 4),
                "subtitle_start_seconds": round(subtitle_start, 4),
                "subtitle_end_seconds": round(subtitle_end, 4),
                "coverage": round(coverage, 4),
                "boundary_confidence": confidence,
                "pause_before_ms": round(pause_before * 1000),
                "pause_after_ms": round(pause_after * 1000),
                "words": [
                    projected_word(expected_token, observed_index)
                    for expected_token, observed_index in (
                        (expected[index][1], expected_to_observed[index])
                        for index in expected_indexes
                        if index in expected_to_observed
                    )
                ],
            }
        )
    for previous, current in zip(sentences, sentences[1:]):
        if previous["end_seconds"] > current["start_seconds"]:
            boundary = round(
                (float(previous["end_seconds"]) + float(current["start_seconds"])) / 2,
                4,
            )
            previous["end_seconds"] = boundary
            current["start_seconds"] = boundary
    return sentences
