import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


def test_app_startup_does_not_launch_local_translation_server(monkeypatch):
    from fastapi.testclient import TestClient
    from fastapi_server import create_app
    from webapp.services import hy_translate

    def unexpected_start(*args, **kwargs):
        raise AssertionError("llama-server must be started lazily")

    monkeypatch.setattr(hy_translate, "ensure_ready", unexpected_start)
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200


def test_translation_runtime_reuses_an_existing_healthy_server(monkeypatch):
    from webapp.services import hy_translate

    monkeypatch.setattr(hy_translate, "_server_proc", None)
    monkeypatch.setattr(hy_translate, "_healthcheck", lambda **kwargs: True)

    def unexpected_popen(*args, **kwargs):
        raise AssertionError("a healthy server must not be started twice")

    monkeypatch.setattr(hy_translate.subprocess, "Popen", unexpected_popen)

    assert hy_translate.ensure_ready() is True
    assert hy_translate._server_proc is None


def test_translation_runtime_starts_lazily_and_reaps_on_shutdown(
    tmp_path, monkeypatch
):
    from webapp.services import hy_translate

    # 隔离本机 .env：create_app 加载后 HY_TRANSLATE_API_KEY 会让 ensure_ready 走云端快路径
    monkeypatch.delenv("HY_TRANSLATE_API_KEY", raising=False)
    monkeypatch.delenv("ELT_DEPLOYMENT", raising=False)

    class FakeProcess:
        returncode = None

        def __init__(self):
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            raise AssertionError("graceful termination should be enough")

    model = tmp_path / "model.gguf"
    executable = tmp_path / "llama-server.exe"
    model.touch()
    executable.touch()
    fake_process = FakeProcess()
    checks = iter([False, False, True])

    monkeypatch.setattr(hy_translate, "_MODEL_PATH", model)
    monkeypatch.setattr(hy_translate, "_EXECUTABLE", executable)
    monkeypatch.setattr(hy_translate, "_LLAMA_DIR", tmp_path)
    monkeypatch.setattr(hy_translate, "_server_proc", None)
    monkeypatch.setattr(
        hy_translate, "_healthcheck", lambda **kwargs: next(checks)
    )
    monkeypatch.setattr(
        hy_translate.subprocess, "Popen", lambda *args, **kwargs: fake_process
    )

    assert hy_translate.ensure_ready() is True
    assert hy_translate._server_proc is fake_process

    hy_translate.stop_local_server()

    assert fake_process.terminated is True
    assert hy_translate._server_proc is None
