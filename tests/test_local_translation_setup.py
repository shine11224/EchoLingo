import hashlib
import io
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_download_verified_rejects_tampered_payload(tmp_path, monkeypatch):
    from webapp.services import local_translation_setup as setup

    monkeypatch.setattr(
        setup.urllib.request, "urlopen", lambda *args, **kwargs: _Response(b"tampered")
    )
    destination = tmp_path / "download.bin"
    with pytest.raises(setup.LocalTranslationSetupError, match="校验失败"):
        setup._download_verified("https://example.invalid/file", "0" * 64, destination)
    assert not destination.exists()
    assert not (tmp_path / "download.bin.part").exists()


def test_one_click_install_places_model_and_llama_runtime(tmp_path, monkeypatch):
    from webapp.services import hy_translate
    from webapp.services import local_translation_setup as setup

    model_payload = b"local-hy-model"
    model_sha = hashlib.sha256(model_payload).hexdigest()
    llama_buffer = io.BytesIO()
    with zipfile.ZipFile(llama_buffer, "w") as package:
        package.writestr("llama-server.exe", b"server")
        package.writestr("llama.dll", b"runtime")
    llama_payload = llama_buffer.getvalue()
    llama_sha = hashlib.sha256(llama_payload).hexdigest()

    model_dir = tmp_path / "models"
    llama_dir = tmp_path / "llama-cpp"
    cache_dir = tmp_path / ".cache"
    monkeypatch.setattr(setup, "_MODEL_DIR", model_dir)
    monkeypatch.setattr(setup, "_MODEL_PATH", model_dir / "model.gguf")
    monkeypatch.setattr(setup, "_MODEL_SIZE", len(model_payload))
    monkeypatch.setattr(setup, "_MODEL_SHA256", model_sha)
    monkeypatch.setattr(setup, "_LLAMA_DIR", llama_dir)
    monkeypatch.setattr(setup, "_LLAMA_EXECUTABLE", llama_dir / "llama-server.exe")
    monkeypatch.setattr(setup, "_LLAMA_SHA256", llama_sha)
    monkeypatch.setattr(setup, "_CACHE_DIR", cache_dir)
    monkeypatch.setattr(setup, "_platform_info", lambda: ("Windows AMD64", True))
    monkeypatch.setattr(hy_translate, "stop_local_server", lambda: None)

    def fake_urlopen(request, timeout=0):
        url = getattr(request, "full_url", str(request))
        return _Response(llama_payload if "llama" in url else model_payload)

    monkeypatch.setattr(setup.urllib.request, "urlopen", fake_urlopen)
    result = setup.install(
        expected_version=setup._PACK_VERSION,
        accepted_license=True,
    )

    assert result["installed"] is True
    assert (model_dir / "model.gguf").read_bytes() == model_payload
    assert (llama_dir / "llama-server.exe").read_bytes() == b"server"
    assert (llama_dir / "llama.dll").read_bytes() == b"runtime"


def test_install_requires_license_acceptance():
    from webapp.services import local_translation_setup as setup

    with pytest.raises(ValueError, match="Tencent HY Community License"):
        setup.install(expected_version=setup._PACK_VERSION, accepted_license=False)
