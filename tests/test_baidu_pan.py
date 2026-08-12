"""百度网盘导入服务的测试。"""
import os
import sys
import threading
import sqlite3
import json
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import db
import pytest

from webapp.services import baidu_pan


class TestParseShareLink:
    def test_link_with_pwd_in_query(self):
        url, pwd = baidu_pan.parse_share_link(
            "https://pan.baidu.com/s/1stdcrjBsAvBT0UuEraf_gA?pwd=yq65", "")
        assert url == "https://pan.baidu.com/s/1stdcrjBsAvBT0UuEraf_gA"
        assert pwd == "yq65"

    def test_link_with_separate_pwd(self):
        url, pwd = baidu_pan.parse_share_link(
            "https://pan.baidu.com/s/1abcDEF-_Xy", "ab12")
        assert pwd == "ab12"

    def test_pwd_in_link_wins_over_separate(self):
        _, pwd = baidu_pan.parse_share_link(
            "https://pan.baidu.com/s/1abcDEF-_Xy?pwd=zz99", "ab12")
        assert pwd == "zz99"

    def test_trailing_slash_accepted(self):
        url, _ = baidu_pan.parse_share_link("https://pan.baidu.com/s/1abcDEF-_Xy/", "x")
        assert url == "https://pan.baidu.com/s/1abcDEF-_Xy"

    @pytest.mark.parametrize("bad", [
        "",
        "https://example.com/s/1abcDEF-_Xy",
        "https://pan.baidu.com/s/2abc",
        "pan.baidu.com/s/1abcDEF-_Xy",
        "https://pan.baidu.com/share/init?surl=abcDEF",
    ])
    def test_rejects_bad_links(self, bad):
        with pytest.raises(ValueError):
            baidu_pan.parse_share_link(bad, "")


class TestFriendlyError:
    @pytest.mark.parametrize("errno,expected", [
        ("-9", "提取码错误"),
        ("-7", "已删除或取消"),
        ("-8", "已过期"),
        ("-32", "空间不足"),
        ("-10", "空间不足"),
        ("13045", "不能转存自己分享"),
        ("13077", "空间不足"),
        ("13041", "不属于该分享"),
    ])
    def test_known_errno_mapped(self, errno, expected):
        err = baidu_pan.BaiduPanError(f"xxx\n错误码: {errno}\n")
        assert expected in baidu_pan.friendly_message(err)

    def test_unknown_errno_fallback(self):
        err = baidu_pan.BaiduPanError("错误码: 99999")
        assert "网盘操作失败" in baidu_pan.friendly_message(err)

    def test_no_errno_passthrough(self):
        assert baidu_pan.friendly_message(BaiduPanErr := baidu_pan.BaiduPanError("network timeout")) == "network timeout"


class TestConfig:
    def test_max_bytes_default(self, monkeypatch):
        monkeypatch.delenv("ELT_BAIDU_PAN_MAX_MB", raising=False)
        assert baidu_pan._max_bytes() == 1024 * 1024 * 1024

    def test_max_bytes_env(self, monkeypatch):
        monkeypatch.setenv("ELT_BAIDU_PAN_MAX_MB", "100")
        assert baidu_pan._max_bytes() == 100 * 1024 * 1024

    def test_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("ELT_BAIDU_PAN_ENABLED", "0")
        assert baidu_pan.capability()["enabled"] is False
        assert baidu_pan.capability()["reason"] == "disabled"


class TestRunCli:
    def test_raises_with_output_on_nonzero(self, monkeypatch):
        proc = MagicMock(returncode=1, stdout="部分输出\n错误码: -9\n", stderr="")
        monkeypatch.setattr(baidu_pan.subprocess, "run", lambda *a, **k: proc)
        monkeypatch.setattr(baidu_pan, "_bin", lambda: "/usr/bin/bdpan")
        with pytest.raises(baidu_pan.BaiduPanError) as exc:
            baidu_pan._run_cli(["transfer", "list", "x"], timeout=5)
        assert "-9" in str(exc.value)

    def test_timeout_mapped(self, monkeypatch):
        def boom(*a, **k):
            raise baidu_pan.subprocess.TimeoutExpired(cmd="bdpan", timeout=5)
        monkeypatch.setattr(baidu_pan.subprocess, "run", boom)
        monkeypatch.setattr(baidu_pan, "_bin", lambda: "/usr/bin/bdpan")
        with pytest.raises(baidu_pan.BaiduPanError, match="超时"):
            baidu_pan._run_cli(["ls"], timeout=5)

    def test_windows_gbk_output_decoded(self, monkeypatch):
        proc = MagicMock(returncode=0, stdout='{"name":"雅思"}'.encode("gb18030"), stderr=b"")
        monkeypatch.setattr(baidu_pan.subprocess, "run", lambda *a, **k: proc)
        monkeypatch.setattr(baidu_pan, "_bin", lambda: "bdpan.exe")
        assert "雅思" in baidu_pan._run_cli(["ls", "--json"], timeout=5)


class TestCliOperations:
    def _patch_run(self, monkeypatch, outputs):
        """outputs: list of stdout strings，按调用顺序返回。"""
        calls = []
        procs = [MagicMock(returncode=0, stdout=o, stderr="") for o in outputs]

        def fake_run(args, **kwargs):
            calls.append(list(args))
            return procs[len(calls) - 1]

        monkeypatch.setattr(baidu_pan.subprocess, "run", fake_run)
        monkeypatch.setattr(baidu_pan, "_bin", lambda: "/usr/bin/bdpan")
        return calls

    def test_transfer_share_parses_json(self, monkeypatch):
        payload = '{"count": 1, "files": [{"name": "a.mp3", "path": "/apps/bdpan/dir/a.mp3", "is_dir": false, "size": 100}]}'
        calls = self._patch_run(monkeypatch, [payload])
        files = baidu_pan.transfer_share("https://pan.baidu.com/s/1abc", "pw", "dir/x")
        assert files == [{"name": "a.mp3", "path": "/apps/bdpan/dir/a.mp3", "is_dir": False, "size": 100}]
        args = calls[0]
        assert args[:3] == ["/usr/bin/bdpan", "transfer", "https://pan.baidu.com/s/1abc"]
        assert "-d" in args and "dir/x" in args and "-p" in args and "pw" in args

    def test_transfer_share_bad_json(self, monkeypatch):
        self._patch_run(monkeypatch, ["not json"])
        with pytest.raises(baidu_pan.BaiduPanError):
            baidu_pan.transfer_share("https://pan.baidu.com/s/1abc", "", "dir/x")

    def test_transfer_share_without_pwd_omits_flag(self, monkeypatch):
        calls = self._patch_run(monkeypatch, ['{"count": 0, "files": []}'])
        baidu_pan.transfer_share("https://pan.baidu.com/s/1abc", "", "dir/x")
        assert "-p" not in calls[0]

    def test_download_and_rm_args(self, monkeypatch, tmp_path):
        calls = []
        target = tmp_path / "a.mp3"
        def fake_run(args, **kwargs):
            calls.append(list(args))
            if args[1] == "download":
                target.write_bytes(b"mp3")
            return MagicMock(returncode=0, stdout="ok", stderr="")
        monkeypatch.setattr(baidu_pan.subprocess, "run", fake_run)
        monkeypatch.setattr(baidu_pan, "_bin", lambda: "/usr/bin/bdpan")
        baidu_pan.download_file("dir/x/a.mp3", str(target))
        baidu_pan.remove_remote("dir/x")
        assert calls[0][1] == "download" and "dir/x/a.mp3" in calls[0] and str(target) in calls[0]
        assert calls[1][1] == "rm" and "dir/x" in calls[1]


class TestImportJob:
    @pytest.fixture()
    def job_env(self, monkeypatch, tmp_path):
        """fake CLI 层 + 用户 uploads 目录 + 独立持久化任务 DB。"""
        monkeypatch.setattr(baidu_pan, "_CAPABILITY_CACHE", {})
        monkeypatch.setenv("ELT_BAIDU_PAN_JOB_DB", str(tmp_path / "jobs.db"))
        baidu_pan._reset_workers_for_tests()
        monkeypatch.setattr(baidu_pan, "capability", lambda: {"enabled": True})
        transferred = []

        def fake_transfer_share(url, pwd, target_dir):
            transferred.append((url, pwd, target_dir))
            return [{"name": "lesson audio.mp3",
                     "path": f"/apps/bdpan/{target_dir}/lesson audio.mp3",
                     "is_dir": False, "size": 1000}]

        monkeypatch.setattr(baidu_pan, "transfer_share", fake_transfer_share)
        downloaded = []

        def fake_download(remote, local, *, timeout=1800):
            downloaded.append(remote)
            Path(local).write_bytes(b"\x00" * 1000)

        monkeypatch.setattr(baidu_pan, "download_file", fake_download)
        removed = []
        monkeypatch.setattr(baidu_pan, "remove_remote", lambda p: removed.append(p))
        monkeypatch.setattr(baidu_pan, "_probe_uploaded_media",
                            lambda path: (60.0, "local_audio"))
        uploads = tmp_path / "uploads"
        monkeypatch.setattr(baidu_pan.user_assets, "current_uploads_root", lambda: uploads)
        monkeypatch.setattr(baidu_pan, "_enough_space", lambda root, size: True)
        import db as db_module
        db_module.init_db(tmp_path / "vocab.db")
        yield {"transferred": transferred, "downloaded": downloaded,
               "removed": removed, "tmp": uploads}
        baidu_pan._wait_job_idle()

    def test_start_import_returns_queued_job(self, job_env):
        job = baidu_pan.start_import("https://pan.baidu.com/s/1abcDEF-_Xy", "pw12",
                                     username="may")
        assert job["status"] in {"queued", "transferring", "downloading", "processing", "ready"}
        baidu_pan._wait_job(job["job_id"])

    def test_job_reaches_ready_with_upload_record(self, job_env):
        job = baidu_pan.start_import("https://pan.baidu.com/s/1abcDEF-_Xy", "pw12",
                                     username="may")
        final = baidu_pan._wait_job(job["job_id"])
        assert final["status"] == "ready"
        upload = db.get_v2_media_upload(final["upload_id"])
        assert upload["original_filename"] == "lesson audio.mp3"
        assert upload["media_kind"] == "local_audio"
        assert (job_env["tmp"] / upload["stored_relpath"]).is_file()
        assert final["quote"]["points"] > 0
        # 网盘副本已清理（整个任务目录）
        assert job_env["removed"] == [f"/apps/bdpan/echolingo-imports/{job['job_id']}"]

    def test_status_requires_same_user(self, job_env):
        job = baidu_pan.start_import("https://pan.baidu.com/s/1abcDEF-_Xy", "pw12",
                                     username="may")
        baidu_pan._wait_job(job["job_id"])
        with pytest.raises(ValueError):
            baidu_pan.get_import_status(job["job_id"], username="bob")
        assert baidu_pan.get_import_status(job["job_id"], username="may")["job_id"] == job["job_id"]

    def test_multi_file_share_rejected(self, job_env, monkeypatch):
        monkeypatch.setattr(baidu_pan, "transfer_share", lambda url, pwd, d: [
            {"name": "a.mp3", "path": "/apps/bdpan/x/a.mp3", "is_dir": False, "size": 1},
            {"name": "b.mp3", "path": "/apps/bdpan/x/b.mp3", "is_dir": False, "size": 1}])
        job = baidu_pan.start_import("https://pan.baidu.com/s/1abcDEF-_Xy", "pw12", username="may")
        assert "单文件" in baidu_pan._wait_job(job["job_id"])["error"]

    def test_dir_share_rejected(self, job_env, monkeypatch):
        monkeypatch.setattr(baidu_pan, "transfer_share", lambda url, pwd, d: [
            {"name": "folder", "path": "/apps/bdpan/x/folder", "is_dir": True, "size": 0}])
        job = baidu_pan.start_import("https://pan.baidu.com/s/1abcDEF-_Xy", "pw12", username="may")
        assert "单文件" in baidu_pan._wait_job(job["job_id"])["error"]

    def test_bad_extension_rejected(self, job_env, monkeypatch):
        monkeypatch.setattr(baidu_pan, "transfer_share", lambda url, pwd, d: [
            {"name": "movie.mkv", "path": "/apps/bdpan/x/movie.mkv", "is_dir": False, "size": 1}])
        job = baidu_pan.start_import("https://pan.baidu.com/s/1abcDEF-_Xy", "pw12", username="may")
        assert "格式" in baidu_pan._wait_job(job["job_id"])["error"]

    def test_doc_accepted_as_text(self, job_env, monkeypatch):
        """.doc（老二进制 Word）按文本类走，ready 返回 file_kind=text。"""
        monkeypatch.setattr(baidu_pan, "transfer_share", lambda url, pwd, d: [
            {"name": "作文.doc", "path": f"/apps/bdpan/{d}/作文.doc", "is_dir": False, "size": 10}])
        monkeypatch.setattr(baidu_pan, "download_file",
                            lambda remote, local, *, timeout=1800: Path(local).write_bytes(b"doc"))
        job = baidu_pan.start_import("https://pan.baidu.com/s/1abcDEF-_Xy", "pw12", username="may")
        result = baidu_pan._wait_job(job["job_id"])
        assert result["status"] == "ready"
        assert result["file_kind"] == "text"

    def test_text_file_ready_with_local_path(self, job_env, monkeypatch):
        """文本类（txt/md/docx/pdf）跳过 ffprobe/媒体注册，ready 返回 file_kind+local_path。"""
        monkeypatch.setattr(baidu_pan, "transfer_share", lambda url, pwd, d: [
            {"name": "notes.txt", "path": f"/apps/bdpan/{d}/notes.txt", "is_dir": False, "size": 12}])
        def fake_download(remote, local, *, timeout=1800):
            local.write_text("hello world", encoding="utf-8")
        monkeypatch.setattr(baidu_pan, "download_file", fake_download)
        monkeypatch.setattr(
            baidu_pan, "_probe_uploaded_media",
            lambda p: (_ for _ in ()).throw(AssertionError("文本不应走 ffprobe")))
        job = baidu_pan.start_import("https://pan.baidu.com/s/1abcDEF-_Xy", "pw12", username="may")
        result = baidu_pan._wait_job(job["job_id"])
        assert result["status"] == "ready"
        assert result["file_kind"] == "text"
        assert Path(result["local_path"]).read_text(encoding="utf-8") == "hello world"
        assert "upload_id" not in result

    def test_oversize_rejected(self, job_env, monkeypatch):
        monkeypatch.setenv("ELT_BAIDU_PAN_MAX_MB", "1")
        monkeypatch.setattr(baidu_pan, "transfer_share", lambda url, pwd, d: [
            {"name": "big.mp3", "path": "/apps/bdpan/x/big.mp3", "is_dir": False, "size": 2 * 1024 * 1024}])
        job = baidu_pan.start_import("https://pan.baidu.com/s/1abcDEF-_Xy", "pw12", username="may")
        assert "1GB" in baidu_pan._wait_job(job["job_id"])["error"]

    def test_transfer_failure_raises_synchronously(self, job_env, monkeypatch):
        """转存失败（如提取码错误）在 worker 中进入可重试失败态。"""
        def boom(url, pwd, target_dir):
            raise baidu_pan.BaiduPanError("xxx\n错误码: -9\n")
        monkeypatch.setattr(baidu_pan, "transfer_share", boom)
        job = baidu_pan.start_import("https://pan.baidu.com/s/1abcDEF-_Xy", "bad1",
                                     username="may")
        assert "提取码错误" in baidu_pan._wait_job(job["job_id"])["error"]

    def test_disabled_feature_rejected(self, job_env, monkeypatch):
        monkeypatch.setattr(baidu_pan, "capability", lambda: {"enabled": False, "reason": "bdpan 未安装"})
        job = baidu_pan.start_import("https://pan.baidu.com/s/1abcDEF-_Xy", "pw12", username="may")
        deadline = __import__('time').time() + 3
        while __import__('time').time() < deadline:
            state = baidu_pan.get_import_status(job["job_id"], username="may")
            if state["status"] == "waiting_auth": break
            __import__('time').sleep(.05)
        assert state["status"] == "waiting_auth"

    def test_busy_when_queue_full(self, job_env, monkeypatch):
        blocker = threading.Event()

        def slow_download(remote, local, *, timeout=1800):
            blocker.wait(10)
            Path(local).write_bytes(b"\x00" * 1000)
        monkeypatch.setattr(baidu_pan, "download_file", slow_download)
        first = baidu_pan.start_import("https://pan.baidu.com/s/1abcDEF-_Xy", "pw12", username="may")
        try:
            with pytest.raises(baidu_pan.BaiduPanBusyError):
                baidu_pan.start_import("https://pan.baidu.com/s/1abcDEF-_Xy", "pw12", username="may")
        finally:
            blocker.set()
            baidu_pan._wait_job(first["job_id"])


class TestRoutes:
    @pytest.fixture()
    def client(self, monkeypatch, tmp_path):
        monkeypatch.setattr(baidu_pan, "_CAPABILITY_CACHE", {})
        monkeypatch.setenv("ELT_BAIDU_PAN_JOB_DB", str(tmp_path / "jobs.db"))
        baidu_pan._reset_workers_for_tests()
        monkeypatch.setattr(baidu_pan, "capability", lambda: {"enabled": True})
        monkeypatch.setattr(baidu_pan, "transfer_share", lambda url, pwd, d: [
            {"name": "a.mp3", "path": f"/apps/bdpan/{d}/a.mp3", "is_dir": False, "size": 1000}])
        monkeypatch.setattr(baidu_pan, "download_file",
                            lambda remote, local, *, timeout=1800: Path(local).write_bytes(b"\x00" * 1000))
        monkeypatch.setattr(baidu_pan, "remove_remote", lambda p: None)
        monkeypatch.setattr(baidu_pan, "_probe_uploaded_media", lambda p: (60.0, "local_audio"))
        monkeypatch.setattr(baidu_pan.user_assets, "current_uploads_root", lambda: tmp_path)
        monkeypatch.setattr(baidu_pan, "_enough_space", lambda root, size: True)
        import db as db_module
        db_module.init_db(tmp_path / "vocab.db")
        from fastapi.testclient import TestClient
        from webapp.fastapi_routes import v2_lessons as routes
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(routes.router)
        return TestClient(app)

    def test_capability_endpoint(self, client):
        resp = client.get("/api/v2/lessons/baidu-pan/capability")
        assert resp.status_code == 200
        assert resp.json() == {
            "enabled": True, "can_browse": True,
            "can_manage_auth": False, "max_bytes": 1024**3,
        }

    def test_create_and_poll_import(self, client):
        resp = client.post("/api/v2/lessons/baidu-pan/imports",
                           json={"share_link": "https://pan.baidu.com/s/1abcDEF-_Xy", "pwd": "pw12"})
        assert resp.status_code == 200, resp.text
        job = resp.json()
        final = baidu_pan._wait_job(job["job_id"])
        assert final["status"] == "ready"
        resp = client.get(f"/api/v2/lessons/baidu-pan/imports/{job['job_id']}")
        assert resp.status_code == 200
        assert resp.json()["upload_id"] == final["upload_id"]
        assert "username" not in resp.json()

    def test_create_bad_link_400(self, client):
        resp = client.post("/api/v2/lessons/baidu-pan/imports",
                           json={"share_link": "https://example.com/x", "pwd": ""})
        assert resp.status_code == 400

    def test_status_unknown_404(self, client):
        resp = client.get("/api/v2/lessons/baidu-pan/imports/nonexistent")
        assert resp.status_code == 404

    def test_busy_503(self, client, monkeypatch):
        def busy(*a, **k):
            raise baidu_pan.BaiduPanBusyError("网盘导入排队中，请稍后重试")
        monkeypatch.setattr(baidu_pan, "start_import", busy)
        resp = client.post("/api/v2/lessons/baidu-pan/imports",
                           json={"share_link": "https://pan.baidu.com/s/1abcDEF-_Xy", "pwd": ""})
        assert resp.status_code == 503


def test_multiuser_baidu_pan_card_routes_text_frontend_contract():
    """多用户云端旧版网盘卡（pollBaiduPanImport）必须按 file_kind 路由：
    text → reading_file 建课；media → 报价建课。
    回归 2026-08-12：云端上传 .doc 分享链接后 job.quote 为 undefined，
    前端 job.quote.points 抛 TypeError。"""
    html = (Path(__file__).resolve().parents[1]
            / "frontend" / "templates" / "index.html").read_text(encoding="utf-8")
    fn_start = html.index("function pollBaiduPanImport(")
    fn_end = html.index("initBaiduPanCard();", fn_start)
    fn = html[fn_start:fn_end]
    assert "file_kind" in fn, "多用户网盘卡未按 file_kind 路由文本导入"
    assert "reading_file" in fn, "多用户网盘卡文本导入未走 reading_file 建课"
    assert "local_path" in fn
    # 网卡卡内须有自带 TTS 开关（默认勾选）：文本导入的 tts 不再依赖下方文本卡的勾选
    assert 'id="baidu-pan-tts"' in html
    assert "baidu-pan-tts')" in fn, "多用户网盘卡文本建课未读卡内 TTS 开关"
    assert 'id="baidu-pan-tts-inline"' in html
    # 两处网盘文本建课（统一卡 + 多用户旧卡）都必须走 billableFetch：
    # 云端计费模式 tts 勾选时 reading_tts 要求 Idempotency-Key（2026-08-12 报错回归）
    assert html.count("billableFetch('reading_tts', '/api/v2/lessons/start'") >= 2


def test_transfer_share_error_envelope_raises_real_reason(monkeypatch):
    """bdpan 3.8.x 失败时退出码仍为 0，输出失败信封 {"code":1,"error":"..."}。
    回归 2026-08-12：失败信封被当空文件列表，误报「仅支持单文件分享」。"""
    monkeypatch.setattr(
        baidu_pan, "_run_cli",
        lambda *a, **k: '{"code": 1, "data": null, "error": "转存失败: 该分享链接需要提取码，请补充提取码后重试"}')
    with pytest.raises(baidu_pan.BaiduPanError, match="需要提取码"):
        baidu_pan.transfer_share("https://pan.baidu.com/s/1abcDEF-_Xy", "", "x")


def test_transfer_share_success_envelope_returns_files(monkeypatch):
    """成功信封 {"count":1,"files":[...]} 正常返回文件列表。"""
    monkeypatch.setattr(
        baidu_pan, "_run_cli",
        lambda *a, **k: '{"count": 1, "files": [{"name": "a.doc", "path": "/x/a.doc", "size": 1, "is_dir": false}]}')
    files = baidu_pan.transfer_share("https://pan.baidu.com/s/1abcDEF-_Xy", "", "x")
    assert len(files) == 1 and files[0]["name"] == "a.doc"


def test_download_file_verifies_artifact(tmp_path, monkeypatch):
    """bdpan 3.8.x 下载失败也可能退出 0，须以产物存在且非空校验。"""
    monkeypatch.setattr(baidu_pan, "_run_cli", lambda *a, **k: "")
    with pytest.raises(baidu_pan.BaiduPanError, match="下载失败"):
        baidu_pan.download_file("/x/a.doc", tmp_path / "a.doc")
    target = tmp_path / "b.doc"
    def fake_ok(*a, **k):
        target.write_bytes(b"doc")
        return ""
    monkeypatch.setattr(baidu_pan, "_run_cli", fake_ok)
    baidu_pan.download_file("/x/b.doc", target)


class TestPersistentQueueV2:
    @pytest.fixture()
    def env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ELT_BAIDU_PAN_JOB_DB", str(tmp_path / "jobs.db"))
        monkeypatch.setattr(baidu_pan, "capability", lambda **kwargs: {"enabled": True})
        monkeypatch.setattr(baidu_pan.user_assets, "current_uploads_root", lambda: tmp_path / "uploads")
        monkeypatch.setattr(baidu_pan, "_enough_space", lambda root, size: True)
        baidu_pan._reset_workers_for_tests()
        return tmp_path

    def test_share_password_never_persisted(self, env):
        job = baidu_pan.start_import(
            "https://pan.baidu.com/s/1secretABC?pwd=a1b2", "", username="alice")
        raw = Path(os.environ["ELT_BAIDU_PAN_JOB_DB"]).read_bytes()
        assert b"a1b2" not in raw
        with sqlite3.connect(os.environ["ELT_BAIDU_PAN_JOB_DB"]) as conn:
            source_ref = conn.execute(
                "SELECT source_ref FROM baidu_pan_jobs WHERE id=?", (job["job_id"],)).fetchone()[0]
        assert "pwd=" not in source_ref
        baidu_pan.cancel_import(job["job_id"], username="alice")

    def test_one_active_job_per_user(self, env, monkeypatch):
        monkeypatch.setattr(baidu_pan, "capability", lambda **kwargs: {"enabled": False})
        first = baidu_pan.start_import(
            "https://pan.baidu.com/s/1firstABC", "a1b2", username="alice")
        with pytest.raises(baidu_pan.BaiduPanBusyError, match="进行中"):
            baidu_pan.start_import(
                "https://pan.baidu.com/s/1secondABC", "a1b2", username="alice")
        baidu_pan.cancel_import(first["job_id"], username="alice")

    def test_restart_marks_download_interrupted(self, env):
        now = baidu_pan._now()
        values = baidu_pan._base_job("alice", False)
        values.update(source_type="share", source_ref="https://pan.baidu.com/s/1x",
                      status="downloading", filename="x.mp3", size=10,
                      started_at=now)
        cols = list(values)
        with baidu_pan._jobs_db() as conn:
            conn.execute(
                f"INSERT INTO baidu_pan_jobs ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                [values[c] for c in cols])
        baidu_pan.init_queue()
        with baidu_pan._jobs_db() as conn:
            row = conn.execute("SELECT status,error FROM baidu_pan_jobs WHERE id=?", (values["id"],)).fetchone()
        assert row["status"] == "failed" and "重试" in row["error"]

    def test_drive_list_marks_unsupported_and_oversize(self, env, monkeypatch):
        monkeypatch.setattr(baidu_pan, "_run_cli", lambda *a, **k: json.dumps([
            {"fs_id": 1, "server_filename": "lesson.mp3", "path": "/apps/bdpan/lesson.mp3", "size": 10},
            {"fs_id": 2, "server_filename": "archive.zip", "path": "/apps/bdpan/archive.zip", "size": 10},
            {"fs_id": 3, "server_filename": "huge.mp4", "path": "/apps/bdpan/huge.mp4", "size": 2 * 1024**3},
        ]))
        items = baidu_pan.list_drive()["items"]
        assert items[0]["selectable"] is True
        assert items[1]["selectable"] is False and "格式" in items[1]["disabled_reason"]
        assert items[2]["selectable"] is False and "1GB" in items[2]["disabled_reason"]

    def test_drive_path_traversal_rejected(self, env):
        with pytest.raises(ValueError, match="不合法"):
            baidu_pan.list_drive("../../etc")

    def test_chinese_display_path_normalised_for_cli(self, env):
        item = baidu_pan._normalise_item({
            "fs_id": 7, "server_filename": "lesson.mp3",
            "path": "我的应用数据/bdpan/courses/lesson.mp3", "size": 10,
        })
        assert item["path"] == "courses/lesson.mp3"

    def test_search_filters_files_outside_app_directory(self, env, monkeypatch):
        monkeypatch.setattr(baidu_pan, "_run_cli", lambda *a, **k: json.dumps({"items": [
            {"fs_id": 1, "server_filename": "ok.mp3", "path": "/apps/bdpan/ok.mp3", "size": 10},
            {"fs_id": 2, "server_filename": "private.mp3", "path": "/我的资源/private.mp3", "size": 10},
        ]}))
        items = baidu_pan.search_drive("mp3")["items"]
        assert [item["name"] for item in items] == ["ok.mp3"]

    def test_locate_drive_item_uses_exact_file_path(self, env, monkeypatch):
        raw = {"fs_id": 7, "server_filename": "lesson.mp3",
               "path": "/apps/bdpan/deep/lesson.mp3", "size": 10}
        calls = []
        monkeypatch.setattr(baidu_pan, "_run_cli", lambda args, **k: calls.append(args) or json.dumps([raw]))
        job = {"id": "j1", "source_ref": "7", "filename": "lesson.mp3"}
        with baidu_pan._MEMORY_LOCK:
            baidu_pan._DIRECT_PATHS["j1"] = "deep/lesson.mp3"
        assert baidu_pan._locate_drive_item(job)["file_id"] == "7"
        assert calls[0][:2] == ["ls", "deep/lesson.mp3"]

    def test_start_drive_import_preserves_normalised_file_id(self, env, monkeypatch):
        monkeypatch.setattr(baidu_pan, "_ensure_workers", lambda: None)
        item = {"file_id": "real-7", "name": "lesson.mp3", "path": "deep/lesson.mp3",
                "is_dir": False, "size": 10, "mtime": "123", "selectable": True,
                "disabled_reason": ""}
        job = baidu_pan.start_drive_import(item, username="", is_admin=True)
        with baidu_pan._jobs_db() as conn:
            row = conn.execute("SELECT source_ref FROM baidu_pan_jobs WHERE id=?", (job["job_id"],)).fetchone()
        assert row["source_ref"] == "real-7"
        baidu_pan.cancel_import(job["job_id"], username="")

    def test_search_and_ls_mtime_are_canonicalised(self, env):
        epoch = 1783005049
        iso = __import__('datetime').datetime.fromtimestamp(
            epoch, tz=__import__('datetime').timezone.utc).isoformat()
        assert baidu_pan._normalise_mtime(epoch) == baidu_pan._normalise_mtime(iso)

    def test_daily_quota_reservation(self, env, monkeypatch):
        monkeypatch.setenv("ELT_BAIDU_PAN_DAILY_GB", "1")
        baidu_pan._reserve_quota("alice", 900 * 1024**2, "one")
        with pytest.raises(baidu_pan.BaiduPanBusyError, match="3GB"):
            baidu_pan._reserve_quota("alice", 200 * 1024**2, "two")
