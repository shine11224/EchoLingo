"""V2 vocabulary highlighting service."""
import html
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import db
from webapp.services import dicts as dict_service

# Common noise words to exclude from highlighting (articles, pronouns, short function words)
_NOISE_WORDS: set[str] | None = None
_INTERMEDIATE_WORDS: set[str] | None = None
_WORD_MEANINGS: dict[str, str] | None = None
_V2_WORDLIST_INDEX: dict[str, dict] | None = None
_LOOKUP_CACHE: dict[str, dict] = {}
_WORD_MEANING_PLACEHOLDERS = {
    "查询中",
    "查询中...",
    "查询中…",
    "正在查询",
    "正在查询...",
    "正在查询…",
    "暂无释义",
    "查询失败，请稍后重试",
    "本地与在线查询均未命中",
}

_COMPILED_DIR = Path(__file__).resolve().parents[3] / "resources" / "wordlists" / "wordlists" / "compiled"
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")

_COMMON_MEANINGS = {
    "randomly": "随机地；任意地",
    "a": "一个；某一",
    "an": "一个；某一",
    "and": "和；并且",
    "are": "是；在",
    "as": "作为；像；当...时",
    "be": "是；成为",
    "became": "变成；成为",
    "become": "成为；变得",
    "better": "更好的；更好地",
    "can": "能够；可以",
    "complex": "复杂的；复合的",
    "content": "内容",
    "do": "做；进行",
    "for": "为了；给",
    "from": "来自；从",
    "have": "有；已经",
    "help": "帮助",
    "how": "如何；怎样",
    "in": "在...里",
    "into": "进入；变成",
    "is": "是",
    "it": "它",
    "learn": "学习",
    "method": "方法",
    "not": "不",
    "of": "...的",
    "on": "在...上；关于",
    "or": "或者",
    "practical": "实用的；实际的",
    "read": "阅读；读",
    "remember": "记住；记得",
    "sentence": "句子",
    "system": "系统",
    "that": "那；那个；引导从句",
    "the": "这个；那个",
    "this": "这；这个",
    "to": "到；向；用于不定式",
    "use": "使用",
    "what": "什么；所...的事",
    "with": "和；带有",
    "word": "单词；词",
    "you": "你；你们",
}


def _get_noise_words() -> set[str]:
    global _NOISE_WORDS
    if _NOISE_WORDS is None:
        _NOISE_WORDS = {
            "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
            "have", "has", "had", "do", "does", "did", "will", "would", "shall",
            "should", "may", "might", "can", "could", "must", "i", "you", "he",
            "she", "it", "we", "they", "me", "him", "her", "us", "them", "my",
            "your", "his", "its", "our", "their", "mine", "yours", "hers", "ours",
            "theirs", "this", "that", "these", "those", "am", "not", "no", "to",
            "of", "in", "for", "on", "with", "at", "by", "from", "as", "into",
            "about", "like", "after", "between", "through", "over", "before",
            "under", "again", "further", "then", "once", "here", "there", "when",
            "where", "why", "how", "all", "both", "each", "few", "more", "most",
            "other", "some", "such", "only", "own", "same", "so", "than", "too",
            "very", "and", "but", "or", "nor", "just", "because", "if", "while",
            "up", "out", "off", "down", "also", "even", "still", "well", "now",
        }
    return _NOISE_WORDS


def tokenize_for_lookup(text: str) -> list[str]:
    raw_words = re.findall(r"[a-zA-Z]+(?:'[a-zA-Z]+)?", text)
    noise = _get_noise_words()
    seen: set[str] = set()
    result: list[str] = []
    for w in raw_words:
        lower = w.lower()
        if len(lower) <= 2 or lower in noise or w[0].isupper():
            continue
        if lower not in seen:
            seen.add(lower)
            result.append(lower)
    return result


def load_default_intermediate_words() -> set[str]:
    global _INTERMEDIATE_WORDS
    if _INTERMEDIATE_WORDS is not None:
        return _INTERMEDIATE_WORDS

    # Prefer BNC 4k-6k wordlist as closest to 3000-5000 intermediate range.
    # Fall back to CEFR B1 if BNC file is missing, then CEFR B2.
    candidates = ["user_bnc_4k_6k.json", "cefr_b1.json", "cefr_b2.json"]
    _INTERMEDIATE_WORDS = set()
    for filename in candidates:
        path = _COMPILED_DIR / filename
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            _INTERMEDIATE_WORDS = {w.lower() for w in data.get("words", [])}
            break
        except Exception:
            continue
    return _INTERMEDIATE_WORDS


def _load_compiled_word_set(filename: str) -> set[str]:
    path = _COMPILED_DIR / filename
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    words = data.get("words") if isinstance(data, dict) else data
    if isinstance(words, dict):
        return {str(word).lower() for word in words}
    if isinstance(words, list):
        return {str(word).lower() for word in words if isinstance(word, str)}
    return set()


def load_v2_wordlist_index() -> dict[str, dict]:
    global _V2_WORDLIST_INDEX
    if _V2_WORDLIST_INDEX is not None:
        return _V2_WORDLIST_INDEX

    words: dict[str, dict] = {}
    for word in load_default_intermediate_words():
        words[word] = {"word": word, "level": "ielts"}
    for word in _load_compiled_word_set("domain_academic.json"):
        words.setdefault(word, {"word": word, "level": "academic"})
    for word in _load_compiled_word_set("cefr_b1.json"):
        words.setdefault(word, {"word": word, "level": "b1"})
    _V2_WORDLIST_INDEX = words
    return _V2_WORDLIST_INDEX


def clear_vocab_caches() -> None:
    global _INTERMEDIATE_WORDS, _WORD_MEANINGS, _V2_WORDLIST_INDEX
    _INTERMEDIATE_WORDS = None
    _WORD_MEANINGS = None
    _V2_WORDLIST_INDEX = None
    _LOOKUP_CACHE.clear()


def _meaning_from_analysis(analysis: Any) -> str:
    if not isinstance(analysis, dict):
        return ""
    basic = str(analysis.get("basic_meaning") or "").strip()
    if basic:
        return basic
    vocab = analysis.get("vocabulary")
    if isinstance(vocab, list):
        for item in vocab:
            if isinstance(item, dict):
                meaning = str(item.get("meaning") or "").strip()
                if meaning:
                    return meaning
    return ""


def load_word_meanings() -> dict[str, str]:
    global _WORD_MEANINGS
    if _WORD_MEANINGS is not None:
        return _WORD_MEANINGS

    meanings: dict[str, str] = {}
    for path in sorted(_COMPILED_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        words = data.get("words") if isinstance(data, dict) else data
        if isinstance(words, dict):
            for word, payload in words.items():
                meaning = ""
                if isinstance(payload, dict):
                    meaning = str(payload.get("basic_meaning") or payload.get("meaning") or "").strip()
                elif isinstance(payload, str):
                    meaning = payload.strip()
                if meaning and not meanings.get(word.lower()):
                    meanings[word.lower()] = meaning
        elif isinstance(words, list):
            for word in words:
                if isinstance(word, str):
                    meanings.setdefault(word.lower(), "")

    try:
        for word, entry in db.get_all_words().items():
            meaning = _meaning_from_analysis(entry.get("cached_analysis"))
            if meaning:
                meanings[word.lower()] = meaning
    except Exception:
        pass

    _WORD_MEANINGS = meanings
    return _WORD_MEANINGS


def remember_word_meaning(word: str, meaning: str) -> None:
    normalized = re.sub(r"[^a-zA-Z']", "", word or "").lower().strip("'")
    if not normalized:
        return
    meanings = load_word_meanings()
    if meaning and not is_word_meaning_placeholder(meaning):
        meanings[normalized] = meaning
    for key in (f"{normalized}:0", f"{normalized}:1"):
        _LOOKUP_CACHE.pop(key, None)


def is_word_meaning_placeholder(meaning: str) -> bool:
    return re.sub(r"\s+", "", str(meaning or "")) in _WORD_MEANING_PLACEHOLDERS


def forget_word_meaning_cache(word: str) -> None:
    normalized = re.sub(r"[^a-zA-Z']", "", word or "").lower().strip("'")
    if not normalized:
        return
    for key in (f"{normalized}:0", f"{normalized}:1"):
        _LOOKUP_CACHE.pop(key, None)


def _get_ipa(word: str) -> str:
    try:
        from analyzer import _to_canonical_ipa
        ipa = _to_canonical_ipa(word)
        cleaned = ipa.rstrip("*").strip() if ipa else ""
        if re.sub(r"[^a-z]", "", cleaned.lower()) == re.sub(r"[^a-z]", "", word.lower()):
            return ""
        return cleaned
    except Exception:
        return ""


def lookup_word_meaning(word: str, *, allow_external_fallback: bool = False) -> dict:
    surface = re.sub(r"[^a-zA-Z'-]", "", word or "").lower().strip("'-")
    normalized = surface.replace("-", "")
    if not normalized:
        return {"word": "", "meaning": "", "phonetic": "", "found": False}
    cache_key = f"{normalized}:{int(allow_external_fallback)}"
    if cache_key in _LOOKUP_CACHE:
        return dict(_LOOKUP_CACHE[cache_key])
    meanings = load_word_meanings()
    candidates = _lookup_candidates(surface)
    for candidate in candidates:
        for meaning in (meanings.get(candidate, ""), _COMMON_MEANINGS.get(candidate, "")):
            if meaning and not is_word_meaning_placeholder(meaning) and _concise_gloss(meaning):
                phonetic = _get_ipa(normalized)
                result = {
                    "word": normalized,
                    "lemma": candidate,
                    "meaning": meaning,
                    "phonetic": phonetic,
                    "found": True,
                    "source": "compiled",
                }
                _LOOKUP_CACHE[cache_key] = result
                return dict(result)
    dict_meaning = _lookup_local_dict_meaning(candidates)
    local_result = None
    if dict_meaning:
        concise_meaning = _concise_gloss(dict_meaning, limit=20) or dict_meaning
        local_result = {
            "word": normalized,
            "lemma": _preferred_lookup_lemma(candidates),
            "meaning": concise_meaning,
            "phonetic": _get_ipa(normalized),
            "found": True,
            "source": "local",
        }
        if not allow_external_fallback or _concise_gloss(concise_meaning, limit=20):
            meanings[normalized] = concise_meaning
            _LOOKUP_CACHE[cache_key] = local_result
            return dict(local_result)
    if not allow_external_fallback:
        phonetic = _get_ipa(normalized)
        result = {"word": normalized, "lemma": normalized, "meaning": "", "phonetic": phonetic, "found": False}
        _LOOKUP_CACHE[cache_key] = result
        return dict(result)
    fallback = _translate_word_fallback(surface)
    if fallback:
        meanings[normalized] = fallback
        result = {
            "word": normalized,
            "lemma": _preferred_lookup_lemma(candidates),
            "meaning": fallback,
            "phonetic": _get_ipa(normalized),
            "found": True,
            "source": "external",
        }
        _LOOKUP_CACHE[cache_key] = result
        return dict(result)
    if local_result:
        meanings[normalized] = local_result["meaning"]
        _LOOKUP_CACHE[cache_key] = local_result
        return dict(local_result)
    result = {"word": normalized, "lemma": normalized, "meaning": "", "found": False}
    _LOOKUP_CACHE[cache_key] = result
    return dict(result)


def _lookup_local_dict_meaning(candidates: list[str]) -> str:
    english_fallback = ""
    for candidate in candidates:
        meaning = str(dict_service.lookup_ecdict(candidate) or "").strip()
        if meaning:
            summary = _summarize_local_dict_meaning(meaning)
            if _concise_gloss(summary):
                return summary
            if summary and not english_fallback:
                english_fallback = summary
    return english_fallback


def _summarize_local_dict_meaning(raw: str) -> str:
    text = html.unescape(str(raw or ""))
    text = re.sub(r"@@@LINK=[^\s]+", " ", text)
    text = re.sub(r"/[^/]{1,80}/", " ", text)
    text = re.sub(r"\b(?:BrE|NAmE|especially|synonym|opposite)\b", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" ;,")
    if not text:
        return ""

    zh_parts = []
    zh_pattern = r"[\u4e00-\u9fff][\u4e00-\u9fff，；、（）()的地得之一二三四五六七八九十0-9-]{1,80}"
    for match in re.finditer(zh_pattern, text):
        part = match.group(0).strip(" ，,；;、")
        if len(part) <= 4 and part.endswith("等"):
            continue
        if part and part not in zh_parts:
            zh_parts.append(part)
        if len(zh_parts) >= 2:
            break

    english_source = re.sub(zh_pattern, " ", text)
    chunks = [
        chunk.strip(" -;:,.")
        for chunk in re.split(r"(?<=[.!?])\s+|\s+[0-9]+\s+", english_source)
    ]
    en_part = ""
    for chunk in chunks:
        if not chunk or len(chunk) < 18:
            continue
        if not re.search(r"[A-Za-z]", chunk):
            continue
        if re.search(r"\b(?:word origin|example bank|see also|grammar point)\b", chunk, flags=re.I):
            continue
        en_part = chunk[:180].strip(" -;:,.")
        break

    parts = []
    if zh_parts:
        parts.append("中文：" + "；".join(zh_parts))
    if en_part:
        parts.append("英文：" + en_part)
    if parts:
        return "\n".join(parts)[:360]
    return text[:260]


def _lookup_candidates(word: str) -> list[str]:
    candidates = [word]
    if word.endswith("'s") and len(word) > 3:
        candidates.append(word[:-2])
    if word.endswith("ies") and len(word) > 4:
        candidates.append(word[:-3] + "y")
    if word.endswith("ied") and len(word) > 4:
        candidates.append(word[:-3] + "y")
    if word.endswith("ing") and len(word) > 5:
        base = word[:-3]
        candidates.append(base)
        if len(base) > 1 and base[-1] == base[-2]:
            candidates.append(base[:-1])
        candidates.append(base + "e")
    if word.endswith("ed") and len(word) > 4:
        base = word[:-2]
        candidates.append(base)
        if len(base) > 1 and base[-1] == base[-2]:
            candidates.append(base[:-1])
        candidates.append(base + "e")
    if word.endswith("es") and len(word) > 4:
        candidates.append(word[:-2])
    if word.endswith("s") and len(word) > 3:
        candidates.append(word[:-1])

    seen = set()
    result = []
    for candidate in [*candidates, *(item.replace("-", "") for item in candidates)]:
        if candidate and candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result


def _preferred_lookup_lemma(candidates: list[str]) -> str:
    for candidate in reversed(candidates):
        if "-" in candidate:
            return candidate
    return candidates[-1] if candidates else ""


def _normalize_reading_word(word: str) -> str:
    normalized = re.sub(r"[^a-zA-Z']", "", word or "").lower().strip("'")
    candidates = _lookup_candidates(normalized)
    return candidates[0] if candidates else normalized


def _reading_highlight_match(word: str, word_index: dict[str, dict]) -> tuple[str, dict] | None:
    normalized = re.sub(r"[^a-zA-Z']", "", word or "").lower().strip("'")
    if len(normalized) <= 2 or normalized in _get_noise_words():
        return None
    for candidate in _lookup_candidates(normalized):
        meta = word_index.get(candidate)
        if meta:
            return candidate, meta
    return None


def highlight_reading_blocks(
    blocks: list[dict],
    hidden_words: set[str] | None = None,
    *,
    include_meanings: bool = True,
) -> dict:
    word_index = load_v2_wordlist_index()
    hidden = {word.lower() for word in (hidden_words or set())}
    meanings = load_word_meanings() if include_meanings else {}
    gloss_cache: dict[str, str] = {}
    result: list[dict] = []
    candidate_count = 0
    for block in blocks:
        text = str(block.get("text", ""))
        highlights: list[dict] = []
        for match in WORD_RE.finditer(text):
            word = match.group(0)
            highlighted = _reading_highlight_match(word, word_index)
            if not highlighted:
                continue
            normalized, meta = highlighted
            if normalized in hidden:
                continue
            if normalized not in gloss_cache:
                if not include_meanings:
                    gloss_cache[normalized] = ""
                else:
                    candidates = _lookup_candidates(normalized)
                    gloss = ""
                    for candidate in candidates:
                        for meaning in (meanings.get(candidate, ""), _COMMON_MEANINGS.get(candidate, "")):
                            gloss = _concise_gloss(meaning)
                            if gloss:
                                break
                        if gloss:
                            break
                    if not gloss:
                        gloss = _concise_gloss(_lookup_local_dict_meaning(candidates))
                    gloss_cache[normalized] = gloss
            highlights.append({
                "word": word,
                "normalized": normalized,
                "level": meta.get("level", "ielts"),
                "meaning": gloss_cache[normalized],
                "start": match.start(),
                "end": match.end(),
            })
        candidate_count += len(highlights)
        block_copy = dict(block)
        block_copy["highlights"] = highlights
        result.append(block_copy)
    return {"blocks": result, "candidate_count": candidate_count}


def _concise_gloss(meaning: str, limit: int = 14) -> str:
    """词下注释只需要短释义：去掉词典长文中的英文段、前缀，按分号截取前几个义项。"""
    text = str(meaning or "").strip()
    if not text:
        return ""
    text = text.split("英文：", 1)[0]
    text = text.removeprefix("中文：").strip()
    text = text.split("\n", 1)[0].strip()
    text = re.sub(r"\s+[A-Za-z][A-Za-z\s'()./=-]*.*$", "", text).strip()
    if "（" not in text and "(" not in text:
        text = text.replace("）", "").replace(")", "")
    senses = []
    for sense in re.split(r"[；;]", text):
        sense = sense.strip()
        if not sense:
            continue
        is_example = bool(re.search(r"[。！？!?]\s*$", sense))
        is_example = is_example or (
            len(sense) >= 9
            and (
                re.search(r"(?:是由|需要|已经|正在|应该|好像|似乎|我们|他们|我的|你的|他的)", sense)
                or sense.endswith(("的", "了"))
            )
        )
        if not is_example:
            senses.append(sense)
    if not senses:
        return ""
    text = "；".join(senses)
    if len(text) <= limit and len(senses) <= 3:
        return text
    out = ""
    for sense in senses[:3]:
        candidate = f"{out}；{sense}" if out else sense
        if len(candidate) > limit:
            break
        out = candidate
    if out:
        return out
    return text[: limit - 1] + "…"


def _translate_word_fallback(word: str) -> str:
    if len(word) <= 2:
        return ""
    ai_meaning = _ai_word_gloss_fallback(word)
    if ai_meaning:
        return ai_meaning
    query = urllib.parse.urlencode({"q": word, "langpair": "en|zh-CN"})
    url = f"https://api.mymemory.translated.net/get?{query}"
    translated = ""
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception:
        data = {}
    translated = str(data.get("responseData", {}).get("translatedText") or "").strip()
    if translated and translated.lower() != word.lower() and re.search(r"[\u4e00-\u9fff]", translated):
        return translated
    return _ai_word_gloss_fallback(word)


def _ai_word_gloss_fallback(word: str) -> str:
    try:
        from prompts import WORD_GLOSS_FALLBACK_PROMPT
        from webapp.runtime import ai_config

        if not ai_config.AI_API_KEY:
            return ""
        response = ai_config.client.chat.completions.create(
            model=ai_config.AI_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": WORD_GLOSS_FALLBACK_PROMPT.format(word=word),
                }
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=80,
            extra_body={"thinking": {"type": "disabled"}},
        )
        payload = json.loads(response.choices[0].message.content or "{}")
        meaning = str(payload.get("meaning") or "").strip()
    except Exception:
        return ""
    if not re.search(r"[\u4e00-\u9fff]", meaning):
        return ""
    return meaning[:80]


def should_highlight_token(token: str) -> bool:
    words = load_default_intermediate_words()
    return token.lower() in words


def highlight_segments(
    segments: list[dict],
    hidden_words: set[str] | None = None,
    *,
    include_meanings: bool = True,
) -> list[dict]:
    intermediate = load_default_intermediate_words()
    meanings = load_word_meanings() if include_meanings else {}
    noise = _get_noise_words()
    hidden = {word.lower() for word in (hidden_words or set())}
    result: list[dict] = []
    for seg in segments:
        text = seg.get("text", "")
        words = re.findall(r"[a-zA-Z]+(?:'[a-zA-Z]+)?", text.lower())
        highlighted: list[str] = []
        seen: set[str] = set()
        for w in words:
            if len(w) <= 2 or w in noise:
                continue
            if w in hidden:
                continue
            if w in intermediate and w not in seen:
                seen.add(w)
                highlighted.append(w)
        seg_copy = dict(seg)
        seg_copy["highlighted_words"] = highlighted
        seg_copy["word_meanings"] = {w: meanings.get(w, "") for w in highlighted if meanings.get(w, "")}
        result.append(seg_copy)
    return result
