"""Hy-MT1.5 local translation service via llama-server HTTP API."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

_SERVER_URL = "http://127.0.0.1:8180/v1/chat/completions"
_HEALTH_URL = "http://127.0.0.1:8180/health"
_ROOT = Path(__file__).resolve().parents[3]
_MODEL_PATH = _ROOT / "models" / "HY-MT1.5-1.8B-Q4_K_M.gguf"
_LLAMA_DIR = _ROOT / "llama-cpp"
_EXECUTABLE = _LLAMA_DIR / "llama-server.exe"
_server_proc = None
_server_lock = threading.Lock()

_PROMPT_EN_ZH = (
    "Translate the following segment into Chinese,"
    " without additional explanation.\n\n{text}"
)


def _cloud_translation_ready() -> bool:
    """Use the configured remote model on constrained production hosts."""
    if os.environ.get("ELT_DEPLOYMENT") != "cloud":
        return False
    from webapp.runtime import ai_config

    return bool(ai_config.AI_API_KEY and ai_config.AI_MODEL)


def _translate_with_cloud_ai(text: str) -> str:
    from webapp.runtime import ai_config

    try:
        response = ai_config.client.chat.completions.create(
            model=ai_config.AI_MODEL,
            messages=[
                {"role": "system", "content": "You are a precise English-to-Chinese translator."},
                {"role": "user", "content": _PROMPT_EN_ZH.format(text=text.strip())},
            ],
            temperature=0.1,
            max_tokens=512,
        )
        return str(response.choices[0].message.content or "").strip()
    except Exception as exc:
        print(f"Cloud translation failed: {exc}")
        return ""


def _healthcheck(timeout: float = 2) -> bool:
    try:
        req = urllib.request.Request(_HEALTH_URL)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


@contextmanager
def _suppress_windows_error_dialogs():
    if os.name != "nt":
        yield
        return
    import ctypes

    flags = 0x0001 | 0x0002 | 0x8000
    kernel32 = ctypes.windll.kernel32
    previous = kernel32.SetErrorMode(flags)
    try:
        yield
    finally:
        kernel32.SetErrorMode(previous)


def ensure_ready(timeout: float = 30) -> bool:
    global _server_proc
    if _cloud_translation_ready():
        return True
    if _healthcheck(timeout=0.5):
        return True
    with _server_lock:
        if _healthcheck(timeout=0.5):
            return True
        if _server_proc is not None and _server_proc.poll() is not None:
            _server_proc = None
        if _server_proc is None:
            if not _MODEL_PATH.exists() or not _EXECUTABLE.exists():
                return False
            popen_options = {}
            if os.name == "nt":
                popen_options["creationflags"] = getattr(
                    subprocess, "CREATE_NO_WINDOW", 0
                )
            try:
                with _suppress_windows_error_dialogs():
                    _server_proc = subprocess.Popen(
                        [
                            str(_EXECUTABLE),
                            "-m", str(_MODEL_PATH),
                            "--host", "127.0.0.1",
                            "--port", "8180",
                            "-ngl", "0",
                            "-c", "2048",
                        ],
                        cwd=str(_LLAMA_DIR),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        **popen_options,
                    )
            except Exception as exc:
                print(f"Local translation server failed to start: {exc}")
                _server_proc = None
                return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _server_proc.poll() is not None:
                code = _server_proc.returncode
                print(
                    "Local translation server exited during startup: "
                    f"0x{code & 0xffffffff:08x}"
                )
                _server_proc = None
                return False
            if _healthcheck(timeout=0.5):
                return True
            time.sleep(0.25)
        stop_local_server()
        return False


def stop_local_server() -> None:
    global _server_proc
    process = _server_proc
    _server_proc = None
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        process.kill()
        process.wait(timeout=5)


def translate(text: str, source: str = "en", target: str = "zh") -> str:
    if not text or not text.strip():
        return ""
    if _cloud_translation_ready():
        return _translate_with_cloud_ai(text)
    if not ensure_ready():
        return ""
    body = json.dumps({
        "messages": [
            {"role": "user", "content": _PROMPT_EN_ZH.format(text=text.strip())}
        ],
        "temperature": 0.1,
        "max_tokens": 512,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        _SERVER_URL,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return ""
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return (content or "").strip()


def is_ready() -> bool:
    return ensure_ready()
