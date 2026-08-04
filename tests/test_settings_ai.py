"""设置页 AI API 配置（含混元翻译 / 千问转录）端点的读写回环测试。"""
import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


def _client(tmp_path, monkeypatch):
    from webapp.runtime import ai_config

    # 把可变配置导向临时目录，绝不碰真实 .env
    monkeypatch.setenv("ELT_CONFIG_DIR", str(tmp_path))
    for key in (
        "HY_TRANSLATE_API_KEY",
        "HY_TRANSLATE_MODEL",
        "DASHSCOPE_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    from fastapi_server import create_app

    return TestClient(create_app()), ai_config


def test_get_settings_includes_hy_and_dashscope_fields(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    data = client.get("/api/settings/ai").json()
    assert "hy_translate_api_key" in data
    assert "hy_translate_model" in data
    assert "dashscope_api_key" in data
    # 未配置时模型名回退默认，key 为空
    assert data["hy_translate_model"] == "hy-mt2-plus"
    assert data["dashscope_api_key"] == ""


def test_save_roundtrip_persists_hy_and_dashscope(tmp_path, monkeypatch):
    client, ai_config = _client(tmp_path, monkeypatch)
    payload = {
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "groq_api_key": "",
        "hy_translate_api_key": "hy-test-key",
        "hy_translate_model": "hy-mt2-pro",
        "dashscope_api_key": "sk-dashscope-test",
    }
    resp = client.post("/api/settings/ai", json=payload)
    assert resp.json()["ok"] is True

    # 运行时环境立即生效（混元/千问在调用处实时读 os.environ）
    assert os.environ["HY_TRANSLATE_API_KEY"] == "hy-test-key"
    assert os.environ["HY_TRANSLATE_MODEL"] == "hy-mt2-pro"
    assert os.environ["DASHSCOPE_API_KEY"] == "sk-dashscope-test"

    # .env 持久化，GET 同源读回
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "HY_TRANSLATE_API_KEY=hy-test-key" in env_text
    assert "DASHSCOPE_API_KEY=sk-dashscope-test" in env_text
    data = client.get("/api/settings/ai").json()
    assert data["hy_translate_api_key"] == "hy-test-key"
    assert data["hy_translate_model"] == "hy-mt2-pro"
    assert data["dashscope_api_key"] == "sk-dashscope-test"


def test_delete_hy_and_dashscope_fields(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    client.post("/api/settings/ai", json={
        "hy_translate_api_key": "hy-x",
        "dashscope_api_key": "sk-x",
    })
    for field in ("hy_translate_api_key", "dashscope_api_key"):
        resp = client.request("DELETE", "/api/settings/ai", json={"field": field})
        assert resp.status_code == 200, field
        assert resp.json()["ok"] is True
    assert os.environ.get("HY_TRANSLATE_API_KEY", "") == ""
    assert os.environ.get("DASHSCOPE_API_KEY", "") == ""

    resp = client.request("DELETE", "/api/settings/ai", json={"field": "no_such"})
    assert resp.status_code == 400
