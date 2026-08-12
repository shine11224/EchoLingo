"""百度网盘导入服务的测试。"""
import os
import sys
import threading
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
        assert baidu_pan._max_bytes() == 500 * 1024 * 1024

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

    def test_download_and_rm_args(self, monkeypatch):
        calls = self._patch_run(monkeypatch, ["ok", "ok"])
        baidu_pan.download_file("dir/x/a.mp3", "/tmp/a.mp3")
        baidu_pan.remove_remote("dir/x")
        assert calls[0][1] == "download" and "dir/x/a.mp3" in calls[0] and "/tmp/a.mp3" in calls[0]
        assert calls[1][1] == "rm" and "dir/x" in calls[1]


class TestImportJob:
    @pytest.fixture()
    def job_env(self, monkeypatch, tmp_path):
        """fake CLI 层 + 用户 uploads 目录 + 内存 DB。"""
        monkeypatch.setattr(baidu_pan, "_CAPABILITY_CACHE", {})
        monkeypatch.setattr(baidu_pan, "_IMPORT_JOBS", {})
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
        monkeypatch.setattr(baidu_pan.user_assets, "current_uploads_root", lambda: tmp_path)
        import db as db_module
        db_module.init_db(tmp_path / "vocab.db")
        yield {"transferred": transferred, "downloaded": downloaded,
               "removed": removed, "tmp": tmp_path}
        baidu_pan._wait_job_idle()

    def test_start_import_returns_queued_job(self, job_env):
        job = baidu_pan.start_import("https://pan.baidu.com/s/1abcDEF-_Xy", "pw12",
                                     username="may")
        assert job["status"] in {"queued", "transferring", "downloading", "ready"}
        assert job["filename"] == "lesson audio.mp3"
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
        with pytest.raises(ValueError, match="单文件"):
            baidu_pan.start_import("https://pan.baidu.com/s/1abcDEF-_Xy", "", username="may")
        # 校验失败后已转存副本被清理
        assert job_env["removed"] == ["/apps/bdpan/x"]

    def test_dir_share_rejected(self, job_env, monkeypatch):
        monkeypatch.setattr(baidu_pan, "transfer_share", lambda url, pwd, d: [
            {"name": "folder", "path": "/apps/bdpan/x/folder", "is_dir": True, "size": 0}])
        with pytest.raises(ValueError, match="单文件"):
            baidu_pan.start_import("https://pan.baidu.com/s/1abcDEF-_Xy", "", username="may")

    def test_bad_extension_rejected(self, job_env, monkeypatch):
        monkeypatch.setattr(baidu_pan, "transfer_share", lambda url, pwd, d: [
            {"name": "movie.mkv", "path": "/apps/bdpan/x/movie.mkv", "is_dir": False, "size": 1}])
        with pytest.raises(ValueError, match="音视频或文本"):
            baidu_pan.start_import("https://pan.baidu.com/s/1abcDEF-_Xy", "", username="may")

    def test_doc_accepted_as_text(self, job_env, monkeypatch):
        """.doc（老二进制 Word）按文本类走，ready 返回 file_kind=text。"""
        monkeypatch.setattr(baidu_pan, "transfer_share", lambda url, pwd, d: [
            {"name": "作文.doc", "path": f"/apps/bdpan/{d}/作文.doc", "is_dir": False, "size": 10}])
        monkeypatch.setattr(baidu_pan, "download_file",
                            lambda remote, local, *, timeout=1800: Path(local).write_bytes(b"doc"))
        job = baidu_pan.start_import("https://pan.baidu.com/s/1abcDEF-_Xy", "", username="may")
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
        job = baidu_pan.start_import("https://pan.baidu.com/s/1abcDEF-_Xy", "", username="may")
        result = baidu_pan._wait_job(job["job_id"])
        assert result["status"] == "ready"
        assert result["file_kind"] == "text"
        assert Path(result["local_path"]).read_text(encoding="utf-8") == "hello world"
        assert "upload_id" not in result

    def test_oversize_rejected(self, job_env, monkeypatch):
        monkeypatch.setenv("ELT_BAIDU_PAN_MAX_MB", "1")
        monkeypatch.setattr(baidu_pan, "transfer_share", lambda url, pwd, d: [
            {"name": "big.mp3", "path": "/apps/bdpan/x/big.mp3", "is_dir": False, "size": 2 * 1024 * 1024}])
        with pytest.raises(ValueError, match="大小限制"):
            baidu_pan.start_import("https://pan.baidu.com/s/1abcDEF-_Xy", "", username="may")

    def test_transfer_failure_raises_synchronously(self, job_env, monkeypatch):
        """转存失败（如提取码错误）在请求路径同步抛出，不产生 job。"""
        def boom(url, pwd, target_dir):
            raise baidu_pan.BaiduPanError("xxx\n错误码: -9\n")
        monkeypatch.setattr(baidu_pan, "transfer_share", boom)
        with pytest.raises(baidu_pan.BaiduPanError) as exc:
            baidu_pan.start_import("https://pan.baidu.com/s/1abcDEF-_Xy", "bad",
                                   username="may")
        assert "提取码错误" in baidu_pan.friendly_message(exc.value)
        assert baidu_pan._IMPORT_JOBS == {}

    def test_disabled_feature_rejected(self, job_env, monkeypatch):
        monkeypatch.setattr(baidu_pan, "capability", lambda: {"enabled": False, "reason": "bdpan 未安装"})
        with pytest.raises(ValueError, match="不可用"):
            baidu_pan.start_import("https://pan.baidu.com/s/1abcDEF-_Xy", "", username="may")

    def test_busy_when_queue_full(self, job_env, monkeypatch):
        monkeypatch.setattr(baidu_pan, "_IMPORT_JOB_LIMIT", 1)
        blocker = threading.Event()

        def slow_download(remote, local, *, timeout=1800):
            blocker.wait(10)
            Path(local).write_bytes(b"\x00" * 1000)
        monkeypatch.setattr(baidu_pan, "download_file", slow_download)
        first = baidu_pan.start_import("https://pan.baidu.com/s/1abcDEF-_Xy", "", username="may")
        try:
            with pytest.raises(baidu_pan.BaiduPanBusyError):
                baidu_pan.start_import("https://pan.baidu.com/s/1abcDEF-_Xy", "", username="may")
        finally:
            blocker.set()
            baidu_pan._wait_job(first["job_id"])


class TestRoutes:
    @pytest.fixture()
    def client(self, monkeypatch, tmp_path):
        monkeypatch.setattr(baidu_pan, "_CAPABILITY_CACHE", {})
        monkeypatch.setattr(baidu_pan, "_IMPORT_JOBS", {})
        monkeypatch.setattr(baidu_pan, "capability", lambda: {"enabled": True})
        monkeypatch.setattr(baidu_pan, "transfer_share", lambda url, pwd, d: [
            {"name": "a.mp3", "path": f"/apps/bdpan/{d}/a.mp3", "is_dir": False, "size": 1000}])
        monkeypatch.setattr(baidu_pan, "download_file",
                            lambda remote, local, *, timeout=1800: Path(local).write_bytes(b"\x00" * 1000))
        monkeypatch.setattr(baidu_pan, "remove_remote", lambda p: None)
        monkeypatch.setattr(baidu_pan, "_probe_uploaded_media", lambda p: (60.0, "local_audio"))
        monkeypatch.setattr(baidu_pan.user_assets, "current_uploads_root", lambda: tmp_path)
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
        assert resp.json() == {"enabled": True}

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
