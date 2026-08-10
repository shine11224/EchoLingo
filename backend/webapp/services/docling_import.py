"""Optional Docling-based PDF reflow extraction (high-quality tier).

Docling runs in an isolated subprocess: keeps torch out of the server process
and applies the Windows env quirks (UTF-8 mode, torch compile off, certifi CA
for first model download). Any failure returns None so callers fall back to
the geometric text-layer pipeline. Set ELT_DOCLING=off to force-disable.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_CONVERT_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "docling_convert.py"
_TIMEOUT_SECONDS = 900  # CPU ~2s/page layout pass; generous for large PDFs

# docling item labels kept as body text; tables/formulas/pictures/captions/
# page headers/footers are dropped entirely (只保留纯文本正文).
KEEP_LABELS = ("text", "section_header", "list_item")

_probe_cache: bool | None = None

# 行尾引用残留：句末点线 + [数字]（". . . [0]"）
_TRAILING_CITATION = re.compile(r"(?:\s*\.\s*){2,}\s*\[\d+\]\s*$|\s*\[\d+\]\s*$")
# 疑似行尾断词粘连：纯小写、长度 ≥10
_JOINED_TOKEN = re.compile(r"\b[a-z]{10,}\b")


def _is_letter_spaced_block(text: str) -> bool:
    """图内 letter-spaced 标签整块剔除：≥4 个空白分词且 ≥70% 是单字母。

    此类块基本是图/流程图内部文字（"R e fi n e m e n t"，连字以 "fi" 两字母
    出现，逐 run 合并不可靠）；正常句子（"I am a boy"）单字母占比远低于阈值。"""
    tokens = text.split()
    if len(tokens) < 4:
        return False
    singles = sum(1 for token in tokens if len(token) == 1)
    return singles / len(tokens) >= 0.7


# 短但结构性的词（Docling 偶尔把章节标成 text 而非 section_header）
_STRUCTURAL_WORDS = {"abstract", "references", "appendix", "appendices", "acknowledgements", "acknowledgments", "keywords"}


def _is_noise_text(text: str) -> bool:
    """图内数字/符号标签等碎片：<12 字符、无句读、不成词组。"""
    if len(text) >= 12:
        return False
    if re.search(r"[.!?;:]", text):
        return False
    if text.lower() in _STRUCTURAL_WORDS:
        return False
    return len(text.split()) <= 2


def _word_in_dict(word: str) -> bool:
    from webapp.services import dicts

    return dicts.word_in_dict(word)


def _split_joined_words(text: str) -> str:
    """行尾断词直接粘连（"retrievalaugmented"）：词不在词典时，尝试拆成两个都在词典的
    部分并补连字符；拆不出则保留原样。词典缺失（极简部署）时整体跳过。"""
    def fix(match: re.Match) -> str:
        token = match.group(0)
        if _word_in_dict(token):
            return token
        for i in range(4, len(token) - 3):
            left, right = token[:i], token[i:]
            if _word_in_dict(left) and _word_in_dict(right):
                return f"{left}-{right}"
        return token

    return _JOINED_TOKEN.sub(fix, text)


def _polish_paragraph(text: str) -> str:
    text = _TRAILING_CITATION.sub("", text).strip()
    text = _split_joined_words(text)
    return text


def items_to_paragraphs(items: list[dict]) -> list[str]:
    """Filter docling items to plain body paragraphs, with figure-noise post-processing.

    后处理顺序：label 过滤 → 字符规整 → letter-spaced 合并 → 引用残留清洗 →
    碎片噪声剔除 → 粘连词拆分 → 小写开头段并回上一段（图文字打断的跨段句）。
    """
    paragraphs: list[str] = []
    for item in items:
        label = str(item.get("label") or "").lower()
        if label not in KEEP_LABELS:
            continue
        text = " ".join(str(item.get("text") or "").split())
        if not text:
            continue
        text = _polish_paragraph(text)
        if not text:
            continue
        if _is_letter_spaced_block(text):
            continue
        if label != "section_header" and _is_noise_text(text):
            continue
        # 小写开头的正文段是上一段被图/表打断的延续：并回上一段
        if label != "section_header" and paragraphs and text[0].islower():
            paragraphs[-1] = f"{paragraphs[-1]} {text}"
            continue
        paragraphs.append(text)
    # 二次过滤：合并可能拼出新的 letter-spaced 块（"Decision M a" + "i n g"），
    # 坐标轴刻度等纯数字/标点碎片（无字母）一并剔除。
    paragraphs = [
        paragraph
        for paragraph in paragraphs
        if not _is_letter_spaced_block(paragraph) and re.search(r"[A-Za-z一-鿿]", paragraph)
    ]
    return paragraphs


def _child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"             # 中文 Windows GBK 默认编码会炸 transformers 配置读取
    env["TORCH_COMPILE_DISABLE"] = "1"  # 无 MSVC 的机器 torch.compile 找不到 cl.exe
    env["TORCHDYNAMO_DISABLE"] = "1"
    if "SSL_CERT_FILE" not in env:      # 首次下载模型走 certifi（Windows 证书存储区损坏绕过）
        try:
            import certifi

            env["SSL_CERT_FILE"] = certifi.where()
        except ImportError:
            pass
    return env


def docling_available() -> bool:
    global _probe_cache
    if os.environ.get("ELT_DOCLING", "on").strip().lower() == "off":
        return False
    if _probe_cache is None:
        try:
            probe = subprocess.run(
                [sys.executable, "-c", "import docling"],
                capture_output=True, timeout=120, env=_child_env(),
            )
            _probe_cache = probe.returncode == 0
        except Exception:
            _probe_cache = False
    return _probe_cache


def extract_text_with_docling(pdf_path: Path) -> str | None:
    """Reflow a PDF to plain paragraphs via Docling; None on any failure."""
    if not docling_available():
        return None
    with tempfile.TemporaryDirectory(prefix="docling_") as tmp:
        out_path = Path(tmp) / "items.json"
        try:
            completed = subprocess.run(
                [sys.executable, str(_CONVERT_SCRIPT), str(pdf_path), str(out_path)],
                capture_output=True, text=True, timeout=_TIMEOUT_SECONDS, env=_child_env(),
            )
        except Exception:
            return None
        if completed.returncode != 0 or not out_path.exists():
            print(f"[docling] conversion failed: {(completed.stderr or '')[-500:]}")
            return None
        try:
            items = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    paragraphs = items_to_paragraphs(items)
    if not paragraphs:
        return None
    return "\n\n".join(paragraphs)


def extract_text_with_docling_bytes(content: bytes) -> str | None:
    """Bytes variant: materialize to a temp file, then reflow via Docling."""
    if not docling_available():
        return None
    with tempfile.TemporaryDirectory(prefix="docling_pdf_") as tmp:
        pdf_path = Path(tmp) / "upload.pdf"
        try:
            pdf_path.write_bytes(content)
        except OSError:
            return None
        return extract_text_with_docling(pdf_path)
