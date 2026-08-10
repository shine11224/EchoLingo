"""Optional Docling-based PDF reflow extraction (high-quality tier).

Docling runs in an isolated subprocess: keeps torch out of the server process
and applies the Windows env quirks (UTF-8 mode, torch compile off, certifi CA
for first model download). Any failure returns None so callers fall back to
the geometric text-layer pipeline. Set ELT_DOCLING=off to force-disable.
"""
from __future__ import annotations

import json
import os
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


def items_to_paragraphs(items: list[dict]) -> list[str]:
    """Filter docling items to plain body paragraphs."""
    paragraphs = []
    for item in items:
        if str(item.get("label") or "").lower() not in KEEP_LABELS:
            continue
        text = " ".join(str(item.get("text") or "").split())
        if text:
            paragraphs.append(text)
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
