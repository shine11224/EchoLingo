"""Hy-MT1.5 local translation service via llama-server HTTP API."""
from __future__ import annotations

import json
import os
import random
import re
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
_cloud_request_lock = threading.Lock()
_cloud_last_request_at = 0.0

_PROMPT_EN_ZH = (
    "Translate the following segment into Chinese,"
    " without additional explanation.\n\n{text}"
)


class TranslationServiceError(RuntimeError):
    """Cloud translation failure with enough metadata for retry/resume decisions."""

    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


def _env_number(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _cloud_status_code(exc: Exception) -> int | None:
    value = getattr(exc, "status_code", None)
    if value is None:
        value = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def is_retryable_translation_error(error: Exception | str) -> bool:
    """Only classify temporary provider/network failures as safe to resume."""
    if isinstance(error, TranslationServiceError):
        return error.retryable
    status = _cloud_status_code(error) if isinstance(error, Exception) else None
    if status in {408, 409, 425, 429, 500, 502, 503, 504}:
        return True
    text = str(error or "").casefold()
    if re.search(r"\b(?:408|409|425|429|500|502|503|504)\b", text):
        return True
    return any(marker in text for marker in (
        "rate_limit", "rate limit", "429006", "temporarily unavailable",
        "service busy", "model capacity", "容量上限", "服务繁忙",
        "timeout", "timed out", "connection reset", "connection error",
        # Compatibility with failures persisted before provider errors were preserved.
        "hy-mt returned an empty translation",
    ))


def _retry_after_seconds(exc: Exception, attempt: int) -> float:
    base = _env_number(
        "HY_TRANSLATE_RETRY_BASE_SECONDS", 2.0, minimum=0.0, maximum=60.0
    )
    cap = _env_number(
        "HY_TRANSLATE_RETRY_MAX_SECONDS", 30.0, minimum=0.0, maximum=300.0
    )
    delay = min(cap, base * (2 ** attempt))
    headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
    try:
        provider_delay = float(headers.get("retry-after", 0) or 0)
    except (TypeError, ValueError):
        provider_delay = 0.0
    delay = min(cap, max(delay, provider_delay))
    return delay + random.uniform(0.0, min(1.0, delay * 0.25))


def _hy_remote_config() -> dict | None:
    """Dedicated remote translation engine (e.g. TokenHub Hy-MT), independent of
    the main LLM config. Enabled when HY_TRANSLATE_API_KEY is set."""
    key = os.environ.get("HY_TRANSLATE_API_KEY", "").strip()
    if not key:
        return None
    return {
        "api_key": key,
        "base_url": os.environ.get("HY_TRANSLATE_BASE_URL", "").strip()
        or "https://tokenhub.tencentmaas.com/v1",
        "model": os.environ.get("HY_TRANSLATE_MODEL", "").strip() or "hy-mt2-plus",
    }


def _cloud_translation_ready() -> bool:
    """Use the configured remote model when no local Hy-MT runtime is available."""
    if _hy_remote_config():
        return True
    from webapp.runtime import ai_config

    if not (ai_config.AI_API_KEY and ai_config.AI_MODEL):
        return False
    if os.environ.get("ELT_DEPLOYMENT") == "cloud":
        return True
    # 本地模式：装了本地 Hy-MT 模型就优先本地（免费、离线、不烧 API 额度）；
    # 没装才回退到主 AI 配置，让 README 承诺的「已配置 AI 接口即可翻译」成立。
    return not (_MODEL_PATH.exists() and _EXECUTABLE.exists())


def _translate_with_cloud_ai(text: str) -> str:
    global _cloud_last_request_at
    cfg = _hy_remote_config()
    if cfg:
        from openai import OpenAI

        client = OpenAI(
            api_key=cfg["api_key"], base_url=cfg["base_url"],
            max_retries=0, timeout=30.0,
        )
        model = cfg["model"]
    else:
        from webapp.runtime import ai_config

        client = ai_config.client
        model = ai_config.AI_MODEL

    attempts = int(_env_number(
        "HY_TRANSLATE_MAX_ATTEMPTS", 6, minimum=1, maximum=10
    ))
    min_interval = _env_number(
        "HY_TRANSLATE_MIN_INTERVAL_SECONDS", 2.0, minimum=0.0, maximum=30.0
    )
    # A single lock covers every course/user in this process. This prevents several
    # background translation jobs from multiplying request pressure on TokenHub.
    with _cloud_request_lock:
        for attempt in range(attempts):
            interval_wait = max(
                0.0, _cloud_last_request_at + min_interval - time.monotonic()
            )
            if interval_wait:
                time.sleep(interval_wait)
            _cloud_last_request_at = time.monotonic()
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "You are a precise English-to-Chinese translator."},
                        {"role": "user", "content": _PROMPT_EN_ZH.format(text=text.strip())},
                    ],
                    temperature=0.1,
                    # 专用 Hy-MT 不推理，512 足够；主 AI 回退可能是推理模型
                    # （reasoning 先吃 token），512 会被思考耗尽导致 content 为空，
                    # 回退路径放宽到 4096。
                    max_tokens=512 if cfg else 4096,
                )
                content = str(response.choices[0].message.content or "").strip()
                if content:
                    return content
                finish = getattr(response.choices[0], "finish_reason", "")
                raise TranslationServiceError(
                    "Hy-MT cloud translation returned an empty response"
                    + (f" (finish_reason={finish})" if finish else ""),
                    retryable=True,
                )
            except Exception as exc:
                retryable = is_retryable_translation_error(exc)
                if retryable and attempt + 1 < attempts:
                    delay = _retry_after_seconds(exc, attempt)
                    print(
                        "Cloud translation temporary failure; "
                        f"retry {attempt + 2}/{attempts} in {delay:.1f}s: {exc}"
                    )
                    time.sleep(delay)
                    continue
                status = _cloud_status_code(exc)
                message = (
                    f"Hy-MT cloud translation failed after {attempt + 1} attempt(s): {exc}"
                )
                print(message)
                raise TranslationServiceError(
                    message, retryable=retryable, status_code=status
                ) from exc


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
