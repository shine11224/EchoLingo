"""统一定位 ffmpeg/ffprobe 可执行文件。

解析顺序：FFMPEG_PATH 环境变量 → Release 包内 tools/ffmpeg → PATH → conda 环境。
云端 Docker 无 tools/ 目录，自动落到 PATH；开发机 conda 走最后一条。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLED_FFMPEG_DIR = _REPO_ROOT / "tools" / "ffmpeg"


def _exe(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def find_ffmpeg() -> str:
    configured = str(os.environ.get("FFMPEG_PATH") or "").strip()
    if configured and Path(configured).exists():
        return configured
    bundled = BUNDLED_FFMPEG_DIR / _exe("ffmpeg")
    if bundled.exists():
        return str(bundled)
    found = shutil.which("ffmpeg")
    if found:
        return found
    conda_root = Path(sys.executable).parent
    for candidate in (conda_root / _exe("ffmpeg"), conda_root / "Library" / "bin" / _exe("ffmpeg")):
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError("ffmpeg not found. Install ffmpeg or use the Release bundle.")


def find_ffprobe() -> str:
    found = shutil.which("ffprobe")
    if found:
        return found
    candidate = Path(find_ffmpeg()).with_name(_exe("ffprobe"))
    if candidate.exists():
        return str(candidate)
    raise FileNotFoundError("ffprobe not found. Install ffmpeg/ffprobe first.")


def find_ffmpeg_dir() -> str | None:
    """返回包含 ffmpeg 的目录，供 yt-dlp 的 ffmpeg_location 使用；找不到返回 None。"""
    try:
        return str(Path(find_ffmpeg()).parent)
    except FileNotFoundError:
        return None
