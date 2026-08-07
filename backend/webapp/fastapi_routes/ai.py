"""Phase 6A–6E — AI text endpoints migrated from Flask."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading

import db
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from prompts import (
    CONNECTED_SPEECH_PROMPT,
    HINT_PROMPT,
    LISTENING_RETELL_PROMPT,
    ORAL_ANALYSIS_PROMPT,
    CORRECTION_PROMPT,
    PRACTICE_EXAMPLE_PROMPT,
    WORD_ANALYSIS_PROMPT,
)
from starlette.concurrency import run_in_threadpool
from webapp.runtime import ai_config
from webapp.services import dicts as dict_service

router = APIRouter()

_IPA_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)*")
_IPA_PROOFREAD_CACHE: dict[str, dict] = {}
_IPA_PROOFREAD_CACHE_LOCK = threading.Lock()
_IPA_PROOFREAD_CACHE_LIMIT = 256


async def _parse_body(request: Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@router.post("/analyze-word")
async def analyze_word(request: Request):
    data = await _parse_body(request)
    word = data.get("word", "").strip()
    sentence = data.get("sentence", "").strip()
    if not word:
        return JSONResponse({"error": "word required"}, status_code=400)

    ecdict = await run_in_threadpool(dict_service.lookup_ecdict, word)
    dict_meta = dict_service.format_ecdict_meta(dict_service.lookup_ecdict_meta(word))
    prompt = WORD_ANALYSIS_PROMPT.format(
        word=word,
        sentence=sentence,
        ecdict=ecdict or "（未找到）",
    )
    try:
        resp = await run_in_threadpool(
            ai_config.client.chat.completions.create,
            model=ai_config.AI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        analysis = json.loads(resp.choices[0].message.content)
        cached = db.cache_word_analysis(
            word,
            analysis,
            target_type=data.get("target_type", "word"),
            lemma=data.get("lemma", ""),
        )
        result = cached or analysis
        if isinstance(result, dict):
            result = {**result, "dict_meta": dict_meta}
        return result
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)



@router.post("/api/hint")
async def gen_hint(request: Request):
    data = await _parse_body(request)
    english = data.get("english", "")
    pattern_template = data.get("pattern_template", "") or data.get("pattern", "")
    vocab = ", ".join(data.get("vocab", []))
    try:
        resp = await run_in_threadpool(
            ai_config.client.chat.completions.create,
            model=ai_config.AI_MODEL,
            messages=[{"role": "user", "content": HINT_PROMPT.format(
                english=english,
                pattern_template=pattern_template or "无",
                vocab=vocab or "无",
            )}],
            temperature=0.7,
            max_tokens=200,
            extra_body={"thinking": {"type": "disabled"}},
        )
        hint = (resp.choices[0].message.content or "").strip()
        if not hint:
            return JSONResponse({"error": "AI未返回内容，请重试"}, status_code=500)
        return {"hint": hint}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/api/practice")
async def practice(request: Request):
    data = await _parse_body(request)
    action = str(data.get("action") or "correct").strip().lower()
    if action not in {"correct", "example"}:
        return JSONResponse({"error": "invalid action"}, status_code=400)
    hint_cn = str(data.get("scenario_cn") or data.get("hint_cn") or "").strip()
    user_answer = str(data.get("user_answer") or "").strip()
    if action == "correct" and not user_answer:
        return JSONResponse({"error": "user_answer required"}, status_code=400)
    english = str(data.get("original_sentence") or data.get("english") or "").strip()
    pattern_template = data.get("pattern_template", "") or data.get("pattern", "")
    vocab_items = list(dict.fromkeys(
        str(word).strip().lower() for word in data.get("vocab", []) if str(word).strip()
    ))
    vocab = ", ".join(vocab_items)
    practice_type = str(data.get("practice_type") or "").strip().lower()
    practice_type = {
        "vocabulary": "word",
        "sentence_pattern": "pattern",
    }.get(practice_type, practice_type)
    if not practice_type:
        practice_type = "pattern" if pattern_template else (
            "phrase" if any(" " in item for item in vocab_items) else "word"
        )
    if practice_type not in {"word", "phrase", "pattern"}:
        return JSONResponse({"error": "invalid practice_type"}, status_code=400)
    target = str(data.get("target") or pattern_template or vocab or "").strip()
    input_method = str(data.get("input_method") or "keyboard").strip().lower()
    if input_method not in {"keyboard", "voice"}:
        return JSONResponse({"error": "invalid input_method"}, status_code=400)
    hint_used = bool(
        data.get("hint_used")
        or data.get("showed_target")
        or data.get("showed_pattern")
    )
    try:
        if action == "example":
            prompt = PRACTICE_EXAMPLE_PROMPT.format(
                practice_type=practice_type,
                target=target or "无",
                meaning=str(data.get("meaning") or "无"),
                english=english or "无",
                source_context=str(data.get("source_context") or english or "无"),
                scenario_cn=hint_cn or "无",
            )
        else:
            prompt = CORRECTION_PROMPT.format(
                practice_type=practice_type,
                target=target or "无",
                meaning=str(data.get("meaning") or "无"),
                source_context=str(data.get("source_context") or english or "无"),
                input_method=input_method,
                hint_used="是" if hint_used else "否",
                hint_cn=hint_cn, english=english,
                pattern_template=pattern_template or "无",
                vocab=vocab or "无",
                user_answer=user_answer,
            )
        resp = await run_in_threadpool(
            ai_config.client.chat.completions.create,
            model=ai_config.AI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.5,
        )
        result = json.loads(resp.choices[0].message.content)
        if not isinstance(result, dict):
            raise ValueError("AI correction must return an object")
        if action == "example":
            example = str(result.get("example_sentence") or "").strip()
            if not example:
                raise ValueError("AI example returned no sentence")
            return {
                "mode": "example",
                "example_sentence": example,
                "scenario_cn": hint_cn,
                "recorded_as_attempt": False,
            }
        verdict = str(result.get("verdict") or "").strip().lower()
        if verdict not in {"accepted", "needs_revision"}:
            raise ValueError("AI correction returned invalid verdict")
        raw_improvement_points = result.get("improvement_points")
        if isinstance(raw_improvement_points, list):
            improvement_points = [
                str(item).strip() for item in raw_improvement_points if str(item).strip()
            ][:3]
        elif str(raw_improvement_points or "").strip():
            improvement_points = [str(raw_improvement_points).strip()]
        else:
            improvement_points = []
        result = {
            "verdict": verdict,
            "status_label": "表达成立" if verdict == "accepted" else "建议修改",
            "target_used_correctly": bool(result.get("target_used_correctly")),
            "key_issue": str(result.get("key_issue") or "").strip(),
            "explanation": str(result.get("explanation") or "").strip(),
            "naturalness_analysis": str(result.get("naturalness_analysis") or "").strip(),
            "improvement_points": improvement_points,
            "revised_sentence": str(result.get("revised_sentence") or "").strip(),
            "idiomatic_suggestion": str(result.get("idiomatic_suggestion") or "").strip(),
            "mode": "correction",
        }
        # 保留精学页现有消费者使用的字段，避免统一接口升级造成前端断裂。
        result["corrected"] = result["revised_sentence"]
        result["standard"] = ""
        result["variant"] = ""
        result["feedback"] = [
            item for item in (
                result["explanation"],
                result["naturalness_analysis"],
                *result["improvement_points"],
                result["idiomatic_suggestion"],
            ) if item
        ]
        result["key_points"] = [result["key_issue"]] if result["key_issue"] else []
        lesson_id = data.get("lesson_id")
        lesson_id = lesson_id if isinstance(lesson_id, int) else None
        sentence_id = data.get("sentence_id")
        sentence_id = sentence_id if isinstance(sentence_id, int) else None
        attempt = db.save_v2_practice_attempt(
            practice_type=practice_type,
            target=target,
            target_type=str(data.get("target_type") or practice_type),
            sentence_id=sentence_id,
            lesson_id=lesson_id,
            user_input=user_answer,
            input_method=input_method,
            hint_used=hint_used,
            scenario_cn=hint_cn,
            hint_text=str(data.get("hint_text") or data.get("hint") or ""),
            source_context=str(data.get("source_context") or english or ""),
            verdict=verdict,
            result=result,
        )
        result["attempt_id"] = attempt["id"]
        return result
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/practice/history")
def practice_history(
    target: str = "",
    sentence_id: int | None = None,
    practice_type: str = "",
    lesson_id: int | None = None,
    source_context: str = "",
    page: int = 1,
    page_size: int = 20,
):
    clean_type = str(practice_type or "").strip().lower()
    if clean_type and clean_type not in {"word", "phrase", "pattern"}:
        return JSONResponse({"error": "invalid practice_type"}, status_code=400)
    if page < 1:
        return JSONResponse({"error": "page must be at least 1"}, status_code=400)
    if page_size < 1 or page_size > 100:
        return JSONResponse({"error": "page_size must be between 1 and 100"}, status_code=400)
    return db.list_v2_practice_attempt_history(
        target=target,
        sentence_id=sentence_id,
        practice_type=clean_type,
        lesson_id=lesson_id,
        source_context=source_context,
        page=page,
        page_size=page_size,
    )


# ── Phase 6C: /api/phonetics ──────────────────────────────────────────────────

def _normalized_ipa_word(value: str) -> str:
    return str(value or "").replace("’", "'").lower()


def _build_ipa_word_payload(
    sentence: str,
    provided_words: list | None,
    ipa_lib,
    strip,
) -> list[dict]:
    matches = list(_IPA_WORD_RE.finditer(str(sentence or "")))
    provided = provided_words if isinstance(provided_words, list) else []
    use_provided = len(provided) == len(matches) and all(
        _normalized_ipa_word(item.get("word") or item.get("text")) == _normalized_ipa_word(match.group(0))
        for item, match in zip(provided, matches)
        if isinstance(item, dict)
    )
    if use_provided and not all(isinstance(item, dict) for item in provided):
        use_provided = False

    words: list[dict] = []
    for index, match in enumerate(matches):
        word = match.group(0)
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(sentence)
        punctuation_after = sentence[match.end():next_start].strip()
        provided_ipa = str(provided[index].get("ipa") or "").strip() if use_provided else ""
        base_ipa = provided_ipa or strip(ipa_lib.convert(word))
        words.append({
            "word_index": index,
            "word": word,
            "base_ipa": base_ipa,
            "punctuation_after": punctuation_after,
        })
    return words


def _validated_ipa_annotations(base_words: list[dict], raw_words) -> list[dict]:
    if not isinstance(raw_words, list) or len(raw_words) != len(base_words):
        return []
    normalized_words = []
    for item in raw_words:
        if isinstance(item, list) and len(item) >= 6:
            normalized_words.append({
                "word_index": item[0],
                "ipa": item[1],
                "weak": item[2],
                "link_to_next": item[3],
                "break_after": item[4],
                "tone": item[5],
            })
        elif isinstance(item, dict):
            normalized_words.append(item)
    by_index = {
        item.get("word_index"): item
        for item in normalized_words
        if isinstance(item.get("word_index"), int)
    }
    if set(by_index) != set(range(len(base_words))):
        return []

    annotations: list[dict] = []
    for base in base_words:
        item = by_index[base["word_index"]]
        ipa = str(item.get("ipa") or "").strip().strip("/")
        if not ipa or any(character.isspace() for character in ipa):
            return []
        tone = str(item.get("tone") or "").strip()
        if tone not in {"", "↗", "↘"}:
            tone = ""
        annotations.append({
            "word_index": base["word_index"],
            "word": base["word"],
            "ipa": ipa,
            "punctuation_after": base["punctuation_after"],
            "weak": bool(item.get("weak", False)),
            "link_to_next": bool(item.get("link_to_next", False)),
            "break_after": bool(item.get("break_after", False)),
            "tone": tone,
        })
    if annotations:
        annotations[-1]["link_to_next"] = False
    return annotations


def _base_ipa_annotations(base_words: list[dict]) -> list[dict]:
    return [{
        "word_index": item["word_index"],
        "word": item["word"],
        "ipa": item["base_ipa"],
        "punctuation_after": item["punctuation_after"],
        "weak": False,
        "link_to_next": False,
        "break_after": bool(re.search(r"[,.;:!?]", item["punctuation_after"])),
        "tone": "↘" if index == len(base_words) - 1 else "",
    } for index, item in enumerate(base_words)]


def _annotations_to_natural(annotations: list[dict]) -> str:
    rendered: list[str] = []
    for index, item in enumerate(annotations):
        linked_in = index > 0 and annotations[index - 1].get("link_to_next")
        rendered.append(f"{'‿' if linked_in else ''}{item['ipa']}")
        if item.get("tone"):
            rendered.append(item["tone"])
        if item.get("break_after") and index < len(annotations) - 1:
            rendered.append("/")
    return " ".join(rendered)


def _interactive_ipa_cache_key(words: list[dict]) -> str:
    payload = json.dumps(
        {"version": 1, "words": words},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _do_interactive_ipa(data: dict, sentences: list, ipa_lib, strip) -> dict:
    from prompts import IPA_INTERACTIVE_PROOFREAD_PROMPT

    supplied_by_sentence = data.get("base_words")
    if not isinstance(supplied_by_sentence, list):
        supplied_by_sentence = []
    force = bool(data.get("force", False))
    prepared: list[tuple[int, list[dict], str]] = []
    results_by_index: dict[int, dict] = {}
    for sentence_index, sentence in enumerate(sentences):
        supplied = supplied_by_sentence[sentence_index] if sentence_index < len(supplied_by_sentence) else []
        base_words = _build_ipa_word_payload(str(sentence), supplied, ipa_lib, strip)
        cache_key = _interactive_ipa_cache_key(base_words)
        if not force:
            with _IPA_PROOFREAD_CACHE_LOCK:
                cached = _IPA_PROOFREAD_CACHE.get(cache_key)
            if cached:
                results_by_index[sentence_index] = {**cached, "cached": True}
                continue
        prepared.append((sentence_index, base_words, cache_key))

    for batch_start in range(0, len(prepared), 5):
        batch = prepared[batch_start:batch_start + 5]
        payload = {
            "results": [{
                "index": sentence_index + 1,
                "text": sentences[sentence_index],
                "words": [[
                    item["word_index"],
                    item["word"],
                    item["base_ipa"],
                    item["punctuation_after"],
                ] for item in base_words],
            } for sentence_index, base_words, _ in batch]
        }
        try:
            max_tokens = min(1600, max(240, sum(len(words) for _, words, _ in batch) * 24))
            response = ai_config.client.chat.completions.create(
                model=ai_config.AI_MODEL,
                temperature=0,
                max_tokens=max_tokens,
                timeout=45,
                extra_body={"thinking": {"type": "disabled"}},
                messages=[
                    {"role": "system", "content": IPA_INTERACTIVE_PROOFREAD_PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                response_format={"type": "json_object"},
            )
            raw_results = json.loads(response.choices[0].message.content or "{}").get("results", [])
            raw_by_index = {
                item.get("index"): item
                for item in raw_results
                if isinstance(item, dict) and isinstance(item.get("index"), int)
            }
        except Exception as exc:
            print(f"[phonetics] interactive ai_ipa batch {batch_start} failed: {exc}")
            raw_by_index = {}

        for sentence_index, base_words, cache_key in batch:
            raw_item = raw_by_index.get(sentence_index + 1, {})
            annotations = _validated_ipa_annotations(base_words, raw_item.get("words"))
            source = "ai_ipa" if annotations else "rule"
            if not annotations:
                annotations = _base_ipa_annotations(base_words)
            result = {
                "canonical": " ".join(item["base_ipa"] for item in base_words),
                "natural": _annotations_to_natural(annotations),
                "source": source,
                "word_annotations": annotations,
                "cached": False,
            }
            results_by_index[sentence_index] = result
            if source == "ai_ipa":
                with _IPA_PROOFREAD_CACHE_LOCK:
                    if len(_IPA_PROOFREAD_CACHE) >= _IPA_PROOFREAD_CACHE_LIMIT:
                        _IPA_PROOFREAD_CACHE.pop(next(iter(_IPA_PROOFREAD_CACHE)))
                    _IPA_PROOFREAD_CACHE[cache_key] = result

    return {"results": [results_by_index[index] for index in range(len(sentences))]}


def _do_phonetics(data: dict) -> dict:
    """Sync worker — mirrors Flask batch_phonetics() logic exactly."""
    import eng_to_ipa as ipa_lib
    from phonetics_processor import annotate

    sentences = data.get("sentences", [])
    mode = data.get("mode", "rule")
    strip = dict_service.strip_ipa_asterisks

    if mode == "ai_ipa" and data.get("structured"):
        return _do_interactive_ipa(data, sentences, ipa_lib, strip)

    if mode == "ai":
        lesson_id = data.get("lesson_id")
        sentence_indices = data.get("sentence_indices")
        if lesson_id:
            analyses_path = os.path.join("output", f"{lesson_id}.analyses.json")
            if os.path.exists(analyses_path):
                with open(analyses_path, "r", encoding="utf-8") as f:
                    cached_analyses = json.load(f)
                indices = sentence_indices if sentence_indices else list(range(len(sentences)))
                results = []
                for pos, idx in enumerate(indices):
                    if isinstance(cached_analyses, list) and 0 <= idx < len(cached_analyses):
                        sa = cached_analyses[idx]
                        canonical = sa.get("phonetics_canonical", "")
                        natural = sa.get("phonetics_natural", "") or canonical
                        source = sa.get("phonetics_source", "ai")
                        if natural:
                            results.append({"canonical": canonical, "natural": natural, "source": source})
                            continue
                    text = sentences[pos] if pos < len(sentences) else ""
                    if text:
                        raw = strip(ipa_lib.convert(str(text)))
                        results.append({"canonical": raw, "natural": annotate(str(text), raw), "source": "rule"})
                    else:
                        results.append({"canonical": "", "natural": "", "source": "rule"})
                return {"results": results}

    if mode == "ai_ipa":
        use_base = data.get("base", True)
        if use_base:
            from prompts import IPA_ANNOTATION_PROMPT_WITH_BASE
            system_prompt = IPA_ANNOTATION_PROMPT_WITH_BASE
        else:
            from prompts import IPA_ANNOTATION_PROMPT_NOBASE
            system_prompt = IPA_ANNOTATION_PROMPT_NOBASE
        model = ai_config.AI_MODEL
        IPA_BATCH = 5
        results_map: dict[int, str] = {}
        for batch_start in range(0, len(sentences), IPA_BATCH):
            batch = sentences[batch_start: batch_start + IPA_BATCH]
            if use_base:
                payload = []
                for j, text in enumerate(batch):
                    raw_c = strip(ipa_lib.convert(str(text)))
                    payload.append({
                        "index": batch_start + j + 1,
                        "text": text,
                        "canonical_ipa": raw_c,
                        "rule_natural_ipa": annotate(str(text), raw_c),
                    })
            else:
                payload = [{"index": batch_start + j + 1, "text": t} for j, t in enumerate(batch)]
            try:
                resp = ai_config.client.chat.completions.create(
                    model=model,
                    temperature=0.2,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    response_format={"type": "json_object"},
                )
                items = json.loads(resp.choices[0].message.content).get("results", [])
                for item in items:
                    results_map[item["index"] - 1] = item.get("natural_ipa", "")
            except Exception as exc:
                print(f"[phonetics] ai_ipa batch {batch_start} failed: {exc}")
        ai_results = []
        for k in range(len(sentences)):
            nat = results_map.get(k, "")
            if nat:
                ai_results.append({"canonical": nat, "natural": nat, "source": "ai_ipa"})
            else:
                text = sentences[k]
                raw = strip(ipa_lib.convert(str(text)))
                ai_results.append({"canonical": raw, "natural": annotate(str(text), raw), "source": "rule"})
        return {"results": ai_results}

    # default: mode="rule"
    results = []
    for text in sentences:
        if not text:
            results.append({"canonical": "", "natural": "", "source": "rule"})
            continue
        raw = strip(ipa_lib.convert(str(text)))
        results.append({"canonical": raw, "natural": annotate(str(text), raw), "source": "rule"})
    return {"results": results}


@router.post("/api/phonetics")
async def batch_phonetics(request: Request):
    if not dict_service.ipa_ready:
        return JSONResponse({"error": "eng-to-ipa not installed"}, status_code=500)
    data = await _parse_body(request)
    try:
        result = await run_in_threadpool(_do_phonetics, data)
        return result
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Phase 6D: /api/oral-analysis ─────────────────────────────────────────────

@router.post("/api/oral-analysis")
async def oral_analysis(request: Request):
    data = await _parse_body(request)
    english = str(data.get("english") or "").strip()
    sentence_id = data.get("sentence_id")
    persist = bool(data.get("persist")) or sentence_id is not None
    force = bool(data.get("force"))
    sentence = None
    if sentence_id is not None:
        try:
            sentence_id = int(sentence_id)
        except (TypeError, ValueError):
            return JSONResponse({"error": "invalid sentence_id"}, status_code=400)
        sentence = db.get_v2_sentence_by_id(sentence_id)
        if not sentence:
            return JSONResponse({"error": "Sentence not found"}, status_code=404)
        english = str(sentence.get("text") or english).strip()
    elif persist and english:
        sentence = db.upsert_v2_sentence(english)
        sentence_id = int(sentence["id"])
    if not english:
        return JSONResponse({"error": "english required"}, status_code=400)
    if sentence_id is not None and not force:
        cached_pattern = db.get_v2_sentence_pattern(sentence_id)
        cached_analysis = (cached_pattern or {}).get("analysis") or {}
        if cached_analysis:
            return {**cached_analysis, "cached": True, "sentence_id": sentence_id}
    try:
        resp = await run_in_threadpool(
            ai_config.client.chat.completions.create,
            model=ai_config.AI_MODEL,
            messages=[{"role": "user", "content": ORAL_ANALYSIS_PROMPT.format(english=english)}],
            response_format={"type": "json_object"},
            temperature=0.4,
            max_tokens=1000,
        )
        analysis = json.loads(resp.choices[0].message.content)
        if not isinstance(analysis, dict):
            raise ValueError("AI oral analysis must be an object")
        if persist and sentence_id is not None:
            db.save_v2_sentence_analysis(sentence_id, analysis)
        return {**analysis, "cached": False, "sentence_id": sentence_id}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/api/listening-retell-analysis")
async def listening_retell_analysis(request: Request):
    data = await _parse_body(request)
    english = str(data.get("english") or "").strip()
    retelling = str(data.get("retelling") or "").strip()
    if not english:
        return JSONResponse({"error": "english required"}, status_code=400)
    if not retelling:
        return JSONResponse({"error": "retelling required"}, status_code=400)
    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            resp = await run_in_threadpool(
                ai_config.client.chat.completions.create,
                model=ai_config.AI_MODEL,
                messages=[{
                    "role": "user",
                    "content": LISTENING_RETELL_PROMPT.format(
                        english=english,
                        retelling=retelling,
                    ),
                }],
                response_format={"type": "json_object"},
                temperature=0.3,
                max_tokens=4096,
                extra_body={"thinking": {"type": "disabled"}},
            )
            content = (resp.choices[0].message.content or "").strip()
            if not content:
                raise ValueError("AI returned an empty retell analysis")
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError("AI retell analysis must be an object")
            return result
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
    return JSONResponse(
        {"error": f"复述分析未返回有效内容，已自动重试：{last_error}"},
        status_code=502,
    )


# ── Phase 6E: /analyze-connected-speech ──────────────────────────────────────

@router.post("/analyze-connected-speech")
async def analyze_connected_speech(request: Request):
    data = await _parse_body(request)
    sentence = data.get("sentence", "").strip()
    phonetics = data.get("phonetics", "").strip()
    if not sentence:
        return JSONResponse({"error": "sentence required"}, status_code=400)
    prompt = CONNECTED_SPEECH_PROMPT.format(
        sentence=sentence,
        phonetics=phonetics or "（未提供）",
    )
    try:
        resp = await run_in_threadpool(
            ai_config.client.chat.completions.create,
            model=ai_config.AI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=600,
        )
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            return JSONResponse({"error": "AI 未返回有效内容，请重试"}, status_code=500)
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return JSONResponse({"error": "AI 返回格式错误，请重试"}, status_code=500)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
