"""
断句质量快速验证脚本。

用法：
  python scripts/test_segmentation.py                    # 用默认两个 VTT 测试
  python scripts/test_segmentation.py path/to/file.vtt  # 指定 VTT

输出：segment 数、punct 覆盖率、bad 比例、max 词数、前 15 句预览。
改完 subtitle_parser.py / analyzer.py 后跑一次，对比前后数据即可。
"""

import argparse
import re
import sys
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from sources.subtitle_parser import parse_subtitle_file
from analyzer import SentenceAnalyzer

SENT_END = re.compile(r"[.!?]\s*$")

DEFAULT_VTTS = [
    ("WITH-PUNCT", Path(".cache/youtube/dYRmZdwi9mo/dYRmZdwi9mo.en.vtt")),
    ("NO-PUNCT",   Path("backend/.cache/youtube/p09yRj47kNM/p09yRj47kNM.en-orig.vtt")),
]


def analyze(label: str, vtt: Path, use_ai: bool = False) -> None:
    if not vtt.exists():
        print(f"[{label}] 文件不存在: {vtt}")
        return

    segs = parse_subtitle_file(vtt)
    analyzer = SentenceAnalyzer()
    local = analyzer._merge_raw_segments(segs) if use_ai else analyzer._local_merge_segments(segs)
    windows = analyzer._token_windows(analyzer._timed_tokens(segs))
    segs_in_windows = sum(len(batch) for batch, _core_ids in windows)

    total = len(local)
    has_punct = sum(1 for s in local if SENT_END.search(s.text.strip()))
    bad = [s for s in local if len(s.text.split()) >= 20 and not SENT_END.search(s.text.strip())]
    huge = [s for s in local if len(s.text.split()) >= 35]
    mx = max(len(s.text.split()) for s in local)
    wc = Counter(len(s.text.split()) for s in local)

    print(f"\n{'='*60}")
    print(f"[{label}]  {vtt}")
    print(f"{'='*60}")
    print(f"  parse_subtitle_file : {len(segs)} segs")
    print(f"  segmentation mode    : {'AI token boundaries' if use_ai else 'local fallback'}")
    print(f"  output segments      : {total} segs")
    print(f"  terminal punct      : {has_punct}/{total} ({has_punct*100//total}%)")
    print(f"  bad (>=20w, no-punct): {len(bad)} ({len(bad)*100//total}%)")
    print(f"  huge (>=35w)        : {len(huge)}")
    print(f"  max words           : {mx}w")
    print(f"  AI token windows    : {len(windows)} windows, {segs_in_windows} token refs")

    # word count distribution buckets
    buckets = [(1,5), (6,10), (11,15), (16,20), (21,25), (26,30), (31,99)]
    dist = []
    for lo, hi in buckets:
        n = sum(v for k, v in wc.items() if lo <= k <= hi)
        dist.append(f"{lo}-{hi}w:{n}")
    print(f"  distribution        : {' | '.join(dist)}")

    print(f"\n  --- first 15 segs ---")
    for s in local[:15]:
        mark = "!" if len(s.text.split()) >= 20 and not SENT_END.search(s.text.strip()) else " "
        print(f"  {mark}[{s.index:3}] ({len(s.text.split()):2}w) {s.text[:80]}")

    if bad:
        print(f"\n  --- bad segments (top 5) ---")
        for s in bad[:5]:
            nxt_idx = s.index  # find next in local
            nxt = next((x for x in local if x.index == s.index + 1), None)
            print(f"  [{s.index:3}] ({len(s.text.split())}w) {s.text[:70]}")
            if nxt:
                print(f"       -> next [{nxt.index}] ({len(nxt.text.split())}w) {nxt.text[:70]}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("vtt", nargs="?", type=Path)
    parser.add_argument("--ai", action="store_true", help="Call AI cue-boundary segmentation")
    args = parser.parse_args()
    if args.vtt:
        vtts = [("CUSTOM", args.vtt)]
    else:
        vtts = DEFAULT_VTTS

    for label, vtt in vtts:
        analyze(label, vtt, use_ai=args.ai)

    print()


if __name__ == "__main__":
    main()
