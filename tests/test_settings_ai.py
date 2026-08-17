"""设置页通用 AI / Groq / 混元配置端点的读写回环测试。"""
import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


def _client(tmp_path, monkeypatch):
    from webapp.runtime import ai_config

    # 把可变配置导向临时目录，绝不碰真实 .env
    monkeypatch.setenv("ELT_CONFIG_DIR", str(tmp_path))
    for key in ("HY_TRANSLATE_API_KEY", "HY_TRANSLATE_MODEL", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    from fastapi_server import create_app

    return TestClient(create_app()), ai_config


def test_get_settings_includes_hy_but_not_removed_dashscope_field(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "must-not-leak")
    data = client.get("/api/settings/ai").json()
    assert "hy_translate_api_key" in data
    assert "hy_translate_model" in data
    assert "dashscope_api_key" not in data
    # 未配置时模型名回退默认
    assert data["hy_translate_model"] == "hy-mt2-plus"


def test_save_roundtrip_persists_hy_and_generic_provider(tmp_path, monkeypatch):
    client, ai_config = _client(tmp_path, monkeypatch)
    payload = {
        "api_key": "",
        "base_url": "https://api.kimi.com/coding/v1",
        "model": "kimi-for-coding",
        "groq_api_key": "",
        "hy_translate_api_key": "hy-test-key",
        "hy_translate_model": "hy-mt2-pro",
    }
    resp = client.post("/api/settings/ai", json=payload)
    assert resp.json()["ok"] is True

    # 运行时环境立即生效
    assert os.environ["HY_TRANSLATE_API_KEY"] == "hy-test-key"
    assert os.environ["HY_TRANSLATE_MODEL"] == "hy-mt2-pro"

    # .env 持久化，GET 同源读回
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "HY_TRANSLATE_API_KEY=hy-test-key" in env_text
    assert "AI_BASE_URL=https://api.kimi.com/coding/v1" in env_text
    assert "AI_MODEL=kimi-for-coding" in env_text
    assert "DASHSCOPE_API_KEY" not in env_text
    data = client.get("/api/settings/ai").json()
    assert data["hy_translate_api_key"] == "hy-test-key"
    assert data["hy_translate_model"] == "hy-mt2-pro"
    assert data["base_url"] == "https://api.kimi.com/coding/v1"
    assert data["model"] == "kimi-for-coding"


def test_delete_hy_field_and_reject_removed_dashscope_field(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    client.post("/api/settings/ai", json={
        "hy_translate_api_key": "hy-x",
    })
    resp = client.request("DELETE", "/api/settings/ai", json={"field": "hy_translate_api_key"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert os.environ.get("HY_TRANSLATE_API_KEY", "") == ""

    resp = client.request("DELETE", "/api/settings/ai", json={"field": "dashscope_api_key"})
    assert resp.status_code == 400

    resp = client.request("DELETE", "/api/settings/ai", json={"field": "no_such"})
    assert resp.status_code == 400


def test_delete_model_restores_flash_default(tmp_path, monkeypatch):
    client, ai_config = _client(tmp_path, monkeypatch)
    client.post("/api/settings/ai", json={"model": "custom-model"})
    resp = client.request("DELETE", "/api/settings/ai", json={"field": "model"})
    assert resp.status_code == 200
    assert ai_config.AI_MODEL == "deepseek-v4-flash"


def test_public_settings_ui_has_provider_and_baidu_presets_without_paraformer():
    html = (Path(__file__).resolve().parents[1]
            / "frontend" / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'value="deepseek">DeepSeek Flash' in html
    assert 'value="qwen">千问 Flash（阿里云百炼）' in html
    assert 'value="kimi-platform">Kimi 开放平台' in html
    assert 'value="kimi-code">Kimi Code 会员 API' in html
    assert "https://api.kimi.com/coding/v1" in html
    assert "kimi-for-coding" in html
    assert "https://dashscope.aliyuncs.com/compatible-mode/v1" in html
    assert "qwen3.6-flash" in html
    assert 'id="baidu-pan-settings-guide"' in html
    assert 'id="baidu-pan-install-btn"' in html
    assert 'id="baidu-pan-auth-code"' in html
    assert "确认并安装 bdpan" in html
    assert "--set-code <" not in html
    assert "支持 B站 / YouTube / 文章链接、百度网盘分享链接" not in html
    assert 'placeholder="粘贴 B站 / YouTube / 网盘链接' not in html
    assert 'value="paraformer"' not in html
    assert 'id="dashscope-api-key"' not in html


def test_public_baidu_settings_exposes_installer_and_install_endpoint(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    from webapp.services import baidu_pan

    monkeypatch.setattr(baidu_pan, "capability", lambda **kwargs: {
        "enabled": False, "installed": False, "reason": "bdpan 未安装",
    })
    monkeypatch.setattr(baidu_pan, "installer_info", lambda: {
        "supported": True, "version": "3.8.4", "installed": False,
    })
    data = client.get("/api/settings/baidu-pan?refresh=true").json()
    assert data["can_manage_auth"] is True
    assert data["installer"]["version"] == "3.8.4"

    captured = {}
    def fake_install(*, expected_version, confirmed):
        captured.update(version=expected_version, confirmed=confirmed)
        return {"ok": True, "installed_version": "3.8.4"}
    monkeypatch.setattr(baidu_pan, "install_cli", fake_install)
    resp = client.post("/api/settings/baidu-pan/install", json={
        "version": "3.8.4", "confirmed": True,
    })
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert captured == {"version": "3.8.4", "confirmed": True}


def test_paraformer_is_rejected_before_transcription(tmp_path):
    from sources.baidu import _transcribe_with_optional_whisper

    try:
        _transcribe_with_optional_whisper(tmp_path / "missing.mp3", "paraformer")
    except ValueError as exc:
        assert "不支持的转录模型" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("removed Paraformer model was accepted")
