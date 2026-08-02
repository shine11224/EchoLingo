"""用已有 HTML 的 segments 测试 _merge_raw_segments 断句效果。"""
import sys, json, re, os
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import Segment
from analyzer import SentenceAnalyzer as Analyzer

HTML = "output/how-to-win-when-software-is-not-a-moat-evan-spie-12dd81b3.html"

with open(HTML, encoding="utf-8") as f:
    content = f.read()

m = re.search(r'const segments = (\[.*?\]);', content, re.DOTALL)
raw = json.loads(m.group(1))

# 把已有句子拆回短碎片，模拟 Whisper 输出（每15词切一刀）
def split_to_fragments(segs):
    frags = []
    for s in segs:
        words = s["text"].split()
        chunk_size = 8
        for i in range(0, len(words), chunk_size):
            chunk = " ".join(words[i:i+chunk_size])
            frags.append(Segment(
                index=len(frags)+1,
                text=chunk,
                start=s["start"] + i * (s["end"]-s["start"]) / len(words),
                end=s["start"] + min(i+chunk_size, len(words)) * (s["end"]-s["start"]) / len(words),
                translation=""
            ))
    return frags

fragments = split_to_fragments(raw[:15])  # 只用前15句，避免 API 调用太多
print(f"输入碎片数：{len(fragments)}")
for f in fragments[:5]:
    print(f"  [{f.start:.1f}] {f.text}")

print("\n--- 开始 AI 合并 ---\n")

analyzer = Analyzer(mode="auto")
result = analyzer._merge_raw_segments(fragments)

print(f"\n合并结果（{len(result)} 句）：")
for seg in result:
    print(f"  [{seg.start:.1f}-{seg.end:.1f}] {seg.text}")
