# -*- coding: utf-8 -*-
"""Quick L2 rule-layer verification for phonetics improvements."""
import sys
sys.path.insert(0, '.')
import eng_to_ipa as ipa_lib
from phonetics_processor import annotate, _add_flapping

CASES = [
    # (label, english_text)
    ("flapping: better",        "better"),
    ("flapping: water",         "water"),
    ("flapping: party",         "party"),
    ("flapping: dirty",         "dirty"),
    ("flapping: city",          "city"),
    ("flapping: butter",        "butter"),
    ("reduction: probably",     "probably fine"),
    ("reduction: actually",     "actually works"),
    ("reduction: interesting",  "that's interesting"),
    ("linking: going to",       "going to help you"),
    ("linking: kind of a",      "kind of a big deal"),
    ("linking: want to",        "I want to know"),
    ("flap: got you",           "got you covered"),
    ("flap: would you",         "would you like that"),
    ("elision: just go",        "just go ahead"),
    ("weak-form: of a",         "out of a sudden"),
]

EXPECTED_FLAP = {"better", "water", "city", "butter"}   # expect 'd' (learner-friendly flap)
EXPECTED_R_FLAP = {"party", "dirty"}
EXPECTED_REDUCTION = {"probably": "prɑbli", "actually": "ækʃli", "interesting": "ɪntrəstɪŋ"}

print("=" * 60)
print("L2 规则层验证")
print("=" * 60)
issues = []
for label, en in CASES:
    raw = ipa_lib.convert(en)
    out = annotate(en, raw)
    flag = ""
    # Check flapping
    first_word = en.split()[0].lower()
    if first_word in EXPECTED_FLAP and "d" not in out:
        flag = "  ❌ 预期有 d (flapping)"
        issues.append(label)
    if first_word in EXPECTED_R_FLAP and "d" not in out:
        flag = "  ❌ 预期有 d (r+t flapping)"
        issues.append(label)
    # Check reduction
    for word, expected_ipa in EXPECTED_REDUCTION.items():
        if word in en and expected_ipa not in out:
            flag = f"  ❌ 预期含 {expected_ipa}"
            issues.append(label)
    # Check linking
    if "going to" in en and "‿" not in out:
        flag = "  ❌ 预期有 ‿ 连读"
        issues.append(label)
    if "kind of" in en and "‿" not in out:
        flag = "  ❌ 预期有 ‿ 连读"
        issues.append(label)

    print(f"[{label}]")
    print(f"  EN  : {en}")
    print(f"  raw : {raw}")
    print(f"  ann : {out}{flag}")
    print()

print("=" * 60)
if issues:
    print(f"❌ 问题项: {len(issues)} — {issues}")
else:
    print("✅ 所有检查通过")
