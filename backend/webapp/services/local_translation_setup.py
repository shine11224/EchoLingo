"""One-click installer for EchoLingo's optional local translation runtime."""
from __future__ import annotations

import hashlib
import os
import platform
import shutil
import threading
import urllib.request
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_MODEL_DIR = _ROOT / "models"
_MODEL_PATH = _MODEL_DIR / "HY-MT1.5-1.8B-Q4_K_M.gguf"
_LLAMA_DIR = _ROOT / "llama-cpp"
_LLAMA_EXECUTABLE = _LLAMA_DIR / "llama-server.exe"
_CACHE_DIR = _ROOT / ".cache" / "local-translation"

_PACK_VERSION = "hy-mt1.5-q4_k_m+llama-b10068-win-x64"
_MODEL_REVISION = "265b2e615a7dc9b06c435dc878829ad99a512ba2"
_MODEL_URL = (
    "https://huggingface.co/tencent/HY-MT1.5-1.8B-GGUF/resolve/"
    f"{_MODEL_REVISION}/HY-MT1.5-1.8B-Q4_K_M.gguf?download=true"
)
_MODEL_SHA256 = "4383ac0c3c8e476de98ff979c2a3f069f8c4fb385e7860cf2d28da896cc477c7"
_MODEL_SIZE = 1_133_080_512
_LLAMA_VERSION = "b10068"
_LLAMA_URL = (
    "https://github.com/ggml-org/llama.cpp/releases/download/b10068/"
    "llama-b10068-bin-win-cpu-x64.zip"
)
_LLAMA_SHA256 = "01d5f30876acfb4a0be59396710f450213495c7181d8fbcce2fad045835ceb89"
_LLAMA_SIZE = 18_007_324
_INSTALL_LOCK = threading.Lock()


class LocalTranslationSetupError(RuntimeError):
    pass


class LocalTranslationSetupBusyError(LocalTranslationSetupError):
    pass


def _platform_info() -> tuple[str, bool]:
    machine = platform.machine().lower()
    supported = os.name == "nt" and machine in {"amd64", "x86_64"}
    return f"{platform.system()} {platform.machine()}", supported


def _looks_installed() -> bool:
    try:
        return (
            _MODEL_PATH.is_file()
            and _MODEL_PATH.stat().st_size == _MODEL_SIZE
            and _LLAMA_EXECUTABLE.is_file()
        )
    except OSError:
        return False


def installer_info() -> dict:
    platform_name, supported = _platform_info()
    return {
        "installed": _looks_installed(),
        "supported": supported,
        "platform": platform_name,
        "version": _PACK_VERSION,
        "model": "Tencent HY-MT1.5-1.8B Q4_K_M",
        "model_revision": _MODEL_REVISION,
        "llama_version": _LLAMA_VERSION,
        "download_bytes": _MODEL_SIZE + _LLAMA_SIZE,
        "install_location": str(_ROOT),
        "model_source": "https://huggingface.co/tencent/HY-MT1.5-1.8B-GGUF",
        "llama_source": f"https://github.com/ggml-org/llama.cpp/releases/tag/{_LLAMA_VERSION}",
        "model_download_url": _MODEL_URL,
        "llama_download_url": _LLAMA_URL,
        "model_sha256": _MODEL_SHA256,
        "llama_sha256": _LLAMA_SHA256,
        "license_url": "https://github.com/Tencent-Hunyuan/HY-MT/blob/main/License.txt",
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_verified(url: str, expected_sha256: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    partial.unlink(missing_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "EchoLingo/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output, length=8 * 1024 * 1024)
        actual = _sha256(partial)
        if actual.casefold() != expected_sha256.casefold():
            raise LocalTranslationSetupError(
                f"下载文件校验失败（期望 {expected_sha256}，实际 {actual}）"
            )
        os.replace(partial, destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def _extract_llama(archive: Path, destination: Path) -> None:
    staging = _CACHE_DIR / "llama-extracted"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive) as package:
            for member in package.infolist():
                relative = Path(member.filename)
                if member.is_dir():
                    continue
                if relative.is_absolute() or ".." in relative.parts:
                    raise LocalTranslationSetupError("llama.cpp 压缩包包含不安全路径")
                target = (staging / relative.name).resolve()
                if target.parent != staging.resolve():
                    raise LocalTranslationSetupError("llama.cpp 压缩包目录结构异常")
                with package.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
        executable = staging / "llama-server.exe"
        if not executable.is_file():
            raise LocalTranslationSetupError("llama.cpp 压缩包缺少 llama-server.exe")
        destination.mkdir(parents=True, exist_ok=True)
        for source in staging.iterdir():
            if source.is_file():
                os.replace(source, destination / source.name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def install(*, expected_version: str, accepted_license: bool) -> dict:
    if not accepted_license:
        raise ValueError("请先确认接受 Tencent HY Community License")
    if expected_version and expected_version != _PACK_VERSION:
        raise ValueError("安装版本已变化，请刷新设置页后重试")
    platform_name, supported = _platform_info()
    if not supported:
        raise LocalTranslationSetupError(
            f"当前一键安装仅支持 Windows x64；检测到 {platform_name}"
        )
    if _looks_installed():
        return {"ok": True, "installed": True, "version": _PACK_VERSION}
    if not _INSTALL_LOCK.acquire(blocking=False):
        raise LocalTranslationSetupBusyError("本地翻译组件正在安装，请稍候")
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        model_download = _CACHE_DIR / _MODEL_PATH.name
        llama_download = _CACHE_DIR / f"llama-{_LLAMA_VERSION}-win-x64.zip"
        model_ready = (
            _MODEL_PATH.is_file()
            and _MODEL_PATH.stat().st_size == _MODEL_SIZE
            and _sha256(_MODEL_PATH) == _MODEL_SHA256
        )
        llama_ready = _LLAMA_EXECUTABLE.is_file()
        if not model_ready:
            if not model_download.is_file() or _sha256(model_download) != _MODEL_SHA256:
                _download_verified(_MODEL_URL, _MODEL_SHA256, model_download)
        if not llama_ready:
            if not llama_download.is_file() or _sha256(llama_download) != _LLAMA_SHA256:
                _download_verified(_LLAMA_URL, _LLAMA_SHA256, llama_download)

        from webapp.services import hy_translate

        hy_translate.stop_local_server()
        if not model_ready:
            _MODEL_DIR.mkdir(parents=True, exist_ok=True)
            os.replace(model_download, _MODEL_PATH)
        if not llama_ready:
            _extract_llama(llama_download, _LLAMA_DIR)
            llama_download.unlink(missing_ok=True)
        if not _looks_installed():
            raise LocalTranslationSetupError("文件已下载，但本地翻译组件完整性检查未通过")
        return {
            "ok": True,
            "installed": True,
            "version": _PACK_VERSION,
            "message": "本地混元翻译已安装，无需配置混元 API Key",
        }
    except (ValueError, LocalTranslationSetupError):
        raise
    except Exception as exc:
        raise LocalTranslationSetupError(f"本地翻译安装失败：{exc}") from exc
    finally:
        _INSTALL_LOCK.release()
