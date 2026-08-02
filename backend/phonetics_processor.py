# -*- coding: utf-8 -*-
"""
Post-process eng-to-ipa output to annotate connected speech features:
  - Linking ‿  for high-value teaching candidates
  - Elision (t)/(d)/(h) for dropped stops/fricatives
  - Thought group markers /  at clause boundaries
  - Intonation ↗/↘ per thought group
"""

from __future__ import annotations
import re

# ── IPA symbol sets ─────────────────────────────────────────────────────────
CONSONANTS = set("bdfghjklmnŋprsʃtθðvwzʒʤʧɾ")
VOWELS     = set("æɑɒɔəɛɜɪiʊuʌeɐɚɝ")

# Function words whose /h/ can be elided in connected speech.
# 功能词 /h/ 弱读/吞音（作助动词时 h 常省略，如 "I have been" → "I (h)ave been"）
H_ELISION_WORDS = {'he', 'him', 'his', 'her'}
EMPHASIS_CUES = {'not', 'never', 'only', 'even', 'also', 'too'}

# 快速语流中常见吞音候选词（仅文本推断提示，非对实际发音的结论）
T_ELISION_WORDS = {
    # 否定缩略（-n't 结尾）
    "can't", "won't", "don't", "isn't", "wasn't", "aren't", "weren't",
    "didn't", "couldn't", "wouldn't", "shouldn't", "haven't", "hasn't",
    "hadn't", "needn't", "mustn't",
    # -st 结尾（辅音丛末位 t）
    'just', 'must', 'last', 'first', 'best', 'worst', 'next',
    'most', 'past', 'fast', 'rest', 'test', 'post', 'cost', 'lost',
    'least', 'almost', 'against', 'chest', 'host', 'trust', 'dust',
    # -nt 结尾
    'want', 'went', 'front', 'point', 'count', 'amount', 'moment',
    'recent', 'student', 'current', 'important', 'different', 'present',
    'content', 'parent', 'accent', 'percent', 'talent', 'ulent',
    # -ght 结尾（gh 不发音，词尾实为 /t/）
    'night', 'right', 'light', 'might', 'sight', 'fight', 'bright',
    'slight', 'tight', 'height', 'weight', 'straight', 'thought',
    # 高频功能词（辅音前常吞 t）
    'that', 'not', 'what', 'but', 'get', 'let', 'put', 'got',
    'out', 'about', 'without',
}
D_ELISION_WORDS = {
    # 连词
    'and',
    # -ld 结尾
    'old', 'cold', 'bold', 'hold', 'told', 'fold', 'gold', 'sold',
    'world', 'child', 'wild', 'mild', 'field', 'build',
    # -nd 结尾
    'hand', 'mind', 'find', 'kind', 'stand', 'land', 'grand', 'brand',
    'friend', 'end', 'found', 'sound', 'round', 'ground', 'around',
    'behind', 'second', 'beyond', 'spend', 'send', 'blend',
    # 情态动词（d 常弱化）
    'would', 'could', 'should',
    # 其他高频
    'good', 'food', 'hard', 'word', 'bad', 'said', 'used',
}
TD_BLOCK_NEXT_INITIALS = set('hyw r'.replace(' ', ''))
BOUNDARY_PUNCT = '.,;:!?'
QUOTE_CHARS = "\"'“”‘’"
EN_VOWELS = set('aeiou')
EN_CONSONANTS = set('bcdfghjklmnpqrstvwxyz')

# Short function words that often reduce or attach to neighboring words in
# classroom connected-speech examples.
LINKING_FUNCTION_WORDS = {
    'of', 'a', 'an', 'the', 'to', 'for', 'in', 'on', 'as', 'and', 'or',
}

# Common spoken chunks worth marking even when the surrounding words are not
# both function words.
COMMON_LINKING_BIGRAMS = {
    ('kind', 'of'),
    ('sort', 'of'),
    ('lot', 'of'),
    ('lots', 'of'),
    ('going', 'to'),
    ('want', 'to'),
    ('wants', 'to'),
    ('wanted', 'to'),
    ('have', 'to'),
    ('has', 'to'),
    ('had', 'to'),
    ('got', 'to'),
    ('used', 'to'),
    ('need', 'to'),
    ('needs', 'to'),
    ('trying', 'to'),
    ('try', 'to'),
    ('take', 'a'),
    ('read', 'a'),
    ('make', 'it'),
    ('apply', 'it'),
}

# Local guardrails for frequent eng-to-ipa misses observed in lessons.
WORD_PRONUNCIATION_OVERRIDES = {
    'reading': 'ˈriːdɪŋ',
    'slightly': 'ˈslaɪtli',
}

# Subordinating conjunctions → insert '/' BEFORE them
SUB_CONJ = {
    'when', 'if', 'because', 'although', 'while', 'since', 'after',
    'before', 'unless', 'though', 'until', 'whereas', 'whenever',
    'wherever', 'whoever', 'whatever', 'whichever', 'however',
}
# Relative pronouns that introduce embedded clauses
RELATIVE   = {'which', 'who', 'whom', 'whose'}
# Wh-words → falling intonation on questions
WH_WORDS   = {'what', 'where', 'when', 'who', 'whom', 'whose', 'which', 'why', 'how'}
READ_PRESENT_PREV = {
    'can', "can't", 'cannot', 'could', "couldn't", 'will', "won't", 'would',
    "wouldn't", 'should', "shouldn't", 'may', 'might', 'must', 'shall',
    'to', 'please', 'go', 'come',
}
READ_PAST_PREV = {
    'have', 'has', 'had', "haven't", "hasn't", "hadn't", "i've", "you've",
    "we've", "they've", "he's", "she's", "it's", "i'd", "you'd", "we'd",
    "they'd", "he'd", "she'd", "it'd",
}
FRAGMENT_FINAL_WORDS = (
    SUB_CONJ
    | {'and', 'or', 'but', 'so', 'yet', 'nor'}
    | {
        'about', 'above', 'across', 'after', 'against', 'along', 'around',
        'at', 'before', 'behind', 'below', 'beneath', 'beside', 'between',
        'by', 'for', 'from', 'in', 'into', 'of', 'off', 'on', 'onto', 'over',
        'through', 'to', 'under', 'until', 'up', 'with', 'within', 'without',
    }
)

# Common fast-speech reduced pronunciations (syllable-reduced forms)
REDUCED_PRONUNCIATIONS = {
    'probably':    'prɑbli',
    'actually':    'ækʃli',
    'comfortable': 'kʌmftəbl',
    'interesting': 'ɪntrəstɪŋ',
    'every':       'ɛvri',
    'different':   'dɪfrənt',
    'generally':   'dʒɛnrəli',
    'family':      'fæmli',
    'evening':     'ivnɪŋ',
    'favorite':    'feɪvrɪt',
    'average':     'ævrɪdʒ',
    'history':     'hɪstri',
    'memory':      'mɛmri',
    'category':    'kæɾɪɡri',
    'temperature': 'tɛmprətʃər',
    'chocolate':   'tʃɑklɪt',
    'vegetable':   'vɛdʒtəbl',
    'literally':   'lɪtrəli',
    'naturally':   'nætʃrəli',
    'separately':  'sɛprɪtli',
    'regularly':   'rɛɡjəli',
    'camera':      'kæmrə',
    'business':    'bɪznɪs',
    'several':     'sɛvrəl',
}


def _clean_word(token: str) -> str:
    return token.strip(BOUNDARY_PUNCT + QUOTE_CHARS).lower()


def _has_boundary_after(token: str) -> bool:
    return token.rstrip(QUOTE_CHARS).endswith(tuple(BOUNDARY_PUNCT))


def _has_boundary_before(token: str) -> bool:
    return token.lstrip(QUOTE_CHARS).startswith(tuple(BOUNDARY_PUNCT))


def _is_group_start(index: int, en_tokens: list[str], breaks: set[int]) -> bool:
    return (
        index == 0
        or index in breaks
        or (index > 0 and _has_boundary_after(en_tokens[index - 1]))
        or _has_boundary_before(en_tokens[index])
    )


def _ipa_first_sound(ipa: str) -> str:
    return ipa.lstrip('‿ˈˌ').strip(BOUNDARY_PUNCT)[:1]


def _ipa_last_sound(ipa: str) -> str:
    core = ipa.rstrip(BOUNDARY_PUNCT + '↘↗|/ ').rstrip('ːˈˌ')
    return core[-1:] if core else ''


def _english_starts_vowel(word: str) -> bool:
    return bool(word) and word[0] in EN_VOWELS


def _td_candidate_word(word: str, sound: str) -> bool:
    if sound == 't' and word in T_ELISION_WORDS:
        return True
    if sound == 'd' and word in D_ELISION_WORDS:
        return True
    if len(word) < 3 or not word.endswith(sound):
        return False
    # Prefer final consonant clusters such as last, kept, next, and, old.
    return word[-2] in EN_CONSONANTS


def _is_teaching_link_candidate(prev_word: str, curr_word: str, curr_ipa: str) -> bool:
    if (prev_word, curr_word) in COMMON_LINKING_BIGRAMS:
        return True

    if curr_word not in LINKING_FUNCTION_WORDS:
        return False

    # Keep compact weak-function-word chains such as "of a" and "in an",
    # while avoiding ordinary content-word links like "trapped in".
    if prev_word in LINKING_FUNCTION_WORDS:
        return _english_starts_vowel(curr_word) or _ipa_first_sound(curr_ipa) in VOWELS

    return False


# ── 1. Thought group detection ──────────────────────────────────────────────

def _thought_group_breaks(en_tokens: list[str]) -> set[int]:
    """
    Return set of token indices BEFORE which a '/' thought group break belongs.
    Index 0 is never a break.

    Only clause-level boundaries are marked — commas and coordinating
    conjunctions are intentionally excluded to keep sentences readable as
    whole units. We only break at:
      - Subordinating conjunctions that open a dependent clause
      - Relative pronouns that introduce an embedded clause
    """
    breaks: set[int] = set()
    for i, tok in enumerate(en_tokens):
        if i == 0:
            continue
        clean = tok.strip(".,;:!?\"'").lower()

        # Subordinating conjunctions opening a dependent clause
        if clean in SUB_CONJ:
            breaks.add(i)
            continue
        # Relative pronouns introducing embedded clauses (mid-sentence only)
        if clean in RELATIVE and i > 1:
            breaks.add(i)
            continue
    return breaks


# ── 2. Linking (C+V and V+V) ────────────────────────────────────────────────

def _add_linking(en_tokens: list[str], ipa_tokens: list[str],
                 breaks: set[int]) -> list[str]:
    """
    Prepend ‿ to tokens that link to the previous word.
    Marks common spoken chunks directly; other high-value teaching candidates
    still need a C+V or V+V boundary. This avoids over-marking ordinary
    content-word transitions.
    No linking across thought group breaks.
    """
    out = list(ipa_tokens)
    for i in range(1, len(ipa_tokens)):
        if _is_group_start(i, en_tokens, breaks):
            continue
        prev_word = _clean_word(en_tokens[i - 1])
        curr_word = _clean_word(en_tokens[i])
        if not prev_word or not curr_word:
            continue

        curr = ipa_tokens[i].lstrip()
        if not curr:
            continue
        if not _is_teaching_link_candidate(prev_word, curr_word, curr):
            continue

        last = _ipa_last_sound(ipa_tokens[i - 1])
        first = _ipa_first_sound(curr)

        c_plus_v = last in CONSONANTS and first in VOWELS
        v_plus_v = last in VOWELS and first in VOWELS

        if (prev_word, curr_word) in COMMON_LINKING_BIGRAMS or c_plus_v or v_plus_v:
            out[i] = "‿" + out[i]
    return out


# ── 3. Multi-syllable reduction ──────────────────────────────────────────────

def _apply_word_reductions(en_tokens: list[str], ipa_tokens: list[str]) -> list[str]:
    """Replace canonical IPA with common fast-speech reduced forms."""
    out = list(ipa_tokens)
    for i, tok in enumerate(en_tokens):
        word = _clean_word(tok)
        if word in REDUCED_PRONUNCIATIONS:
            out[i] = REDUCED_PRONUNCIATIONS[word]
    return out


def _replace_token_body(token: str, body: str) -> str:
    prefix_match = re.match(r'^[‿ˈˌ]*', token)
    prefix = prefix_match.group(0) if prefix_match else ''
    core = token[len(prefix):]
    stripped = core.rstrip(BOUNDARY_PUNCT)
    punct = core[len(stripped):]
    return prefix + body + punct


def _replace_token_pronunciation(token: str, pronunciation: str) -> str:
    prefix_match = re.match(r'^[‿]*', token)
    prefix = prefix_match.group(0) if prefix_match else ''
    core = token[len(prefix):]
    stripped = core.rstrip(BOUNDARY_PUNCT)
    punct = core[len(stripped):]
    return prefix + pronunciation + punct


def _apply_word_pronunciation_overrides(en_tokens: list[str], ipa_tokens: list[str]) -> list[str]:
    """Correct high-confidence word-level IPA misses before connected-speech markup."""
    out = list(ipa_tokens)
    for i, tok in enumerate(en_tokens):
        word = _clean_word(tok)
        raw_word = tok.strip(BOUNDARY_PUNCT + QUOTE_CHARS)
        if word == 'us' and raw_word != 'US':
            out[i] = _replace_token_pronunciation(out[i], 'əs')
        elif word == 'used' and i + 1 < len(en_tokens) and _clean_word(en_tokens[i + 1]) == 'to':
            out[i] = _replace_token_pronunciation(out[i], 'ˈjuːst')
            out[i + 1] = _replace_token_pronunciation(out[i + 1], 'tə')
        elif word in WORD_PRONUNCIATION_OVERRIDES:
            out[i] = _replace_token_pronunciation(out[i], WORD_PRONUNCIATION_OVERRIDES[word])
    return out


def apply_word_pronunciation_overrides(text: str, ipa_raw: str) -> str:
    """Apply high-confidence word-level pronunciation fixes to aligned IPA."""
    if not ipa_raw:
        return ipa_raw

    en_tokens = [t for t in re.split(r'\s+', text.strip()) if t]
    ipa_tokens = [t for t in re.split(r'\s+', ipa_raw.strip()) if t]
    if len(en_tokens) != len(ipa_tokens):
        return ipa_raw

    ipa_tokens = _apply_word_pronunciation_overrides(en_tokens, ipa_tokens)
    ipa_tokens = _disambiguate_read(en_tokens, ipa_tokens)
    return ' '.join(ipa_tokens)


def _disambiguate_read(en_tokens: list[str], ipa_tokens: list[str]) -> list[str]:
    """Correct present/past read when context makes the tense clear."""
    out = list(ipa_tokens)
    for i, tok in enumerate(en_tokens):
        if _clean_word(tok) != 'read':
            continue
        prev = _clean_word(en_tokens[i - 1]) if i > 0 else ''
        if prev in READ_PAST_PREV or prev.endswith("'ve") or prev.endswith("'d"):
            out[i] = _replace_token_body(out[i], 'rɛd')
        elif prev in READ_PRESENT_PREV:
            out[i] = _replace_token_body(out[i], 'riːd')
    return out


# ── 4. Flapping: intervocalic /t/ → [ɾ] ─────────────────────────────────────

COMMON_FLAPPING_WORDS = {
    'better', 'butter', 'water', 'city', 'pretty', 'party', 'dirty', 'forty',
    'getting', 'sitting', 'matter', 'later', 'bottom', 'little',
}


def _add_flapping(ipa_tokens: list[str], en_tokens: list[str] | None = None) -> list[str]:
    """
    American English flapping: intervocalic /t/ → [ɾ] within a word.
    Pattern: vowel (+ optional ː) + t + vowel → replace t with ɾ.
    Only within a single IPA token (no cross-word flapping).
    """
    vowel_cls = '[æɑɒɔəɛɜɪiʊuʌeɐɚɝ]'
    pattern = re.compile(
        rf'({vowel_cls}[ːˑ]?|r)t({vowel_cls})',
        re.UNICODE,
    )
    out: list[str] = []
    for i, tok in enumerate(ipa_tokens):
        if en_tokens is not None:
            word = _clean_word(en_tokens[i]) if i < len(en_tokens) else ''
            if word not in COMMON_FLAPPING_WORDS:
                out.append(tok)
                continue
        out.append(pattern.sub(r'\1ɾ\2', tok))
    return out


# ── 5. Elision: (t), (d), (h) ───────────────────────────────────────────────

def _add_elision(en_tokens: list[str], ipa_tokens: list[str],
                 breaks: set[int]) -> list[str]:
    """
    Mark likely connected-speech candidates in the IPA tokens:
      (h) — unstressed function-word candidates lose initial /h/
      (t)/(d) — common fast-speech final cluster candidates before consonants
    These are rule-based hints for study, not real audio conclusions.
    """
    out = list(ipa_tokens)
    n   = len(out)

    for i in range(n):
        en_word = en_tokens[i].strip(".,;:!?\"'").lower() if i < len(en_tokens) else ''
        ipa = out[i]

        prev_word = _clean_word(en_tokens[i - 1]) if i > 0 else ''
        raw_word = en_tokens[i].strip(BOUNDARY_PUNCT + QUOTE_CHARS) if i < len(en_tokens) else ''

        # (h) deletion: unstressed, non-initial function-word candidates.
        if (
            en_word in H_ELISION_WORDS
            and not _is_group_start(i, en_tokens, breaks)
            and raw_word.islower()
            and prev_word not in EMPHASIS_CUES
        ):
            ipa = re.sub(r'^(‿?)h', r'\1(h)', ipa)
            out[i] = ipa
            continue

        # (t)/(d): conservative fast-speech candidates before consonants.
        if i < n - 1:
            next_word = _clean_word(en_tokens[i + 1])
            if (
                _has_boundary_after(en_tokens[i])
                or _is_group_start(i + 1, en_tokens, breaks)
                or not next_word
                or next_word[0] in TD_BLOCK_NEXT_INITIALS
            ):
                continue

            next_sound = _ipa_first_sound(ipa_tokens[i + 1])
            if next_sound not in CONSONANTS:
                continue

            # Strip trailing punctuation from current token for inspection
            ipa_body = ipa.rstrip(".,;:!?")
            punct    = ipa[len(ipa_body):]

            if ipa_body.endswith('t') and _td_candidate_word(en_word, 't'):
                out[i] = ipa_body[:-1] + '(t)' + punct
            elif ipa_body.endswith('d') and _td_candidate_word(en_word, 'd'):
                out[i] = ipa_body[:-1] + '(d)' + punct

    return out


# ── 4. Thought group markers + per-group intonation ─────────────────────────

def _build_annotated(ipa_tokens: list[str], breaks: set[int],
                     en_tokens: list[str]) -> str:
    """
    Insert '/' at break positions and append ↗/↘ at the last token of each group.

    Non-final groups → ↗   (incomplete, listener keeps attention)
    Final group      → ↘ (statement/wh-q/command) or ↗ (yes-no question)
    """
    n = len(ipa_tokens)

    # Sentence-final tone
    raw_text   = ' '.join(en_tokens)
    first_word = en_tokens[0].lower().strip("\"'") if en_tokens else ''
    if raw_text.rstrip().endswith('?'):
        final_tone = '↗' if first_word not in WH_WORDS else '↘'
    elif _is_incomplete_fragment(en_tokens):
        final_tone = '↗'
    else:
        final_tone = '↘'

    # Build group spans as (start_idx, end_idx inclusive)
    sorted_breaks = sorted(breaks)
    group_ends = [b - 1 for b in sorted_breaks] + [n - 1]

    # Append tone symbol to the last token of each group
    out = list(ipa_tokens)
    for k, end in enumerate(group_ends):
        if 0 <= end < n:
            tone = final_tone if k == len(group_ends) - 1 else '↗'
            out[end] = out[end].rstrip() + ' ' + tone

    # Insert '/' before each break position (iterate in order; earlier insertions
    # don't affect later indices because we build a new list)
    result: list[str] = []
    for i, tok in enumerate(out):
        if i in breaks:
            result.append('/')
        result.append(tok)

    return ' '.join(result)


def _is_incomplete_fragment(en_tokens: list[str]) -> bool:
    raw_text = ' '.join(en_tokens).strip()
    if not raw_text or re.search(r'[.?!;]["\')\]]*$', raw_text):
        return False
    last_word = _clean_word(en_tokens[-1])
    return last_word in FRAGMENT_FINAL_WORDS


# ── 5. Public entry point ────────────────────────────────────────────────────

def annotate(text: str, ipa_raw: str) -> str:
    """
    Given original English text and raw eng-to-ipa output, return an
    annotated IPA string with linking, elision, thought groups, and intonation.
    Existing ˈˌ stress marks from eng-to-ipa are preserved.
    """
    if not ipa_raw:
        return ipa_raw

    en_tokens  = [t for t in re.split(r'\s+', text.strip())    if t]
    ipa_tokens = [t for t in re.split(r'\s+', ipa_raw.strip()) if t]

    breaks = _thought_group_breaks(en_tokens)

    # Token-level linking and elision require aligned lengths
    if len(en_tokens) == len(ipa_tokens):
        ipa_tokens = _apply_word_pronunciation_overrides(en_tokens, ipa_tokens)
        ipa_tokens = _disambiguate_read(en_tokens, ipa_tokens)
        ipa_tokens = _add_flapping(ipa_tokens, en_tokens)
        ipa_tokens = _add_linking(en_tokens, ipa_tokens, breaks)
        ipa_tokens = _add_elision(en_tokens, ipa_tokens, breaks)

    return _build_annotated(ipa_tokens, breaks, en_tokens)
