from __future__ import annotations

import json
import os
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

import requests as _http
from openai import OpenAI

from prompts import MERGE_ONLY_PROMPT, NATURAL_COMBINED_ANALYSIS_PROMPT
from schemas import PatternItem, Segment, SentenceAnalysis, VocabularyItem


BASE_DIR = Path(__file__).resolve().parent


def _free_translate(text: str, timeout: int = 4) -> str:
    """MyMemory 免费翻译（en→zh-CN），无 API key 时的字幕/词义兜底。失败静默返回空字符串。"""
    if not text.strip():
        return ""
    try:
        url = "https://api.mymemory.translated.net/get"
        resp = _http.get(url, params={"q": text[:500], "langpair": "en|zh-CN"}, timeout=timeout)
        data = resp.json()
        translated = data.get("responseData", {}).get("translatedText", "")
        return translated if isinstance(translated, str) and translated else ""
    except Exception:
        return ""


def _enrich_free_translations(analyses: list) -> None:
    """并发调用 MyMemory 为 fallback 分析填充中文翻译和词义。"""
    tasks: list[tuple] = []  # (type, index, sub_index, text)
    for i, a in enumerate(analyses):
        if not a.translation:
            tasks.append(("sent", i, 0, a.text))
        for j, v in enumerate(a.vocabulary):
            if not v.meaning or v.meaning == "建议结合上下文理解":
                tasks.append(("word", i, j, v.word))

    if not tasks:
        return

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_free_translate, t[3]): t for t in tasks}
        for future in as_completed(futures):
            kind, i, j, _ = futures[future]
            result = future.result()
            if not result:
                continue
            if kind == "sent":
                analyses[i].translation = result
            else:
                analyses[i].vocabulary[j].meaning = result


def _load_dotenv() -> None:
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


_load_dotenv()

try:
    import eng_to_ipa as _ipa_lib
    from phonetics_processor import (
        annotate as _annotate_ipa,
        apply_word_pronunciation_overrides as _apply_word_pronunciation_overrides,
    )

    def _to_canonical_ipa(text: str) -> str:
        raw = _ipa_lib.convert(text)
        canonical = re.sub(r'\*([^*]+)\*', r'\1', raw)
        return _apply_word_pronunciation_overrides(text, canonical)

    def _to_rule_natural_ipa(text: str) -> str:
        return _annotate_ipa(text, _to_canonical_ipa(text))

except ImportError:
    _ipa_lib = None
    def _to_canonical_ipa(text: str) -> str:
        return ""

    def _to_rule_natural_ipa(text: str) -> str:
        return ""


# prompts 已迁移至 prompts.py，此处通过顶部 import 引入


def _save_trace(trace_dir: "Path | None", filename: str, data: object) -> None:
    if trace_dir is None:
        return
    import json as _json
    p = Path(trace_dir)
    p.mkdir(parents=True, exist_ok=True)
    (p / filename).write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[trace] {filename} → {p}")


class SentenceAnalyzer:
    def __init__(self, mode: str = "auto", model: str | None = None) -> None:
        self.mode = mode
        self.model = model or _first_env("AI_MODEL", "DEEPSEEK_MODEL", default="deepseek-v4-flash")
        api_key = _first_env("AI_API_KEY", "DEEPSEEK_API_KEY", "deepseek", "DEEPSEEK")
        base_url = _first_env("AI_BASE_URL", "base_url_deepseek", default="https://api.deepseek.com")
        self._has_key = bool(api_key)
        self.client = OpenAI(api_key=api_key, base_url=base_url) if self._has_key else None
        self._ENABLE_AI_BOUNDARY_REVIEW = os.getenv("ELT_ENABLE_AI_BOUNDARY_REVIEW", "1") != "0"
        self._ENABLE_AI_IPA_REFINEMENT = os.getenv("ELT_ENABLE_AI_IPA_REFINEMENT", "1") != "0"

    def analyze(self, segments: list[Segment], lesson_title: str) -> list[SentenceAnalysis]:
        """兼容入口：将输入 segments 直接走 analyze_from_raw_segments 主路径，丢弃返回的 segments。"""
        _, analyses = self.analyze_from_raw_segments(segments, lesson_title)
        return analyses

    _SENT_END = re.compile(r'[.?!;]["\')\s]*$')
    _SENTENCE_SPAN = re.compile(r'.+?(?:[.?!;]["\')\]]*|$)(?=\s+|$)', re.S)
    _HARD_MAX_SENTENCE_WORDS = 45
    _MAX_COMMA_CONTINUATION_WORDS = 45
    # ASR 转录丢句末点时的大写断句规则（原 v2_intensive 阅读尺，615c277 引入，
    # dd2d5d9 统一断句后成死代码，2026-08-14 并入统一断句尺）：
    # 句中大写词视作句界，但连续大写专名（New York / River Thames）不构成句界；
    # They/This 等句中恒小写的功能词大写时是强句界信号，即使前词是专名
    # （覆盖 "Oatly They attracted…" 这类专名+丢标点粘连）；
    # 冠词后的大写词是形容词性专名（the Swedish brand / a British firm），不构成句界。
    _CAPITAL_BOUNDARY_RE = re.compile(r"\s+(?=[A-Z](?:[a-z]+)?\b)")
    _CAPITAL_SPLIT_EXCLUDED_TOKENS = {"I"}
    _CAPITAL_SPLIT_FUNCTION_WORDS = {
        "They", "He", "She", "It", "We", "You",
        "This", "That", "These", "Those", "There",
    }
    _CAPITAL_SPLIT_ARTICLES = {"the", "a", "an"}
    _CAPITAL_SPLIT_MIN_WORDS = 8
    _SENTENCE_TERMINAL_RE = re.compile(r'[.!?]["\')\]]?$')
    _AI_WINDOW_WORDS = 120
    _AI_CONTEXT_WORDS = 25
    _FORBIDDEN_BOUNDARY_AFTER = {
        "a", "an", "the", "to", "of", "in", "on", "at", "for", "with", "by", "from",
        "and", "or", "but", "because", "if", "than", "that", "which", "who", "whose",
        "give", "gives", "gave", "given", "help", "helps", "helped", "helping",
        "revolve", "revolves", "revolved", "revolving",
    }
    _MOVE_BOUNDARY_BEFORE = {"so"}
    _ENABLE_AI_BOUNDARY_REVIEW = os.getenv("ELT_ENABLE_AI_BOUNDARY_REVIEW", "1") != "0"
    _ENABLE_AI_IPA_REFINEMENT = os.getenv("ELT_ENABLE_AI_IPA_REFINEMENT", "1") != "0"

    def _rule_presplit_segments(self, segments: list[Segment]) -> list[Segment]:
        """按句末标点(.?!;)将 Whisper 碎片预合并成候选句，保证 AI 不跨标点合并。"""
        result: list[Segment] = []
        buf: list[Segment] = []
        for seg in segments:
            buf.append(seg)
            if self._SENT_END.search(seg.text.strip()):
                text = " ".join(s.text for s in buf)
                starts = [s.start for s in buf if s.start is not None]
                ends   = [s.end   for s in buf if s.end   is not None]
                result.append(Segment(index=0, text=text,
                                      start=min(starts) if starts else None,
                                      end=max(ends)     if ends   else None,
                                      translation=""))
                buf = []
        if buf:
            text = " ".join(s.text for s in buf)
            starts = [s.start for s in buf if s.start is not None]
            ends   = [s.end   for s in buf if s.end   is not None]
            result.append(Segment(index=0, text=text,
                                  start=min(starts) if starts else None,
                                  end=max(ends)     if ends   else None,
                                  translation=""))
        return result

    def _make_batches(self, segments: list[Segment], max_size: int) -> list[list[Segment]]:
        """将碎片按句末标点切批，批次边界尽量落在句末，避免一句横跨两批。"""
        batches: list[list[Segment]] = []
        i = 0
        while i < len(segments):
            end = min(i + max_size, len(segments))
            if end < len(segments):
                # 在 [i, end) 内找最后一个以句末标点结尾的位置
                last_boundary = -1
                for j in range(i, end):
                    if self._SENT_END.search(segments[j].text.strip()):
                        last_boundary = j
                if last_boundary > i:
                    end = last_boundary + 1
            batches.append(segments[i:end])
            i = end
        return batches

    @staticmethod
    def _word_count(text: str) -> int:
        return len(re.findall(r"[A-Za-z0-9']+", text))

    @classmethod
    def _close_sentence(cls, part: str) -> str:
        """大写边界即句界：补回 ASR 丢掉的句末点，防止下游按标点合并时重新粘连。"""
        return part if cls._SENTENCE_TERMINAL_RE.search(part) else part + "."

    @classmethod
    def _split_capital_boundaries(cls, text: str) -> list[str]:
        """按句中大写词切无标点粘连句；专名连用不切，功能词大写必切。"""
        result: list[str] = []
        start = 0
        for boundary in cls._CAPITAL_BOUNDARY_RE.finditer(text):
            candidate = text[start:boundary.start()].strip()
            if cls._word_count(candidate) < cls._CAPITAL_SPLIT_MIN_WORDS:
                continue
            words_before = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text[:boundary.start()])
            words_after = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text[boundary.end():])
            current = words_after[0] if words_after else ""
            previous = words_before[-1] if words_before else ""
            following = words_after[1] if len(words_after) > 1 else ""
            # 大写词是全文最后一个词（"…brand Oatly."）时不构成句界——
            # 句子不可能从最后一个词开始；也保证本规则幂等（二次应用不再切）。
            if len(words_after) < 2:
                continue
            if current in cls._CAPITAL_SPLIT_EXCLUDED_TOKENS:
                continue
            if current in cls._CAPITAL_SPLIT_FUNCTION_WORDS:
                result.append(cls._close_sentence(candidate))
                start = boundary.end()
                continue
            # New York / River Thames / United Kingdom 等连续大写专名不构成句界。
            if previous[:1].isupper() or following[:1].isupper():
                continue
            # the Swedish brand / a British firm：冠词后是形容词性专名，不构成句界。
            if previous.lower() in cls._CAPITAL_SPLIT_ARTICLES:
                continue
            result.append(cls._close_sentence(candidate))
            start = boundary.end()
        tail = text[start:].strip()
        if tail:
            result.append(tail)
        return result

    @classmethod
    def _split_text_sentences(cls, text: str) -> list[str]:
        """Split obvious multi-sentence text while preserving existing wording."""
        cleaned = re.sub(r"\s+", " ", text).strip()
        if not cleaned:
            return []
        pieces = [m.group(0).strip() for m in cls._SENTENCE_SPAN.finditer(cleaned)]
        pieces = [p for p in pieces if p]
        out: list[str] = []
        for piece in pieces or [cleaned]:
            out.extend(cls._split_overlong_sentence(piece))
        return cls._merge_comma_continuations(out)

    @classmethod
    def _merge_comma_continuations(cls, pieces: list[str]) -> list[str]:
        merged: list[str] = []
        i = 0
        while i < len(pieces):
            current = pieces[i]
            if i + 1 < len(pieces) and current.rstrip().endswith(","):
                combined = f"{current} {pieces[i + 1]}".strip()
                if cls._word_count(combined) <= cls._MAX_COMMA_CONTINUATION_WORDS:
                    merged.append(combined)
                    i += 2
                    continue
            merged.append(current)
            i += 1
        return merged

    @classmethod
    def _split_overlong_sentence(cls, text: str) -> list[str]:
        words = text.split()
        if len(words) <= cls._HARD_MAX_SENTENCE_WORDS:
            return [text]

        comma_parts = re.split(r"(?<=,)\s+", text)
        if len(comma_parts) > 1 and all(len(part.split()) <= cls._HARD_MAX_SENTENCE_WORDS for part in comma_parts):
            chunks: list[str] = []
            current: list[str] = []
            current_words = 0
            for part in comma_parts:
                part_words = len(part.split())
                if current and current_words + part_words > cls._HARD_MAX_SENTENCE_WORDS:
                    chunks.append(" ".join(current).strip())
                    current = [part]
                    current_words = part_words
                else:
                    current.append(part)
                    current_words += part_words
            if current:
                chunks.append(" ".join(current).strip())
            if all(chunk for chunk in chunks):
                return chunks

        return [
            " ".join(words[i:i + cls._HARD_MAX_SENTENCE_WORDS]).strip()
            for i in range(0, len(words), cls._HARD_MAX_SENTENCE_WORDS)
        ]

    @classmethod
    def _split_segment_sentences(cls, segment: Segment, contributors: list[Segment] | None = None) -> list[Segment]:
        pieces = cls._split_text_sentences(segment.text)
        if len(pieces) <= 1:
            return [segment]

        if contributors and len(contributors) == len(pieces):
            return [
                Segment(
                    index=0,
                    text=piece,
                    start=source.start,
                    end=source.end,
                    translation=segment.translation,
                )
                for piece, source in zip(pieces, contributors)
            ]

        start = segment.start
        end = segment.end
        if start is None or end is None or end <= start:
            return [
                Segment(index=0, text=piece, start=start, end=end, translation=segment.translation)
                for piece in pieces
            ]

        total_chars = sum(max(1, len(piece)) for piece in pieces)
        cursor = start
        out: list[Segment] = []
        duration = end - start
        for pos, piece in enumerate(pieces):
            if pos == len(pieces) - 1:
                piece_end = end
            else:
                piece_end = cursor + duration * (max(1, len(piece)) / total_chars)
            out.append(Segment(index=0, text=piece, start=cursor, end=piece_end, translation=segment.translation))
            cursor = piece_end
        return out

    def _group_cues(self, segments: list[Segment], boundary_ids: set[int]) -> list[Segment]:
        """Rebuild sentences from cue boundaries while preserving source timestamps."""
        result: list[Segment] = []
        buf: list[Segment] = []

        def flush(count: int | None = None) -> None:
            nonlocal buf
            if not buf:
                return
            selected = buf if count is None else buf[:count]
            buf = [] if count is None else buf[count:]
            text = " ".join(s.text.strip() for s in selected if s.text.strip()).strip()
            if not text:
                return
            starts = [s.start for s in selected if s.start is not None]
            ends = [s.end for s in selected if s.end is not None]
            merged = Segment(
                index=0,
                text=text,
                start=min(starts) if starts else None,
                end=max(ends) if ends else None,
                translation="",
            )
            result.extend(self._split_segment_sentences(merged, selected))

        for seg in segments:
            buf.append(seg)
            if seg.index in boundary_ids or self._SENT_END.search(seg.text.strip()):
                flush()
            elif self._word_count(" ".join(s.text for s in buf)) >= self._HARD_MAX_SENTENCE_WORDS:
                flush(self._best_soft_split(buf))

        flush()
        for i, seg in enumerate(result, 1):
            seg.index = i
        return result

    @staticmethod
    def _pause_after_ms(segments: list[Segment], pos: int) -> int | None:
        if pos + 1 >= len(segments):
            return None
        current, following = segments[pos], segments[pos + 1]
        if current.end is None or following.start is None:
            return None
        return round(max(0.0, following.start - current.end) * 1000)

    def _forbidden_boundary_after(self, token: Segment) -> bool:
        word = token.text.lower().strip(".,?!;:\"'()[]{}")
        return word in self._FORBIDDEN_BOUNDARY_AFTER

    def _best_soft_split(self, segments: list[Segment]) -> int:
        """Choose a cue boundary for the abnormal overlong fallback."""
        if len(segments) <= 1:
            return len(segments)
        candidates: list[tuple[int, int, int]] = []
        prefix_words = 0
        for pos in range(len(segments) - 1):
            prefix_words += self._word_count(segments[pos].text)
            if self._forbidden_boundary_after(segments[pos]):
                continue
            pause = self._pause_after_ms(segments, pos) or 0
            candidates.append((pause, -abs(prefix_words - 30), pos + 1))
        return max(candidates)[2] if candidates else len(segments)

    def _local_merge_segments(self, segments: list[Segment]) -> list[Segment]:
        """Fallback segmentation: reliable punctuation plus an abnormal-length cap."""
        return self._group_cues(segments, set())

    def _token_windows(self, segments: list[Segment]) -> list[tuple[list[Segment], set[int]]]:
        """Build core windows with context overlap; only core boundary decisions are committed."""
        windows: list[tuple[list[Segment], set[int]]] = []
        i = 0
        while i < len(segments):
            core_start = i
            core_words = 0
            while i < len(segments) and core_words < self._AI_WINDOW_WORDS:
                core_words += self._word_count(segments[i].text)
                i += 1
            core_end = i
            left = core_start
            left_words = 0
            while left > 0 and left_words < self._AI_CONTEXT_WORDS:
                left -= 1
                left_words += self._word_count(segments[left].text)
            right = core_end
            right_words = 0
            while right < len(segments) and right_words < self._AI_CONTEXT_WORDS:
                right_words += self._word_count(segments[right].text)
                right += 1
            windows.append((segments[left:right], {seg.index for seg in segments[core_start:core_end]}))
        return windows

    def _timed_tokens(self, cues: list[Segment]) -> list[Segment]:
        """Expand cue text into tokens and interpolate cue-local timestamps."""
        tokens: list[Segment] = []
        for cue in cues:
            words = cue.text.split()
            for pos, word in enumerate(words):
                if cue.start is None or cue.end is None or cue.end <= cue.start:
                    token_start, token_end = cue.start, cue.end
                else:
                    duration = cue.end - cue.start
                    token_start = cue.start + duration * pos / len(words)
                    token_end = cue.start + duration * (pos + 1) / len(words)
                tokens.append(Segment(
                    index=len(tokens) + 1,
                    text=word,
                    start=token_start,
                    end=token_end,
                    translation="",
                ))
        return tokens

    def _review_window_with_ai(
        self, batch: list[Segment], core_ids: set[int], bn: int, total: int
    ) -> set[int]:
        payload = {
            "transcript": " ".join(seg.text for seg in batch),
            "tokens": [{"token_id": seg.index, "text": seg.text} for seg in batch],
            "pauses": [
                {"after_token_id": seg.index, "pause_ms": pause}
                for pos, seg in enumerate(batch)
                if (pause := self._pause_after_ms(batch, pos)) is not None and pause > 0
            ],
        }
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                temperature=0.1,
                max_tokens=1600,
                messages=[
                    {"role": "system", "content": MERGE_ONLY_PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
                timeout=60,
            )
            raw = json.loads(resp.choices[0].message.content)
            values = raw.get("break_after_token_ids", [])
            if not isinstance(values, list):
                raise ValueError("break_after_token_ids must be a list")
            valid_ids = {seg.index for seg in batch}
            reviewed = {int(value) for value in values if int(value) in valid_ids and int(value) in core_ids}
            print(f"[analyzer] AI token 边界窗口 {bn}/{total} 完成")
            return reviewed
        except Exception as e:
            print(f"[analyzer] AI token 边界窗口 {bn}/{total} 失败（{e}），使用本地兜底")
            return set()

    def _sanitize_ai_boundaries(self, tokens: list[Segment], boundaries: set[int]) -> set[int]:
        """Reject high-confidence dangling boundaries after the AI audit."""
        accepted: set[int] = set()
        previous = 0
        for token_id in sorted(boundaries):
            if not 1 <= token_id <= len(tokens):
                continue
            token = tokens[token_id - 1]
            pause = self._pause_after_ms(tokens, token_id - 1) or 0
            span_words = token_id - previous
            word = token.text.lower().strip(".,?!;:\"'()[]{}")
            if word in self._MOVE_BOUNDARY_BEFORE and token_id - 1 > previous:
                token_id -= 1
                token = tokens[token_id - 1]
                pause = self._pause_after_ms(tokens, token_id - 1) or 0
                span_words = token_id - previous
                word = token.text.lower().strip(".,?!;:\"'()[]{}")
            if self._forbidden_boundary_after(token) and not self._SENT_END.search(token.text.strip()):
                continue
            if span_words < 8 and not self._SENT_END.search(token.text.strip()) and pause < 1200:
                continue
            accepted.add(token_id)
            previous = token_id
        return accepted

    def _merge_raw_segments(self, segments: list[Segment]) -> list[Segment]:
        """Step 1: ask AI for cue boundaries, then rebuild text and timestamps locally."""
        cues = [
            Segment(index=i, text=seg.text, start=seg.start, end=seg.end, translation="")
            for i, seg in enumerate(segments, 1)
            if seg.text.strip()
        ]
        if not self._ENABLE_AI_BOUNDARY_REVIEW:
            result = self._local_merge_segments(cues)
            print(f"[analyzer] Step 1 本地兜底：{len(cues)} cues → {len(result)} 句（AI边界审核关闭）")
            return result

        tokens = self._timed_tokens(cues)
        windows = self._token_windows(tokens)
        total = len(windows)
        protected = {
            seg.index
            for seg in tokens
            if self._SENT_END.search(seg.text.strip())
        }
        boundaries = set(protected)
        with ThreadPoolExecutor(max_workers=min(total, 8) or 1) as pool:
            futures = [
                pool.submit(self._review_window_with_ai, batch, core_ids, bn, total)
                for bn, (batch, core_ids) in enumerate(windows, 1)
            ]
        for future in futures:
            boundaries.update(future.result())
        boundaries = self._sanitize_ai_boundaries(tokens, boundaries)
        result = self._group_cues(tokens, boundaries)
        print(f"[analyzer] Step 1 AI token 断句：{len(cues)} cues → {len(result)} 句")
        return result

    @staticmethod
    def _word_tokens(text: str) -> list[str]:
        return re.findall(r"[A-Za-z0-9']+", text.lower())

    @staticmethod
    def _normalize_for_dedupe(text: str) -> str:
        return " ".join(SentenceAnalyzer._word_tokens(text))

    @staticmethod
    def _similarity(left: str, right: str) -> float:
        left_tokens = SentenceAnalyzer._word_tokens(left)
        right_tokens = SentenceAnalyzer._word_tokens(right)
        if not left_tokens or not right_tokens:
            return 0.0
        left_set = set(left_tokens)
        right_set = set(right_tokens)
        return len(left_set & right_set) / max(len(left_set), len(right_set))

    @staticmethod
    def _untokenize(words: list[str], original: str) -> str:
        suffix = ""
        match = re.search(r'([.?!;,]["\')\]]*)\s*$', original.strip())
        if match:
            suffix = match.group(1)
        text = " ".join(words).strip()
        if suffix and not text.endswith(suffix):
            text += suffix
        return text

    @classmethod
    def _compress_repeated_ngrams(cls, text: str) -> str:
        return cls._compress_repeated_ngrams_once(cls._compress_repeated_ngrams_once(text))

    @classmethod
    def _compress_repeated_ngrams_once(cls, text: str) -> str:
        words = re.findall(r"[A-Za-z0-9']+|[^\w\s]", text)
        plain = [w for w in words if re.match(r"[A-Za-z0-9']+$", w)]
        if len(plain) < 6:
            return text

        lowered = [w.lower() for w in plain]
        out: list[str] = []
        i = 0
        changed = False
        while i < len(plain):
            compressed = False
            for n in range(10, 1, -1):
                if i + 2 * n > len(plain):
                    continue
                first = lowered[i:i + n]
                repeats = 1
                j = i + n
                while j + n <= len(plain) and lowered[j:j + n] == first:
                    repeats += 1
                    j += n
                if repeats >= 2:
                    out.extend(plain[i:i + n])
                    i = j
                    changed = True
                    compressed = True
                    break
            if not compressed:
                out.append(plain[i])
                i += 1

        return cls._untokenize(out, text) if changed else text

    @classmethod
    def _trim_overlap_prefix(cls, previous: str, current: str) -> str:
        prev_l = cls._word_tokens(previous)
        cur_l = cls._word_tokens(current)
        if len(prev_l) < 3 or len(cur_l) < 3:
            return current
        cur_orig = re.findall(r"[A-Za-z0-9']+", current)
        max_n = min(10, len(prev_l), len(cur_l))
        for n in range(max_n, 2, -1):
            if prev_l[-n:] == cur_l[:n]:
                rest = cur_orig[n:]
                if not rest:
                    return ""
                return cls._untokenize(rest, current)
        return current

    def _denoise_repeated_sentence_text(self, segments: list[Segment]) -> list[Segment]:
        """Conservatively remove ASR repetition without changing audio time coverage."""
        cleaned: list[Segment] = []
        for seg in segments:
            text = self._compress_repeated_ngrams(seg.text)
            candidate = Segment(index=0, text=text, start=seg.start, end=seg.end, translation=seg.translation)

            if cleaned:
                prev = cleaned[-1]
                same = self._normalize_for_dedupe(prev.text) == self._normalize_for_dedupe(candidate.text)
                very_similar = self._similarity(prev.text, candidate.text) >= 0.92
                if same or (very_similar and self._word_count(candidate.text) >= 4):
                    prev.end = candidate.end if candidate.end is not None else prev.end
                    continue

                trimmed = self._trim_overlap_prefix(prev.text, candidate.text)
                if trimmed != candidate.text:
                    if not trimmed:
                        prev.end = candidate.end if candidate.end is not None else prev.end
                        continue
                    candidate.text = trimmed

            cleaned.append(candidate)

        for i, seg in enumerate(cleaned, 1):
            seg.index = i
        return cleaned

    def analyze_from_raw_segments(self, segments: list[Segment], lesson_title: str, trace_dir=None) -> tuple[list[Segment], list[SentenceAnalysis]]:
        """主路径：
        Step 1 — 本地断句 + 重复去噪
        Step 2 — IPA 专项标注
        Step 3 — 并发翻译分析，每批 20 句，最多 4 个并发线程

        返回：(合并后的 Segment 列表, 与之一一对应的 SentenceAnalysis 列表)
        """
        if self.mode == "mock" or not self._has_key:
            fallbacks = [self._fallback(seg.text) for seg in segments]
            if self._has_key is False:
                _enrich_free_translations(fallbacks)
            return segments, fallbacks

        # ── Step 1：合并 ──────────────────────────────────────────────
        # 将 Whisper ASR 切出的碎片句合并成完整句子，为 Step 2 提供干净输入
        merged_segments = self._merge_raw_segments(segments)
        _save_trace(trace_dir, "step1_merge.json", [
            {"index": s.index, "text": s.text, "start": s.start, "end": s.end}
            for s in merged_segments
        ])
        merged_segments = self._denoise_repeated_sentence_text(merged_segments)
        _save_trace(trace_dir, "step1_denoised.json", [
            {"index": s.index, "text": s.text, "start": s.start, "end": s.end}
            for s in merged_segments
        ])

        # ── Step 2：IPA 专项标注 ───────────────────────────────────────
        print("[STEP:ipa_annotate]")
        texts = [seg.text for seg in merged_segments]
        ipa_map = self._refine_ipa_pass(texts) if self._ENABLE_AI_IPA_REFINEMENT else {}
        if not self._ENABLE_AI_IPA_REFINEMENT:
            print("[analyzer] Step 2 IPA专项：AI覆盖关闭，使用本地规则音标")
        precomputed_ipa_by_index: dict[int, str] = {}
        for i, ipa in ipa_map.items():
            if not (0 <= i < len(merged_segments)):
                continue
            seg = merged_segments[i]
            fallback_ipa = _to_rule_natural_ipa(seg.text)
            cleaned_ipa = self._clean_natural_ipa(ipa, fallback_ipa, seg.text)
            if cleaned_ipa != fallback_ipa:
                precomputed_ipa_by_index[seg.index] = cleaned_ipa
        ipa_trace = [
            {
                "index": i + 1,
                "text": seg.text,
                "canonical_ipa": _to_canonical_ipa(seg.text),
                "natural_ipa": precomputed_ipa_by_index.get(seg.index) or _to_rule_natural_ipa(seg.text),
                "source": "ai_ipa" if seg.index in precomputed_ipa_by_index else "rule",
            }
            for i, seg in enumerate(merged_segments)
        ]
        _save_trace(trace_dir, "step2_ipa.json", ipa_trace)
        # 兼容旧脚本/人工查看路径；语义上现在它是 Step2 的输出。
        _save_trace(trace_dir, "step3_ipa.json", ipa_trace)

        # ── Step 3：深度分析 ──────────────────────────────────────────
        # 对每批 20 句并发调用 NATURAL_COMBINED_ANALYSIS_PROMPT，只返回译文；IPA 使用 Step2 结果
        BATCH_SIZE = 20
        MAX_WORKERS = 4
        batches = [(i, merged_segments[i:i + BATCH_SIZE]) for i in range(0, len(merged_segments), BATCH_SIZE)]
        total = len(batches)
        batch_segments: dict[int, list[Segment]] = {}
        batch_analyses: dict[int, list[SentenceAnalysis]] = {}

        print("[STEP:analyze]")
        print(f"[analyzer] Step 3 分析：{len(merged_segments)} 句 → {total} 批（{BATCH_SIZE}/批，{MAX_WORKERS} 并发）…")

        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, total)) as executor:
            futures = {
                executor.submit(self._analyze_combined_batch, batch, lesson_title, precomputed_ipa_by_index): (idx, batch)
                for idx, batch in batches
            }
            done_count = 0
            for future in as_completed(futures):
                idx, failed_batch = futures[future]
                done_count += 1
                try:
                    segs, analyses = future.result()
                    batch_segments[idx] = segs
                    batch_analyses[idx] = analyses
                except Exception as e:
                    print(f"[analyzer] batch {idx} failed ({e}), using fallback for {len(failed_batch)} segments")
                    batch_segments[idx] = [
                        Segment(index=0, text=seg.text, start=seg.start, end=seg.end, translation="")
                        for seg in failed_batch
                    ]
                    fallback_analyses = [self._fallback(seg.text) for seg in failed_batch]
                    for seg, analysis in zip(failed_batch, fallback_analyses):
                        ipa = precomputed_ipa_by_index.get(seg.index)
                        if ipa:
                            analysis.phonetics = ipa
                            analysis.phonetics_natural = ipa
                            analysis.phonetics_source = "ai_ipa"
                    batch_analyses[idx] = fallback_analyses
                print(f"[analyzer] {done_count}/{total} batches done")

        # 按批次原始顺序拼回，重新从 1 连续编号
        merged: list[Segment] = []
        analyses: list[SentenceAnalysis] = []
        for idx in sorted(batch_segments):
            merged.extend(batch_segments[idx])
            analyses.extend(batch_analyses[idx])

        for i, seg in enumerate(merged, 1):
            seg.index = i

        print(f"[analyzer] 完成：{len(segments)} → {len(merged)} 句（断句+IPA+翻译分析）")
        analysis_trace = [
            {
                "index": i + 1,
                "text": a.text,
                "translation": a.translation,
                "phonetics": a.phonetics,
                "speech_features": a.speech_features,
                "difficulty": a.difficulty,
            }
            for i, a in enumerate(analyses)
        ]
        _save_trace(trace_dir, "step3_analysis.json", analysis_trace)
        # 兼容旧脚本/人工查看路径；语义上现在它是 Step3 的输出。
        _save_trace(trace_dir, "step2_analysis.json", analysis_trace)

        return merged, analyses

    def _analyze_combined_batch(
        self,
        batch: list[Segment],
        lesson_title: str,
        precomputed_ipa_by_index: dict[int, str] | None = None,
    ) -> tuple[list[Segment], list[SentenceAnalysis]]:
        """单批次处理（NATURAL_COMBINED_ANALYSIS_PROMPT 路径）：
        输入：20 句已合并的 Segment（含 index/text/start/end）
        输出：(合并后 Segment 列表, SentenceAnalysis 列表)

        AI 返回字段：text / indices / translation
        注意：复杂分析字段保持 SentenceAnalysis 默认空值，后续按需点击生成。
        """

        payload = [
            {
                "index": s.index,
                "text": s.text,
                "precomputed_natural_ipa": (precomputed_ipa_by_index or {}).get(s.index, ""),
            }
            for s in batch
        ]
        user_prompt = (
            f"Lesson: {lesson_title}\n"
            f"Segments with local IPA candidates:\n{json.dumps(payload, ensure_ascii=False)}"
        )
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0.2,
            max_tokens=8000,
            messages=[
                {"role": "system", "content": NATURAL_COMBINED_ANALYSIS_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "disabled"}},
            timeout=120,
        )
        raw = response.choices[0].message.content.strip()
        raw = self._extract_json(raw)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # AI 偶尔返回非法 JSON：把错误内容发回去让它自我修复，再解析一次
            repair_prompt = (
                "Your previous response was not valid JSON. "
                "Return ONLY the corrected JSON object, no explanation:\n"
                + raw[:3000]
            )
            repair_resp = self.client.chat.completions.create(
                model=self.model,
                temperature=0,
                max_tokens=8000,
                messages=[
                    {"role": "system", "content": NATURAL_COMBINED_ANALYSIS_PROMPT},
                    {"role": "user", "content": repair_prompt},
                ],
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
                timeout=120,
            )
            repaired = repair_resp.choices[0].message.content.strip()
            repaired = self._extract_json(repaired)
            try:
                data = json.loads(repaired)
            except json.JSONDecodeError:
                # 修复也失败，该批退回规则兜底
                print(f"[analyzer] combined batch JSON repair failed, falling back for {len(batch)} segments")
                segments_out = [
                    Segment(index=0, text=seg.text, start=seg.start, end=seg.end, translation="")
                    for seg in batch
                ]
                return segments_out, [self._fallback(seg.text) for seg in batch]

        # AI 返回的分析列表，兼容偶尔用不同键名的情况。Step 2 的契约是“只分析”，
        # 不允许再改变 Step 1 已确定的句子集合；否则会出现丢句/重排。
        items = data.get("sentences") or next(
            (v for v in data.values() if isinstance(v, list)), []
        )

        item_by_index: dict[int, dict] = {}
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                indices = item.get("indices")
                if isinstance(indices, list) and len(indices) == 1:
                    try:
                        idx = int(indices[0])
                    except (TypeError, ValueError):
                        continue
                    item_by_index[idx] = item
                elif item.get("index") is not None:
                    try:
                        idx = int(item.get("index"))
                    except (TypeError, ValueError):
                        continue
                    item_by_index[idx] = item

        if not item_by_index and isinstance(items, list) and len(items) == len(batch):
            item_by_index = {
                seg.index: item
                for seg, item in zip(batch, items)
                if isinstance(item, dict)
            }

        segments_out: list[Segment] = []
        analyses_out: list[SentenceAnalysis] = []

        for seg in batch:
            item = item_by_index.get(seg.index, {})
            text = seg.text
            translation = item.get("translation") or ""

            segments_out.append(Segment(
                index=0, text=text, start=seg.start, end=seg.end, translation=translation,
            ))

            # IPA：先算规则层候选（作为 fallback），再用 AI 返回的做校正
            # 注意：text 来自 AI 返回值，可能与原始 s.text 不同（AI 可能修正标点/合并），
            # 因此不能复用 payload 构建阶段的 IPA 计算结果，需在此重新计算
            canonical_ipa = _to_canonical_ipa(text)
            rule_natural_ipa = _to_rule_natural_ipa(text)
            precomputed_ipa = (precomputed_ipa_by_index or {}).get(seg.index, "")
            natural_ipa = precomputed_ipa or self._clean_natural_ipa(item.get("natural_ipa"), rule_natural_ipa, text)
            phonetics_source = "ai_ipa" if precomputed_ipa else ("ai_text" if natural_ipa != rule_natural_ipa else "rule")

            analyses_out.append(SentenceAnalysis(
                text=text,
                phonetics=natural_ipa,
                phonetics_canonical=canonical_ipa,
                phonetics_natural=natural_ipa,
                phonetics_source=phonetics_source,
                translation=translation,
            ))

        return segments_out, analyses_out


    @staticmethod
    def _clean_natural_ipa(value: object, fallback: str, text: str = "") -> str:
        natural = str(value or "").strip()
        if not natural:
            return fallback
        # AI 返回了 JSON 片段或超长内容时用规则兜底
        if "{" in natural or "}" in natural or len(natural) > max(240, len(fallback) * 3):
            return fallback
        if natural == fallback:
            return fallback
        can_fix_known_gap = "*" in fallback or bool(re.search(r"\d", fallback)) or bool(re.search(r"\d", text))
        if not can_fix_known_gap:
            return fallback
        if "*" in natural:
            return fallback
        if natural.count("/") != fallback.count("/"):
            return fallback
        if natural.count("↗") != fallback.count("↗") or natural.count("↘") != fallback.count("↘"):
            return fallback
        if any(bad in natural for bad in ("(h)æv", "(h)æz", "(h)əd", "pɔrd", "keɪdəd")):
            return fallback
        return natural

    @staticmethod
    def _clean_confidence(value: object) -> float | None:
        if value is None or value == "":
            return None
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            return None
        if confidence != confidence:
            return None
        return max(0.0, min(1.0, confidence))

    @staticmethod
    def _clean_difficulty(value: object) -> str:
        # NATURAL_COMBINED_ANALYSIS_PROMPT 返回中文难度（低/中/高），映射到 CEFR 供前端使用
        _ZH_TO_CEFR = {"低": "A2", "中": "B1", "高": "B2"}
        raw = str(value or "").strip()
        return _ZH_TO_CEFR.get(raw, "B1")

    @staticmethod
    def _clean_speech_features(value: object) -> list[str]:
        # 校验 combined prompt 返回的语流特征标签，只保留受控词表内的值，最多 3 个
        _ALLOWED = {"连读", "吞音", "弱读", "弱读链", "省略", "重读"}
        if not isinstance(value, list):
            return []
        return [s for s in value if isinstance(s, str) and s.strip() in _ALLOWED][:3]

    _EXPRESSION_FUNCTION_VOCAB = {"提出观点", "举例说明", "转折让步", "强调递进", "因果解释", "提出建议", "总结收尾"}
    _TOPIC_TAG_VOCAB = {"爱好兴趣", "职业工作", "家庭关系", "健康生活", "学习成长", "科技数字", "社会文化", "旅行体验"}

    @classmethod
    def _clean_expression_function(cls, raw: object) -> str:
        v = str(raw).strip() if raw else ""
        return v if v in cls._EXPRESSION_FUNCTION_VOCAB else ""

    @classmethod
    def _clean_topic_tag(cls, raw: object) -> str:
        v = str(raw).strip() if raw else ""
        return v if v in cls._TOPIC_TAG_VOCAB else ""



    @staticmethod
    def _extract_json(raw: str) -> str:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        return raw

    def _refine_ipa_pass(self, texts: list[str]) -> dict[int, str]:
        """Step 3：IPA 专项标注（10 句/批，4 并发，WITH_BASE），返回 index→natural_ipa 映射。"""
        from prompts import IPA_ANNOTATION_PROMPT_WITH_BASE
        IPA_BATCH = 10
        IPA_WORKERS = 4
        results: dict[int, str] = {}
        lock = __import__("threading").Lock()

        batches = [(bs, texts[bs: bs + IPA_BATCH]) for bs in range(0, len(texts), IPA_BATCH)]
        total_batches = len(batches)
        print(f"[analyzer] Step 3 IPA专项：{len(texts)} 句 → {total_batches} 批（{IPA_BATCH}/批，{IPA_WORKERS} 并发）…")

        done_count = 0

        def _run_batch(batch_start: int, batch: list[str]) -> None:
            nonlocal done_count
            payload = [
                {
                    "index": batch_start + j + 1,
                    "text": text,
                    "canonical_ipa": _to_canonical_ipa(text),
                }
                for j, text in enumerate(batch)
            ]
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    temperature=0.2,
                    max_tokens=1500,
                    messages=[
                        {"role": "system", "content": IPA_ANNOTATION_PROMPT_WITH_BASE},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    response_format={"type": "json_object"},
                    extra_body={"thinking": {"type": "disabled"}},
                    timeout=60,
                )
                raw_content = (resp.choices[0].message.content or "").strip()
                if not raw_content:
                    raise ValueError("API 返回空响应")
                items = json.loads(raw_content).get("results", [])
                with lock:
                    for item in items:
                        idx = item.get("index", 0) - 1
                        nat = item.get("natural_ipa", "")
                        if 0 <= idx < len(texts) and nat:
                            fallback = _to_rule_natural_ipa(texts[idx])
                            cleaned = self._clean_natural_ipa(nat, fallback, texts[idx])
                            if cleaned != fallback:
                                results[idx] = cleaned
                    done_count += 1
                    print(f"[analyzer] IPA批次 {done_count}/{total_batches} 完成")
            except Exception as e:
                with lock:
                    done_count += 1
                print(f"[analyzer] IPA批次 {batch_start} 失败：{e}")

        with ThreadPoolExecutor(max_workers=IPA_WORKERS) as executor:
            for bs, batch in batches:
                executor.submit(_run_batch, bs, batch)

        return results

    def _fallback(self, text: str) -> SentenceAnalysis:
        """AI 调用失败时的规则兜底：用本地 IPA 和简单词汇提取填充 SentenceAnalysis。"""
        words = re.findall(r"[A-Za-z']+", text)
        useful = []
        for word in words:
            normalized = word.lower()
            if len(normalized) < 6:
                continue
            if normalized not in useful:
                useful.append(normalized)
            if len(useful) == 3:
                break

        vocabulary = [
            VocabularyItem(word=word, ipa=_to_canonical_ipa(word), meaning="建议结合上下文理解", example="")
            for word in useful
        ]
        connected = []
        for left, right in zip(words, words[1:]):
            connected.append(f"{left.lower()}_{right.lower()}")
            if len(connected) == 3:
                break

        template = self._guess_pattern(text)
        canonical_ipa = _to_canonical_ipa(text)
        rule_natural_ipa = _to_rule_natural_ipa(text)
        return SentenceAnalysis(
            text=text,
            phonetics=rule_natural_ipa,
            phonetics_canonical=canonical_ipa,
            phonetics_natural=rule_natural_ipa,
            phonetics_source="rule",
            vocabulary=vocabulary,
            pattern=PatternItem(
                template=template,
                usage="用于描述句子的基本结构，当前为本地兜底分析。",
            ),
            connected_speech=connected,
            difficulty=self._guess_difficulty(text),
        )

    @staticmethod
    def _guess_pattern(text: str) -> str:
        lowered = text.lower()
        if " as " in lowered:
            return text.replace(" as ", " as ... ", 1)
        if " because " in lowered:
            return text.replace(" because ", " because ... ", 1)
        words = re.findall(r"[A-Za-z']+", text)
        if len(words) >= 4:
            return " ".join(words[:4]) + " ..."
        return text

    @staticmethod
    def _guess_difficulty(text: str) -> str:
        word_count = len(re.findall(r"[A-Za-z']+", text))
        if word_count <= 6:
            return "A2"
        if word_count <= 12:
            return "B1"
        if word_count <= 20:
            return "B2"
        return "C1"

    @staticmethod
    def to_json_ready(items: list[SentenceAnalysis]) -> list[dict]:
        return [asdict(item) for item in items]
