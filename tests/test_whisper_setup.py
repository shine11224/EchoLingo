import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


def _write_model_snapshot(root: Path, model_name: str, revision: str) -> Path:
    snapshot = root / f"models--Systran--faster-whisper-{model_name}" / "snapshots" / revision
    snapshot.mkdir(parents=True)
    for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt"):
        (snapshot / name).write_bytes(name.encode("utf-8"))
    return snapshot


def test_detects_pinned_model_in_existing_cache(tmp_path, monkeypatch):
    from webapp.services import whisper_setup

    revision = whisper_setup.MODEL_SPECS["base"]["revision"]
    snapshot = _write_model_snapshot(tmp_path, "base", revision)
    monkeypatch.setattr(whisper_setup, "cache_roots", lambda: [tmp_path])

    status = whisper_setup.model_status("base")
    assert status["installed"] is True
    assert status["snapshot_dir"] == str(snapshot)
    assert whisper_setup.resolve_download_root("base") == tmp_path


def test_one_click_download_uses_pinned_revision(tmp_path, monkeypatch):
    from webapp.services import whisper_setup

    calls = []
    monkeypatch.delenv("ELT_DEPLOYMENT", raising=False)
    monkeypatch.setattr(whisper_setup, "_PROJECT_CACHE", tmp_path)
    monkeypatch.setattr(whisper_setup, "cache_roots", lambda: [tmp_path])

    def fake_download(*, repo_id, revision, cache_dir):
        calls.append((repo_id, revision, cache_dir))
        _write_model_snapshot(tmp_path, "base", revision)
        return str(tmp_path)

    monkeypatch.setattr("huggingface_hub.snapshot_download", fake_download)
    result = whisper_setup.download_model("base")

    assert result["model"]["installed"] is True
    assert calls == [(
        "Systran/faster-whisper-base",
        whisper_setup.MODEL_SPECS["base"]["revision"],
        str(tmp_path),
    )]


def test_cloud_mode_blocks_local_model_download(monkeypatch):
    from webapp.services import whisper_setup

    monkeypatch.setenv("ELT_DEPLOYMENT", "cloud")
    with pytest.raises(whisper_setup.WhisperSetupError, match="云端部署"):
        whisper_setup.download_model("base")
