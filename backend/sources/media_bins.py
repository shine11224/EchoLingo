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
BUNDLED_VCREDIST_DIR = _REPO_ROOT / "tools" / "vcredist"


def enable_bundled_runtime() -> None:
    """把 Release 包内的运行时 DLL 目录加入 Windows DLL 搜索路径。

    Python 3.8+ 解析 ctypes/pyd 依赖时不再查 PATH 和 exe 所在目录，只认
    ``os.add_dll_directory()``。未装 VC++ 2015-2022 Redistributable 的干净
    Windows 上 ctranslate2.dll 会因缺 msvcp140.dll 加载失败（Whisper 全挂），
    因此 Release 打包 tools/vcredist 并在这里登记。tesseract/ffmpeg 目录一
    并登记，便于其依赖解析；目录不存在时静默跳过（云端/开发环境无 tools/）。
    必须在 import ctranslate2 / faster_whisper 之前调用。
    """
    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return
    for directory in (BUNDLED_VCREDIST_DIR, BUNDLED_FFMPEG_DIR, _REPO_ROOT / "tools" / "tesseract"):
        try:
            if directory.is_dir():
                os.add_dll_directory(str(directory))
        except OSError:
            continue


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
