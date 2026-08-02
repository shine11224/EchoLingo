"""Dictionary and IPA helpers shared by FastAPI routes."""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path

_DEFAULT_DICT_DIR = Path.home() / "AppData" / "Roaming" / "Francochinois" / "eudic" / "dict"
DICT_DIR = os.environ.get("DICT_DIR", "") or str(_DEFAULT_DICT_DIR)
DICTS = {
    "oald": "oald9.mdx",
    "longman": "LongmanDictionaryOfContemporaryEnglish6thEnEn.mdx",
    "vocab": "Vocabulary.com Dictionary.mdx",
}
MDD_FILES = ["oald9.mdd", "oald9.1.mdd", "oald9.2.mdd", "Vocabulary.com Dictionary.mdd"]
_MDX_CACHE = {}
_MDX_KEY_INDEX_CACHE = {}
_MDX_CACHE_LOCK = threading.Lock()


def strip_html(html: str | None) -> str:
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:1200]


def lookup_dict(dict_key: str, word: str) -> str:
    fname = DICTS.get(dict_key)
    if not fname:
        return ""
    try:
        path = os.path.join(DICT_DIR, fname)
        result = _lookup_mdx(path, word)
        if result and "@@@LINK" in result:
            chunks = result.split("\n---\n")
            real = [chunk for chunk in chunks if not chunk.startswith("@@@LINK")]
            if real:
                # 混合记录（如 OALD9 OL 版）：丢弃跳转记录，保留真实词条
                result = "\n---\n".join(real)
            else:
                linked = chunks[0].split("=", 1)[-1].split("\n")[0].strip().strip("\x00\r\n")
                result = _lookup_mdx(path, linked)
        return strip_html(result) if result else ""
    except Exception:
        return ""


def _get_mdx_reader(path: str):
    cached = _MDX_CACHE.get(path)
    if cached is not None:
        return cached
    with _MDX_CACHE_LOCK:
        cached = _MDX_CACHE.get(path)
        if cached is None:
            from mdict_utils.reader import MDX

            cached = MDX(path, "", False, None)
            key_index = {}
            for index, (_, key) in enumerate(cached._key_list):
                positions = key_index.get(key)
                if positions is None:
                    key_index[key] = index
                elif isinstance(positions, int):
                    key_index[key] = [positions, index]
                else:
                    positions.append(index)
            _MDX_CACHE[path] = cached
            _MDX_KEY_INDEX_CACHE[path] = key_index
    return cached


def _lookup_mdx(path: str, word: str) -> str:
    """Reuse one parsed MDX reader and exact-key index instead of reopening per word."""
    from mdict_utils.reader import get_record

    reader = _get_mdx_reader(path)
    key_list = reader._key_list
    target = word.encode("UTF-8")
    positions = _MDX_KEY_INDEX_CACHE[path].get(target)
    if positions is None:
        return ""
    if isinstance(positions, int):
        positions = [positions]
    records = []
    for index in positions:
        offset, key = key_list[index]
        length = key_list[index + 1][0] - offset if index + 1 < len(key_list) else -1
        record = get_record(reader, key, offset, length)
        if record:
            records.append(record)
    return "\n---\n".join(records)


def lookup_all(word: str) -> dict[str, str]:
    return {key: lookup_dict(key, word.lower()) for key in DICTS}


mdd_cache = {}


def get_mdd(fname: str):
    if fname not in mdd_cache:
        try:
            from mdict_utils.reader import MDD

            mdd_cache[fname] = MDD(os.path.join(DICT_DIR, fname))
        except Exception:
            mdd_cache[fname] = None
    return mdd_cache[fname]


ipa_ready = False


def init_ipa() -> None:
    global ipa_ready
    try:
        import eng_to_ipa  # noqa: F401
        from phonetics_processor import annotate  # noqa: F401
        ipa_ready = True
    except ImportError:
        ipa_ready = False


init_ipa()


def strip_ipa_asterisks(raw: str) -> str:
    return re.sub(r"\*([^*]+)\*", r"\1", raw)
