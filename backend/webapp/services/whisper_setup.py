"""Detection and one-click downloads for public local faster-whisper models."""
from __future__ import annotations

import os
import threading
from pathlib import Path

from webapp.runtime import ai_config

MODEL_SPECS = {
    "base": {
        "label": "base（本地，最快）",
        "size_label": "约 150 MB",
        "revision": "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
    },
    "medium": {
        "label": "medium（本地，均衡）",
        "size_label": "约 1.5 GB",
        "revision": "08e178d48790749d25932bbc082711ddcfdfbc4f",
    },
    "large-v3": {
        "label": "large-v3（本地，最准）",
        "size_label": "约 3 GB",
        "revision": "edaa852ec7e145841d8ffdb056a99866b5f0a478",
    },
}

_PROJECT_CACHE = ai_config.BASE_DIR / ".cache" / "huggingface" / "hub"
_CRITICAL_FILES = {
    "model": ("model.bin",),
    "config": ("config.json",),
    "tokenizer": ("tokenizer.json",),
    "vocabulary": ("vocabulary.txt", "vocabulary.json"),
}
_DOWNLOAD_LOCK = threading.Lock()


class WhisperSetupError(RuntimeError):
    pass


class WhisperSetupBusyError(WhisperSetupError):
    pass


def local_models_enabled() -> bool:
    return os.environ.get("ELT_DEPLOYMENT", "").strip().casefold() != "cloud"


def cache_roots() -> list[Path]:
    roots = [_PROJECT_CACHE]
    if os.environ.get("HUGGINGFACE_HUB_CACHE"):
        roots.append(Path(os.environ["HUGGINGFACE_HUB_CACHE"]).expanduser())
    if os.environ.get("HF_HOME"):
        roots.append(Path(os.environ["HF_HOME"]).expanduser() / "hub")
    roots.append(Path.home() / ".cache" / "huggingface" / "hub")
    result = []
    seen = set()
    for root in roots:
        key = str(root.resolve())
        if key not in seen:
            seen.add(key)
            result.append(root)
    return result


def _snapshot_status(model_name: str, root: Path) -> dict | None:
    spec = MODEL_SPECS[model_name]
    model_root = root / f"models--Systran--faster-whisper-{model_name}"
    snapshot = model_root / "snapshots" / spec["revision"]
    if not snapshot.is_dir():
        return None
    files = {
        kind: next((str(snapshot / name) for name in names if (snapshot / name).is_file()), "")
        for kind, names in _CRITICAL_FILES.items()
    }
    installed = all(files.values())
    size_bytes = 0
    if installed:
        try:
            size_bytes = sum(path.stat().st_size for path in snapshot.iterdir() if path.is_file())
        except OSError:
            size_bytes = 0
    return {
        "installed": installed,
        "cache_root": str(root),
        "snapshot_dir": str(snapshot),
        "size_bytes": size_bytes,
        "files": files,
    }


def model_status(model_name: str) -> dict:
    if model_name not in MODEL_SPECS:
        raise ValueError(f"不支持的 Whisper 模型：{model_name}")
    spec = MODEL_SPECS[model_name]
    found = None
    for root in cache_roots():
        candidate = _snapshot_status(model_name, root)
        if candidate and candidate["installed"]:
            found = candidate
            break
        if candidate and found is None:
            found = candidate
    return {
        "name": model_name,
        "label": spec["label"],
        "size_label": spec["size_label"],
        "revision": spec["revision"],
        "repo_id": f"Systran/faster-whisper-{model_name}",
        **(found or {
            "installed": False,
            "cache_root": "",
            "snapshot_dir": "",
            "size_bytes": 0,
            "files": {},
        }),
    }


def status() -> dict:
    enabled = local_models_enabled()
    return {
        "enabled": enabled,
        "deployment": os.environ.get("ELT_DEPLOYMENT", "local") or "local",
        "download_root": str(_PROJECT_CACHE),
        "source": "https://huggingface.co/Systran",
        "models": [model_status(name) for name in MODEL_SPECS] if enabled else [],
    }


def resolve_download_root(model_name: str) -> Path:
    current = model_status(model_name)
    if current["installed"] and current["cache_root"]:
        return Path(current["cache_root"])
    return _PROJECT_CACHE


def download_model(model_name: str) -> dict:
    if not local_models_enabled():
        raise WhisperSetupError("云端部署不允许下载或运行本地 Whisper 模型")
    if model_name not in MODEL_SPECS:
        raise ValueError(f"不支持的 Whisper 模型：{model_name}")
    current = model_status(model_name)
    if current["installed"]:
        return {"ok": True, "model": current}
    if not _DOWNLOAD_LOCK.acquire(blocking=False):
        raise WhisperSetupBusyError("另一个 Whisper 模型正在下载，请稍候")
    try:
        try:
            from huggingface_hub import snapshot_download
        except ModuleNotFoundError as exc:
            raise WhisperSetupError("缺少 huggingface-hub，请先安装 requirements.txt") from exc
        spec = MODEL_SPECS[model_name]
        _PROJECT_CACHE.mkdir(parents=True, exist_ok=True)
        snapshot_download(
            repo_id=f"Systran/faster-whisper-{model_name}",
            revision=spec["revision"],
            cache_dir=str(_PROJECT_CACHE),
        )
        installed = model_status(model_name)
        if not installed["installed"]:
            raise WhisperSetupError("模型下载完成，但完整性检测未通过")
        return {"ok": True, "model": installed}
    except (ValueError, WhisperSetupError):
        raise
    except Exception as exc:
        raise WhisperSetupError(f"Whisper 模型下载失败：{exc}") from exc
    finally:
        _DOWNLOAD_LOCK.release()
