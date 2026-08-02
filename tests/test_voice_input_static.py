import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from fastapi.testclient import TestClient


def test_voice_input_module_is_served_with_replaceable_adapter_contract(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())

    response = client.get("/static/voice-input.js")

    assert response.status_code == 200
    assert "createVoiceInput" in response.text
    assert "createWebSpeechAdapter" in response.text
    assert "isSupported" in response.text
    assert "onInterim" in response.text
    assert "onFinal" in response.text
    assert "onStateChange" in response.text
    assert "start(language)" in response.text
    assert "stop()" in response.text
    assert "destroy()" in response.text
