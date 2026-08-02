import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from fastapi.testclient import TestClient


def test_local_media_without_transcript_does_not_pass_removed_resegment_flag(tmp_path, monkeypatch):
    from fastapi_server import create_app
    import webapp.fastapi_routes.jobs as jobs

    media = tmp_path / "sample.mp3"
    media.write_bytes(b"not-a-real-mp3")
    captured = {}

    class FakeProcess:
        def __init__(self, command, **kwargs):
            captured["command"] = command
            self.stdout = []
            self.returncode = 0

        def wait(self):
            self.returncode = 0
            return 0

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(jobs.subprocess, "Popen", FakeProcess)

    client = TestClient(create_app())
    response = client.post("/api/generate", json={
        "source_type": "local",
        "local_path": str(media),
        "analysis_mode": "mock",
        "whisper_model": "base",
    })

    assert response.status_code == 200
    assert response.json()["job_id"]

    deadline = time.time() + 2
    while "command" not in captured and time.time() < deadline:
        time.sleep(0.01)

    command = captured["command"]
    assert "--video-file" in command
    assert str(media) in command
    assert "--resegment" not in command
