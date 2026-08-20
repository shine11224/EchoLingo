"""AI and speech runtime configuration shared by non-Flask code."""
from __future__ import annotations

import os
from pathlib import Path

from openai import OpenAI

BASE_DIR = Path(__file__).resolve().parents[3]


def env_path() -> Path:
    """Resolve mutable settings outside the immutable application image when deployed."""
    config_dir = os.environ.get("ELT_CONFIG_DIR", "").strip()
    if config_dir:
        return Path(config_dir).expanduser() / ".env"
    return BASE_DIR / ".env"


def load_dotenv() -> None:
    env_file = env_path()
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


load_dotenv()

AI_API_KEY = os.environ.get("AI_API_KEY", "")
AI_BASE_URL = (
    os.environ.get("AI_BASE_URL")
    or os.environ.get("base_url_deepseek")
    or "https://api.deepseek.com"
)
AI_MODEL = os.environ.get("AI_MODEL") or "deepseek-chat"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

def _make_client() -> OpenAI:
    # Placeholder keeps the SDK constructible with no credentials configured;
    # AI endpoints report "key missing" before any request is attempted.
    return OpenAI(api_key=AI_API_KEY or "missing-api-key", base_url=AI_BASE_URL)


client = _make_client()


def rebuild_openai_client() -> None:
    global client
    client = _make_client()


def _write_env_updates(updates: dict[str, str]) -> None:
    path = env_path()
    lines = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    new_lines = []
    seen = set()
    for line in lines:
        handled = False
        for key, value in updates.items():
            if line.startswith(f"{key}="):
                new_lines.append(f"{key}={value}")
                seen.add(key)
                handled = True
                break
        if not handled:
            new_lines.append(line)

    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={value}")

    path.write_text("\n".join(new_lines), encoding="utf-8")


def save_settings(
    api_key: str,
    base_url: str,
    model: str,
    groq_api_key: str,
    hy_translate_api_key: str = "",
    hy_translate_model: str = "",
    dashscope_api_key: str = "",
) -> None:
    global AI_API_KEY, AI_BASE_URL, AI_MODEL, GROQ_API_KEY
    _write_env_updates({
        "AI_API_KEY": api_key,
        "AI_BASE_URL": base_url,
        "AI_MODEL": model,
        "GROQ_API_KEY": groq_api_key,
        "HY_TRANSLATE_API_KEY": hy_translate_api_key,
        "HY_TRANSLATE_MODEL": hy_translate_model,
        "DASHSCOPE_API_KEY": dashscope_api_key,
    })
    AI_API_KEY = api_key
    AI_BASE_URL = base_url
    AI_MODEL = model
    GROQ_API_KEY = groq_api_key
    os.environ["GROQ_API_KEY"] = groq_api_key
    # 混元翻译与千问转录在调用处实时读 os.environ，无需重建 client
    os.environ["HY_TRANSLATE_API_KEY"] = hy_translate_api_key
    os.environ["HY_TRANSLATE_MODEL"] = hy_translate_model
    os.environ["DASHSCOPE_API_KEY"] = dashscope_api_key
    rebuild_openai_client()


def delete_setting(field: str) -> str | None:
    global AI_API_KEY, AI_BASE_URL, AI_MODEL, GROQ_API_KEY
    defaults = {
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "groq_api_key": "",
        "hy_translate_api_key": "",
        "hy_translate_model": "",
        "dashscope_api_key": "",
    }
    env_key_map = {
        "api_key": "AI_API_KEY",
        "base_url": "AI_BASE_URL",
        "model": "AI_MODEL",
        "groq_api_key": "GROQ_API_KEY",
        "hy_translate_api_key": "HY_TRANSLATE_API_KEY",
        "hy_translate_model": "HY_TRANSLATE_MODEL",
        "dashscope_api_key": "DASHSCOPE_API_KEY",
    }
    if field not in defaults:
        return None

    new_value = defaults[field]
    if field == "api_key":
        AI_API_KEY = new_value
    elif field == "base_url":
        AI_BASE_URL = new_value
    elif field == "model":
        AI_MODEL = new_value
    elif field == "groq_api_key":
        GROQ_API_KEY = new_value

    rebuild_openai_client()
    os.environ[env_key_map[field]] = new_value
    _write_env_updates({env_key_map[field]: new_value})
    return new_value
