"""百度网盘导入：持久化 FIFO 队列、分享转存与管理员网盘直读。

凭据由 bdpan 自身保存在部署机器的配置目录。本模块不读取 Token，不把提取码
写入 SQLite；分享提取码仅保存在当前进程内存，重启后由用户重新补充。
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import math
import locale
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import db
from webapp.runtime import ai_config
from webapp.storage import user_assets
from webapp.services.v2_lessons import (
    _probe_uploaded_media,
    _safe_upload_filename,
    media_upload_quote,
)

_SHARE_RE = re.compile(
    r"^https?://pan\.baidu\.com/s/1(?P<surl>[A-Za-z0-9_\-]+?)"
    r"(?:\?pwd=(?P<pwd>[A-Za-z0-9]{4}))?/?$"
)
_ERRNO_RE = re.compile(r"错误码[:：]\s*(-?\d+)")
_AUTH_CODE_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_MEDIA_EXTS = {".mp3", ".mp4", ".m4a", ".wav", ".mov"}
_TEXT_EXTS = {".txt", ".md", ".doc", ".docx", ".pdf"}
_SUPPORTED_EXTS = _MEDIA_EXTS | _TEXT_EXTS
_TERMINAL = {"ready", "failed", "cancelled"}
_WAITING = {"queued", "waiting_password", "waiting_auth", "waiting_quota", "waiting_space"}
_ACTIVE = _WAITING | {"transferring", "downloading", "processing"}

_BDPAN_INSTALL_VERSION = "3.8.4"
_BDPAN_SOURCE_REPO = "https://github.com/baidu-netdisk/bdpan-storage"
_BDPAN_CDN_BASE = (
    "https://issuecdn.baidupcs.com/issue/netdisk/ai-bdpan/installer/"
    f"{_BDPAN_INSTALL_VERSION}"
)
_BDPAN_INSTALLERS = {
    "darwin-amd64": "cc6b10d4afea9baad77c68dadea6b9e4ecd7f8815cf364ebe6be0e51648e4623",
    "darwin-arm64": "a0c395a83f9abc8f1423c30b21dfae73819376f7b1822d3bd4d3de62392c4c0c",
    "linux-amd64": "02050e9a5ed5c5ddc314bf920c103238a669366a130e3bd43a125d83fdd00548",
    "linux-arm64": "abb39a1f7dc0bf44883bbb30b057e6e65c5db64ffdce7c2ae92dea60a136362a",
    "windows-amd64": "194f5174bbb3d9260cc5b7d465c4f98d6b9279e028db136dc1e57cc3bc1f49a0",
}
_BDPAN_INSTALL_MAX_BYTES = 100 * 1024 * 1024
_INSTALL_LOCK = threading.Lock()

_ERRNO_MESSAGES = {
    "-9": "提取码错误，请核对后重试",
    "-7": "分享链接已删除或取消",
    "-8": "分享链接已过期，请让分享者重新生成",
    "-32": "服务器网盘空间不足，请联系管理员",
    "-10": "服务器网盘空间不足，请联系管理员",
    "13045": "不能转存自己分享的文件，请切换到“我的网盘”选择文件",
    "13077": "服务器网盘空间不足，请联系管理员",
    "13041": "转存失败：文件不属于该分享链接",
}


class BaiduPanError(RuntimeError):
    pass


class BaiduPanBusyError(RuntimeError):
    pass


class BaiduPanCancelled(BaiduPanError):
    pass


def parse_share_link(raw: str, pwd: str) -> tuple[str, str]:
    text = (raw or "").strip()
    match = _SHARE_RE.match(text)
    if not match:
        raise ValueError("分享链接格式不正确，应为 https://pan.baidu.com/s/1... 单文件分享")
    link_pwd = match.group("pwd") or ""
    return f"https://pan.baidu.com/s/1{match.group('surl')}", link_pwd or (pwd or "").strip()


def friendly_message(err: Exception) -> str:
    text = str(err)
    match = _ERRNO_RE.search(text)
    if not match:
        return text
    return _ERRNO_MESSAGES.get(match.group(1), f"网盘操作失败（错误码 {match.group(1)}）")


def _max_bytes() -> int:
    try:
        return max(1, int(os.environ.get("ELT_BAIDU_PAN_MAX_MB", "1024"))) * 1024 * 1024
    except ValueError:
        return 1024 * 1024 * 1024


def _daily_limit_bytes() -> int:
    try:
        return max(1, int(os.environ.get("ELT_BAIDU_PAN_DAILY_GB", "3"))) * 1024**3
    except ValueError:
        return 3 * 1024**3


def _download_concurrency() -> int:
    try:
        return max(1, min(8, int(os.environ.get("ELT_BAIDU_PAN_DOWNLOAD_CONCURRENCY", "2"))))
    except ValueError:
        return 2


def _retention_days() -> int:
    try:
        return max(1, int(os.environ.get("ELT_BAIDU_PAN_JOB_RETENTION_DAYS", "7")))
    except ValueError:
        return 7


def _timezone() -> ZoneInfo:
    name = os.environ.get("TZ") or os.environ.get("ELT_TIMEZONE") or "Asia/Shanghai"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def _day_key() -> str:
    return dt.datetime.now(_timezone()).date().isoformat()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _bin() -> str:
    configured = os.environ.get("ELT_BAIDU_PAN_BIN", "").strip()
    if configured:
        return configured
    discovered = shutil.which("bdpan")
    if discovered:
        return discovered
    # 官方安装器会写入用户级目录；当前服务进程不会自动刷新 PATH，
    # 因此直接探测常见安装位置，让网页一键安装后可以立即使用。
    candidates: list[Path] = []
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            candidates.append(Path(local_app_data) / "bdpan" / "bdpan.exe")
        candidates.append(Path.home() / "AppData" / "Local" / "bdpan" / "bdpan.exe")
    else:
        candidates.extend((
            Path.home() / ".local" / "bin" / "bdpan",
            Path.home() / "bin" / "bdpan",
            Path("/usr/local/bin/bdpan"),
        ))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


def _installer_platform_key() -> str:
    system = platform.system().lower()
    os_key = {"darwin": "darwin", "linux": "linux", "windows": "windows"}.get(system, "")
    machine = platform.machine().lower()
    arch_key = "arm64" if machine in {"arm64", "aarch64"} else (
        "amd64" if machine in {"amd64", "x86_64", "x64"} else ""
    )
    return f"{os_key}-{arch_key}" if os_key and arch_key else ""


def _installed_version() -> str:
    binary = _bin()
    if not binary:
        return ""
    for args in ([binary, "version"], [binary, "--version"]):
        try:
            proc = subprocess.run(args, capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0:
            text = _decode_cli_output(proc.stdout or proc.stderr or b"").strip()
            match = re.search(r"\d+\.\d+(?:\.\d+)?", text)
            return match.group(0) if match else text.splitlines()[0][:80] if text else ""
    return ""


def installer_info() -> dict:
    platform_key = _installer_platform_key()
    checksum = _BDPAN_INSTALLERS.get(platform_key, "")
    suffix = ".exe" if platform_key.startswith("windows-") else ""
    filename = f"bdpan-installer-{platform_key}{suffix}" if platform_key else ""
    return {
        "supported": bool(checksum),
        "platform": platform_key or "unsupported",
        "version": _BDPAN_INSTALL_VERSION,
        "source_name": "百度网盘官方 bdpan-storage",
        "source_repo": _BDPAN_SOURCE_REPO,
        "download_url": f"{_BDPAN_CDN_BASE}/{filename}" if filename else "",
        "sha256": checksum,
        "installed": bool(_bin()),
        "installed_version": _installed_version(),
        "install_location": _bin() or "官方安装器的用户级默认目录（安装后自动检测，无需配置 PATH）",
    }


def _download_installer(url: str, destination: Path, expected_sha256: str) -> None:
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(url, timeout=45) as response, destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _BDPAN_INSTALL_MAX_BYTES:
                    raise BaiduPanError("官方安装器体积异常，已停止安装")
                digest.update(chunk)
                output.write(chunk)
    except (OSError, urllib.error.URLError) as exc:
        raise BaiduPanError(f"下载官方安装器失败：{exc}") from exc
    if size == 0:
        raise BaiduPanError("官方安装器下载结果为空")
    actual = digest.hexdigest().lower()
    if actual != expected_sha256.lower():
        raise BaiduPanError("官方安装器 SHA-256 校验失败，已停止安装")


def _execute_installer(path: Path) -> None:
    if os.name != "nt":
        path.chmod(path.stat().st_mode | 0o111)
    try:
        proc = subprocess.run([str(path), "--yes"], capture_output=True, timeout=300)
    except subprocess.TimeoutExpired as exc:
        raise BaiduPanError("bdpan 安装超时，请稍后重试") from exc
    except OSError as exc:
        raise BaiduPanError(f"无法运行 bdpan 官方安装器：{exc}") from exc
    if proc.returncode != 0:
        output = _decode_cli_output(proc.stderr or proc.stdout or b"").strip()
        raise BaiduPanError(output[:500] or f"bdpan 安装器退出码 {proc.returncode}")


def install_cli(*, expected_version: str, confirmed: bool) -> dict:
    """下载并执行固定版本官方安装器。必须由网页上的显式确认触发。"""
    if not confirmed:
        raise ValueError("请先确认安装来源、版本和安全提示")
    if str(expected_version or "").strip() != _BDPAN_INSTALL_VERSION:
        raise ValueError("安装版本已变化，请刷新页面后重新确认")
    info = installer_info()
    if not info["supported"]:
        raise BaiduPanError(f"当前平台暂不支持一键安装：{info['platform']}")
    if not _INSTALL_LOCK.acquire(blocking=False):
        raise BaiduPanBusyError("bdpan 正在安装，请稍候")
    try:
        suffix = ".exe" if info["platform"].startswith("windows-") else ""
        with tempfile.TemporaryDirectory(prefix="echolingo-bdpan-") as temp_dir:
            installer = Path(temp_dir) / f"bdpan-installer{suffix}"
            _download_installer(info["download_url"], installer, info["sha256"])
            _execute_installer(installer)
        _CAPABILITY_CACHE.clear()
        installed = _bin()
        if not installed:
            raise BaiduPanError("安装器已完成，但应用未找到 bdpan；请重启应用后重新检测")
        result = installer_info()
        result.update({"ok": True, "message": "bdpan 安装完成"})
        return result
    finally:
        _INSTALL_LOCK.release()


def _jobs_db_path() -> Path:
    configured = os.environ.get("ELT_BAIDU_PAN_JOB_DB", "").strip()
    if configured:
        return Path(configured)
    config_dir = os.environ.get("ELT_CONFIG_DIR", "").strip()
    if config_dir:
        return Path(config_dir) / "baidu_pan_jobs.db"
    return ai_config.BASE_DIR / "resources" / "baidu_pan_jobs.db"


@contextlib.contextmanager
def _jobs_db():
    path = _jobs_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("PRAGMA journal_mode=WAL")
    try:
        _init_jobs_db(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _init_jobs_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS baidu_pan_jobs (
      id TEXT PRIMARY KEY,
      username TEXT NOT NULL,
      is_admin INTEGER NOT NULL DEFAULT 0,
      source_type TEXT NOT NULL CHECK(source_type IN ('share','drive')),
      source_ref TEXT NOT NULL,
      filename TEXT NOT NULL DEFAULT '',
      size INTEGER NOT NULL DEFAULT 0,
      mtime TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL,
      error TEXT NOT NULL DEFAULT '',
      progress_bytes INTEGER NOT NULL DEFAULT 0,
      queue_position INTEGER NOT NULL DEFAULT 0,
      file_kind TEXT NOT NULL DEFAULT '',
      upload_id TEXT NOT NULL DEFAULT '',
      local_path TEXT NOT NULL DEFAULT '',
      duration_seconds REAL NOT NULL DEFAULT 0,
      media_kind TEXT NOT NULL DEFAULT '',
      quote_json TEXT NOT NULL DEFAULT '{}',
      quota_day TEXT NOT NULL DEFAULT '',
      quota_reserved INTEGER NOT NULL DEFAULT 0,
      quota_counted INTEGER NOT NULL DEFAULT 0,
      db_path TEXT NOT NULL DEFAULT '',
      multiuser_context INTEGER NOT NULL DEFAULT 0,
      uploads_root TEXT NOT NULL DEFAULT '',
      remote_temp_dir TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      started_at TEXT,
      finished_at TEXT,
      retry_of TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_baidu_jobs_queue
      ON baidu_pan_jobs(status, created_at);
    CREATE INDEX IF NOT EXISTS idx_baidu_jobs_user
      ON baidu_pan_jobs(username, created_at DESC);
    CREATE TABLE IF NOT EXISTS baidu_pan_daily_usage (
      username TEXT NOT NULL,
      day_key TEXT NOT NULL,
      reserved_bytes INTEGER NOT NULL DEFAULT 0,
      used_bytes INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY(username, day_key)
    );
    """)


def init_queue() -> None:
    """初始化持久化队列并恢复可恢复任务。"""
    with _jobs_db() as conn:
        conn.execute(
            "UPDATE baidu_pan_jobs SET status='failed', error=?, finished_at=?, updated_at=? "
            "WHERE status IN ('transferring','downloading','processing')",
            ("服务重启导致任务中断，请点击重试", _now(), _now()),
        )
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=_retention_days())).isoformat()
        conn.execute(
            "DELETE FROM baidu_pan_jobs WHERE status IN ('ready','failed','cancelled') "
            "AND COALESCE(finished_at, updated_at) < ?", (cutoff,),
        )
    _ensure_workers()


_CAPABILITY_CACHE: dict = {}


def capability(*, refresh: bool = False) -> dict:
    if os.environ.get("ELT_BAIDU_PAN_ENABLED", "1") != "1":
        return {"enabled": False, "installed": bool(_bin()), "reason": "disabled"}
    now = time.monotonic()
    cached = _CAPABILITY_CACHE.get("value")
    if not refresh and cached and now - _CAPABILITY_CACHE.get("at", 0) < 60:
        return dict(cached)
    result = _probe_capability()
    _CAPABILITY_CACHE.update({"value": result, "at": now})
    return dict(result)


def _probe_capability() -> dict:
    if not _bin():
        return {"enabled": False, "installed": False, "reason": "bdpan 未安装"}
    try:
        out = _run_cli(["whoami", "--json"], timeout=15)
        info = json.loads(out)
        enabled = bool(info.get("authenticated") and info.get("has_valid_token"))
        result = {
            "enabled": enabled,
            "installed": True,
            "reason": "" if enabled else "bdpan 未登录或 token 失效",
            "username": _mask_username(str(info.get("username") or info.get("user_name") or "")),
            "expires_at": str(info.get("expires_at") or info.get("expire_time") or ""),
            "checked_at": _now(),
        }
        return result
    except Exception as exc:
        return {"enabled": False, "installed": True, "reason": friendly_message(exc), "checked_at": _now()}


def _mask_username(name: str) -> str:
    if not name:
        return ""
    if len(name) <= 2:
        return name[:1] + "*"
    return name[:1] + "*" * min(4, len(name) - 2) + name[-1:]


def _run_cli(args: list[str], *, timeout: int, stdin_text: str | None = None) -> str:
    binary = _bin()
    if not binary:
        raise BaiduPanError("bdpan 未安装")
    try:
        proc = subprocess.run(
            [binary, *args],
            input=stdin_text.encode("utf-8") if stdin_text is not None else None,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise BaiduPanError(f"网盘操作超时（{timeout}s）")
    stdout = _decode_cli_output(proc.stdout or b"")
    stderr = _decode_cli_output(proc.stderr or b"")
    output = f"{stdout}\n{stderr}".strip()
    if proc.returncode != 0:
        raise BaiduPanError(output or f"bdpan 退出码 {proc.returncode}")
    return stdout


def _decode_cli_output(raw: bytes | str) -> str:
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        encoding = locale.getpreferredencoding(False) or "gb18030"
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            return raw.decode("gb18030", errors="replace")


def transfer_share(url: str, pwd: str, target_dir: str) -> list[dict]:
    args = ["transfer", url, "-d", target_dir, "--json"]
    if pwd:
        args += ["-p", pwd]
    out = _run_cli(args, timeout=180)
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise BaiduPanError(f"转存结果解析失败：{out[:200]}")
    if not isinstance(data, dict):
        raise BaiduPanError(f"转存结果解析失败：{out[:200]}")
    if data.get("error") or data.get("code") not in (None, 0):
        raise BaiduPanError(str(data.get("error") or f"网盘转存失败（code={data.get('code')}）"))
    return list(data.get("files") or data.get("items") or [])


_ACTIVE_PROCESSES: dict[str, subprocess.Popen] = {}
_ACTIVE_PROCESSES_LOCK = threading.Lock()
_WORKER_LOCAL = threading.local()


def download_file(remote_path: str, local_path: Path, *, timeout: int = 7200) -> None:
    """可取消下载；工作线程中同时把本地文件大小写回近似进度。"""
    binary = _bin()
    if not binary:
        raise BaiduPanError("bdpan 未安装")
    local_path = Path(local_path)
    job_id = str(getattr(_WORKER_LOCAL, "job_id", ""))
    # 独立调用（CLI 单测/运维脚本）仍走统一同步封装；worker 才启用 Popen
    # 轮询，以便更新进度并可取消。
    if not job_id:
        _run_cli(["download", remote_path, str(local_path)], timeout=timeout)
        if not local_path.exists() or local_path.stat().st_size == 0:
            raise BaiduPanError(f"下载失败或产物为空：{remote_path}")
        return
    try:
        proc = subprocess.Popen(
            [binary, "download", remote_path, str(local_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
        )
    except OSError as exc:
        raise BaiduPanError(str(exc)) from exc
    if job_id:
        with _ACTIVE_PROCESSES_LOCK:
            _ACTIVE_PROCESSES[job_id] = proc
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if local_path.exists() and job_id:
                    _update_job(job_id, progress_bytes=local_path.stat().st_size)
                if _cancel_requested(job_id):
                    proc.terminate()
                    with contextlib.suppress(Exception):
                        proc.wait(timeout=5)
                    raise BaiduPanCancelled("任务已取消")
                if time.monotonic() >= deadline:
                    proc.terminate()
                    raise BaiduPanError(f"网盘操作超时（{timeout}s）")
        output = f"{stdout or ''}\n{stderr or ''}".strip()
        if proc.returncode != 0:
            raise BaiduPanError(output or f"bdpan 退出码 {proc.returncode}")
    finally:
        if job_id:
            with _ACTIVE_PROCESSES_LOCK:
                _ACTIVE_PROCESSES.pop(job_id, None)
    if not local_path.exists() or local_path.stat().st_size == 0:
        raise BaiduPanError(f"下载失败或产物为空：{remote_path}")


def remove_remote(remote_path: str) -> None:
    _run_cli(["rm", "-f", remote_path], timeout=60)


def _parse_list_payload(out: str) -> list[dict]:
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise BaiduPanError("网盘目录结果解析失败") from exc
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if data.get("error") or data.get("code") not in (None, 0):
            raise BaiduPanError(str(data.get("error") or "网盘目录读取失败"))
        for key in ("items", "files", "results", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                for nested in ("items", "files", "results"):
                    if isinstance(value.get(nested), list):
                        return value[nested]
    return []


def _safe_remote_path(path: str) -> str:
    clean = str(path or "").replace("\\", "/").strip()
    if "\x00" in clean or any(part == ".." for part in PurePosixPath(clean).parts):
        raise ValueError("网盘路径不合法")
    if clean in {"", "/", "/apps/bdpan", "/apps/bdpan/"}:
        return ""
    if clean in {"我的应用数据", "我的应用数据/bdpan"}:
        return ""
    if clean.startswith("我的应用数据/bdpan/"):
        clean = clean[len("我的应用数据/bdpan/"):]
    if clean.startswith("/apps/bdpan/"):
        clean = clean[len("/apps/bdpan/"):]
    if clean.startswith("/") or clean.startswith("~"):
        raise ValueError("网盘路径不在应用目录内")
    return clean.strip("/")


def _normalise_item(raw: dict) -> dict:
    raw_path = str(raw.get("path") or raw.get("remote_path") or "")
    path = _safe_remote_path(raw_path)
    name = str(raw.get("server_filename") or raw.get("name") or PurePosixPath(path).name)
    is_dir = bool(raw.get("isdir") if "isdir" in raw else raw.get("is_dir"))
    size = int(raw.get("size") or 0)
    mtime = _normalise_mtime(raw.get("server_mtime") or raw.get("mtime") or raw.get("modified_at"))
    file_id = str(raw.get("fs_id") or raw.get("id") or hashlib.sha256(path.encode()).hexdigest()[:20])
    suffix = Path(name).suffix.lower()
    selectable = not is_dir and suffix in _SUPPORTED_EXTS and 0 < size <= _max_bytes()
    reason = ""
    if not is_dir and suffix not in _SUPPORTED_EXTS:
        reason = "暂不支持此格式"
    elif not is_dir and size <= 0:
        reason = "无法读取文件大小"
    elif not is_dir and size > _max_bytes():
        reason = "超过 1GB 上限"
    return {
        "file_id": file_id, "name": name, "path": path, "is_dir": is_dir,
        "size": size, "mtime": mtime, "selectable": selectable, "disabled_reason": reason,
    }


def _normalise_mtime(value: object) -> str:
    if value in (None, ""):
        return ""
    text = str(value)
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return str(int(float(text)))
    try:
        return str(int(dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()))
    except ValueError:
        return text


def _normalise_allowed_items(raw_items: list[dict]) -> list[dict]:
    items: list[dict] = []
    for raw in raw_items:
        try:
            items.append(_normalise_item(raw))
        except ValueError:
            # bdpan 全局搜索可能返回授权应用目录外的个人网盘结果；本应用
            # 只能访问 /apps/bdpan，目录浏览与搜索都必须静默过滤越界项。
            continue
    return items


def list_drive(path: str = "", *, page: int = 1, page_size: int = 50,
               order: str = "time", desc: bool = True) -> dict:
    clean = _safe_remote_path(path)
    page = max(1, int(page))
    page_size = max(1, min(50, int(page_size)))
    args = ["ls"]
    if clean:
        # `bdpan ls relative/folder` resolves first-level folders as the item
        # itself on some CLI builds.  The canonical app-directory path lists
        # its children and still remains inside the authorised sandbox.
        args.append(f"/apps/bdpan/{clean}")
    args += ["--json", "--order", order if order in {"name", "time", "size"} else "time"]
    if desc:
        args.append("--desc")
    items = _normalise_allowed_items(_parse_list_payload(_run_cli(args, timeout=30)))
    start = (page - 1) * page_size
    return {
        "path": clean, "items": items[start:start + page_size], "page": page,
        "page_size": page_size, "total": len(items), "has_more": start + page_size < len(items),
    }


def search_drive(query: str, *, page: int = 1, page_size: int = 50) -> dict:
    q = str(query or "").strip()
    if not q:
        raise ValueError("请输入文件名")
    page = max(1, int(page))
    page_size = max(1, min(50, int(page_size)))
    out = _run_cli(["search", q, "--page", str(page), "--page-size", str(page_size), "--json"], timeout=30)
    items = _normalise_allowed_items(_parse_list_payload(out))[:200]
    return {"query": q, "items": items, "page": page, "page_size": page_size,
            "has_more": len(items) == page_size}


def begin_web_auth() -> dict:
    if not _bin():
        raise ValueError("bdpan 未安装，请先按部署文档安装")
    out = _run_cli(["login", "--accept-disclaimer", "--get-auth-url"], timeout=30)
    urls = _URL_RE.findall(out)
    if not urls:
        raise BaiduPanError("未能生成百度授权链接")
    return {"authorization_url": urls[-1], "expires_in": 600}


def complete_web_auth(code: str) -> dict:
    clean = str(code or "").strip()
    if not _AUTH_CODE_RE.fullmatch(clean):
        raise ValueError("授权码应为 32 位十六进制字符串")
    help_text = _run_cli(["login", "--help"], timeout=15)
    if "set-code-stdin" not in help_text:
        raise BaiduPanError("当前 bdpan 版本不支持安全提交授权码，请升级到 3.6.2 或更高版本")
    # 授权码仅通过 stdin 传递，避免出现在进程参数和系统进程列表中。
    _run_cli(
        ["login", "--accept-disclaimer", "--set-code-stdin"],
        timeout=30,
        stdin_text=f"{clean}\n",
    )
    _CAPABILITY_CACHE.clear()
    _QUEUE_WAKE.set()
    result = capability(refresh=True)
    if not result.get("enabled"):
        raise BaiduPanError(result.get("reason") or "授权验证失败")
    return result


def logout_web_auth() -> None:
    with _jobs_db() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM baidu_pan_jobs WHERE status IN (%s)" % ",".join("?" * len(_ACTIVE)),
            tuple(_ACTIVE),
        ).fetchone()[0]
    if active:
        raise ValueError(f"仍有 {active} 个活跃任务，请先取消后再解除授权")
    _run_cli(["logout"], timeout=30)
    _CAPABILITY_CACHE.clear()


_PENDING_PASSWORDS: dict[str, tuple[str, float]] = {}
_DIRECT_PATHS: dict[str, str] = {}
_CANCEL_REQUESTS: set[str] = set()
_MEMORY_LOCK = threading.Lock()
_TRANSFER_LOCK = threading.Lock()
_QUEUE_WAKE = threading.Event()
_WORKERS_LOCK = threading.Lock()
_WORKERS_STARTED = False


def _reset_workers_for_tests() -> None:
    """测试辅助：worker 为进程级单例，测试切换任务 DB 后唤醒现有线程即可。"""
    _QUEUE_WAKE.set()


def _public_username(username: str) -> str:
    return str(username or "__local__")


def _active_for_user(conn: sqlite3.Connection, username: str, *, exclude_id: str = "") -> bool:
    placeholders = ",".join("?" * len(_ACTIVE))
    sql = f"SELECT 1 FROM baidu_pan_jobs WHERE username=? AND status IN ({placeholders})"
    params: list[object] = [username, *_ACTIVE]
    if exclude_id:
        sql += " AND id<>?"
        params.append(exclude_id)
    return conn.execute(sql + " LIMIT 1", params).fetchone() is not None


def _base_job(username: str, is_admin: bool) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "username": _public_username(username),
        "is_admin": 1 if is_admin else 0,
        "db_path": str(db.current_db_path()),
        "multiuser_context": 1 if db.current_user_root() is not None else 0,
        "uploads_root": str(user_assets.current_uploads_root()),
        "created_at": _now(), "updated_at": _now(),
    }


def _insert_job(values: dict) -> dict:
    cols = list(values)
    with _jobs_db() as conn:
        if _active_for_user(conn, values["username"]):
            raise BaiduPanBusyError("你已有一个进行中的网盘任务，请等待完成或先取消")
        conn.execute(
            f"INSERT INTO baidu_pan_jobs ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
            [values[c] for c in cols],
        )
    _ensure_workers()
    _QUEUE_WAKE.set()
    return get_import_status(values["id"], username=values["username"])


def start_import(share_link: str, pwd: str, *, username: str, is_admin: bool = False) -> dict:
    """分享链接只做格式校验后入队；轮到执行时才转存与校验文件。"""
    url, secret = parse_share_link(share_link, pwd)
    job = _base_job(username, is_admin)
    job.update({"source_type": "share", "source_ref": url, "status": "queued" if secret else "waiting_password"})
    if secret:
        with _MEMORY_LOCK:
            _PENDING_PASSWORDS[job["id"]] = (secret, time.time())
    return _insert_job(job)


def start_drive_import(item: dict, *, username: str, is_admin: bool) -> dict:
    if not is_admin and username:
        raise PermissionError("admin only")
    # API 传入的是已经标准化的前端 item；再次走 _normalise_item 会因字段名
    # file_id/is_dir 不同丢失真实 fs_id，导致 worker 无法重新定位文件。
    if "file_id" in item:
        name = str(item.get("name") or "")
        size = int(item.get("size") or 0)
        is_dir = bool(item.get("is_dir", False))
        selectable = not is_dir and Path(name).suffix.lower() in _SUPPORTED_EXTS and 0 < size <= _max_bytes()
        normal = {
            "file_id": str(item.get("file_id") or ""),
            "name": name,
            "path": _safe_remote_path(str(item.get("path") or "")),
            "is_dir": is_dir,
            "size": size,
            "mtime": _normalise_mtime(item.get("mtime")),
            "selectable": selectable,
            "disabled_reason": str(item.get("disabled_reason") or ""),
        }
    else:
        normal = _normalise_item(item)
    if normal["is_dir"] or not normal["selectable"]:
        raise ValueError(normal["disabled_reason"] or "请选择单个受支持文件")
    job = _base_job(username, is_admin)
    job.update({
        "source_type": "drive", "source_ref": normal["file_id"],
        "filename": normal["name"], "size": normal["size"], "mtime": normal["mtime"],
        "status": "queued", "quota_day": _day_key(),
    })
    with _MEMORY_LOCK:
        _DIRECT_PATHS[job["id"]] = str(normal["path"])
    if not is_admin:
        _reserve_quota(job["username"], normal["size"], job["id"])
        job["quota_reserved"] = normal["size"]
    return _insert_job(job)


def supply_password(job_id: str, pwd: str, *, username: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9]{4}", str(pwd or "")):
        raise ValueError("提取码应为 4 位字母或数字")
    job = _get_owned_job(job_id, username)
    if job["source_type"] != "share" or job["status"] not in {"waiting_password", "failed", "waiting_quota"}:
        raise ValueError("当前任务不需要补充提取码")
    with _MEMORY_LOCK:
        _PENDING_PASSWORDS[job_id] = (pwd, time.time())
    _update_job(job_id, status="queued", error="", finished_at=None)
    _QUEUE_WAKE.set()
    return get_import_status(job_id, username=username)


def retry_import(job_id: str, *, username: str, pwd: str = "") -> dict:
    old = _get_owned_job(job_id, username)
    if old["status"] not in _TERMINAL:
        raise ValueError("只有已结束的任务可以重试")
    if old["source_type"] == "share":
        result = start_import(old["source_ref"], pwd, username=username, is_admin=bool(old["is_admin"]))
    else:
        path = _locate_drive_item(old)
        result = start_drive_import(path, username=username, is_admin=bool(old["is_admin"]))
    _update_job(result["job_id"], retry_of=job_id)
    return get_import_status(result["job_id"], username=username)


def cancel_import(job_id: str, *, username: str) -> dict:
    job = _get_owned_job(job_id, username)
    if job["status"] in _TERMINAL:
        return _serialise_job(job)
    if job["status"] == "processing":
        raise ValueError("课程数据处理中，已不能取消")
    with _MEMORY_LOCK:
        _CANCEL_REQUESTS.add(job_id)
        _PENDING_PASSWORDS.pop(job_id, None)
        _DIRECT_PATHS.pop(job_id, None)
    if job["status"] in _WAITING:
        _release_reserved_quota(job)
        _update_job(job_id, status="cancelled", error="用户取消", finished_at=_now())
        _cleanup_job_files(job)
    else:
        with _ACTIVE_PROCESSES_LOCK:
            proc = _ACTIVE_PROCESSES.get(job_id)
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.terminate()
    _QUEUE_WAKE.set()
    return get_import_status(job_id, username=username)


def get_import_status(import_id: str, *, username: str) -> dict:
    return _serialise_job(_get_owned_job(import_id, username))


def list_imports(*, username: str, is_admin: bool = False, limit: int = 50) -> list[dict]:
    limit = max(1, min(100, int(limit)))
    with _jobs_db() as conn:
        if is_admin:
            rows = conn.execute("SELECT * FROM baidu_pan_jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM baidu_pan_jobs WHERE username=? ORDER BY created_at DESC LIMIT ?",
                (_public_username(username), limit),
            ).fetchall()
    return [_serialise_job(dict(r), admin_view=is_admin) for r in rows]


def _get_owned_job(job_id: str, username: str) -> dict:
    with _jobs_db() as conn:
        row = conn.execute("SELECT * FROM baidu_pan_jobs WHERE id=?", (str(job_id),)).fetchone()
    if not row or row["username"] != _public_username(username):
        raise ValueError("Import not found")
    return dict(row)


def _serialise_job(job: dict, *, admin_view: bool = False) -> dict:
    status = job.get("status", "")
    size = int(job.get("size") or 0)
    progress = min(size, int(job.get("progress_bytes") or 0)) if size else int(job.get("progress_bytes") or 0)
    result = {
        "job_id": job.get("id") or job.get("job_id"), "status": status,
        "filename": job.get("filename") or "", "size": size,
        "progress_bytes": progress, "progress_percent": round(progress * 100 / size, 1) if size else None,
        "error": job.get("error") or "", "file_kind": job.get("file_kind") or "",
        "local_path": job.get("local_path") or "",
        "duration_seconds": float(job.get("duration_seconds") or 0),
        "media_kind": job.get("media_kind") or "", "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"), "retry_of": job.get("retry_of") or "",
    }
    if job.get("upload_id"):
        result["upload_id"] = job["upload_id"]
    try:
        result["quote"] = json.loads(job.get("quote_json") or "{}") or None
    except Exception:
        result["quote"] = None
    if status == "queued":
        result["queue_position"] = _queue_position(str(result["job_id"]))
    if admin_view:
        result["username"] = "local" if job.get("username") == "__local__" else job.get("username")
        result["source_type"] = job.get("source_type")
        result["source_summary"] = _source_summary(job)
    return result


def _source_summary(job: dict) -> str:
    if job.get("source_type") == "drive":
        return str(job.get("filename") or "网盘文件")
    ref = str(job.get("source_ref") or "")
    return "pan.baidu.com/s/…" + ref[-4:] if ref else "分享链接"


def _queue_position(job_id: str) -> int:
    with _jobs_db() as conn:
        row = conn.execute("SELECT created_at FROM baidu_pan_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return 0
        return int(conn.execute(
            "SELECT COUNT(*) FROM baidu_pan_jobs WHERE status='queued' AND created_at<=?",
            (row["created_at"],),
        ).fetchone()[0])


def _update_job(job_id: str, **changes) -> None:
    if not changes:
        return
    changes["updated_at"] = _now()
    with _jobs_db() as conn:
        conn.execute(
            "UPDATE baidu_pan_jobs SET " + ",".join(f"{k}=?" for k in changes) + " WHERE id=?",
            [*changes.values(), job_id],
        )


def _reserve_quota(username: str, size: int, job_id: str) -> None:
    day = _day_key()
    with _jobs_db() as conn:
        row = conn.execute(
            "SELECT reserved_bytes,used_bytes FROM baidu_pan_daily_usage WHERE username=? AND day_key=?",
            (username, day),
        ).fetchone()
        reserved, used = (int(row[0]), int(row[1])) if row else (0, 0)
        if reserved + used + size > _daily_limit_bytes():
            raise BaiduPanBusyError("今日百度网盘导入流量已达 3GB，请明日再试")
        conn.execute(
            "INSERT INTO baidu_pan_daily_usage(username,day_key,reserved_bytes,used_bytes) VALUES(?,?,?,0) "
            "ON CONFLICT(username,day_key) DO UPDATE SET reserved_bytes=reserved_bytes+excluded.reserved_bytes",
            (username, day, size),
        )


def _release_reserved_quota(job: dict) -> None:
    amount = int(job.get("quota_reserved") or 0)
    if not amount or job.get("quota_counted"):
        return
    with _jobs_db() as conn:
        conn.execute(
            "UPDATE baidu_pan_daily_usage SET reserved_bytes=MAX(0,reserved_bytes-?) "
            "WHERE username=? AND day_key=?",
            (amount, job["username"], job.get("quota_day") or _day_key()),
        )
        conn.execute("UPDATE baidu_pan_jobs SET quota_reserved=0 WHERE id=?", (job["id"],))


def _count_quota_started(job: dict) -> None:
    amount = int(job.get("quota_reserved") or job.get("size") or 0)
    if not amount or job.get("is_admin") or job.get("quota_counted"):
        return
    day = job.get("quota_day") or _day_key()
    with _jobs_db() as conn:
        conn.execute(
            "INSERT INTO baidu_pan_daily_usage(username,day_key,reserved_bytes,used_bytes) VALUES(?,?,0,?) "
            "ON CONFLICT(username,day_key) DO UPDATE SET "
            "reserved_bytes=MAX(0,reserved_bytes-?),used_bytes=used_bytes+excluded.used_bytes",
            (job["username"], day, amount, amount),
        )
        conn.execute("UPDATE baidu_pan_jobs SET quota_reserved=0,quota_counted=1 WHERE id=?", (job["id"],))


def _ensure_quota(job: dict, size: int) -> bool:
    if job.get("is_admin"):
        return True
    try:
        _reserve_quota(job["username"], size, job["id"])
    except BaiduPanBusyError:
        _update_job(job["id"], status="waiting_quota", error="等待次日流量额度", quota_reserved=0)
        return False
    _update_job(job["id"], quota_day=_day_key(), quota_reserved=size)
    job["quota_day"], job["quota_reserved"] = _day_key(), size
    return True


def _enough_space(root: Path, size: int) -> bool:
    try:
        return shutil.disk_usage(root).free >= size + 2 * 1024**3
    except OSError:
        return False


def _cancel_requested(job_id: str) -> bool:
    with _MEMORY_LOCK:
        return job_id in _CANCEL_REQUESTS


def _ensure_workers() -> None:
    global _WORKERS_STARTED
    with _WORKERS_LOCK:
        if _WORKERS_STARTED:
            return
        _WORKERS_STARTED = True
        for index in range(_download_concurrency()):
            threading.Thread(target=_worker_loop, daemon=True, name=f"baidu-pan-worker-{index + 1}").start()


def _worker_loop() -> None:
    while True:
        job = _claim_next_job()
        if job is None:
            _QUEUE_WAKE.wait(2)
            _QUEUE_WAKE.clear()
            _requeue_waiting_jobs()
            continue
        _run_job(job)


def _claim_next_job() -> dict | None:
    with _jobs_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM baidu_pan_jobs WHERE status='queued' ORDER BY created_at,id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        changed = conn.execute(
            "UPDATE baidu_pan_jobs SET status='transferring',started_at=COALESCE(started_at,?),updated_at=? "
            "WHERE id=? AND status='queued'", (_now(), _now(), row["id"]),
        ).rowcount
        return dict(row) if changed else None


def _requeue_waiting_jobs() -> None:
    cap = capability()
    now_ts = time.time()
    with _jobs_db() as conn:
        if cap.get("enabled"):
            conn.execute("UPDATE baidu_pan_jobs SET status='queued',error='',updated_at=? WHERE status='waiting_auth'", (_now(),))
        rows = conn.execute(
            "SELECT * FROM baidu_pan_jobs WHERE status IN ('waiting_password','waiting_quota','waiting_space')"
        ).fetchall()
        for row in rows:
            job = dict(row)
            age = now_ts - dt.datetime.fromisoformat(job["updated_at"]).timestamp()
            if job["status"] in {"waiting_password", "waiting_auth"} and age > 86400:
                conn.execute(
                    "UPDATE baidu_pan_jobs SET status='failed',error=?,finished_at=?,updated_at=? WHERE id=?",
                    ("等待配置超过 24 小时，请点击重试", _now(), _now(), job["id"]),
                )
            elif job["status"] == "waiting_quota" and (job.get("quota_day") or "") != _day_key():
                with _MEMORY_LOCK:
                    has_secret = job["source_type"] != "share" or job["id"] in _PENDING_PASSWORDS
                conn.execute(
                    "UPDATE baidu_pan_jobs SET status=?,error='',updated_at=? WHERE id=?",
                    ("queued" if has_secret else "waiting_password", _now(), job["id"]),
                )
            elif job["status"] == "waiting_space" and _enough_space(Path(job["uploads_root"]), int(job["size"] or 0)):
                conn.execute("UPDATE baidu_pan_jobs SET status='queued',error='',updated_at=? WHERE id=?", (_now(), job["id"]),)


def _run_job(job: dict) -> None:
    token = None
    if job.get("multiuser_context"):
        token = db.set_current_db_path(Path(job["db_path"]))
    _WORKER_LOCAL.job_id = job["id"]
    remote_path = ""
    remote_temp_dir = ""
    folder = Path(job["uploads_root"]) / uuid.uuid4().hex
    try:
        if _cancel_requested(job["id"]):
            raise BaiduPanCancelled("任务已取消")
        if not capability().get("enabled"):
            _update_job(job["id"], status="waiting_auth", error="等待管理员重新授权")
            return
        if job["source_type"] == "share":
            with _MEMORY_LOCK:
                secret_info = _PENDING_PASSWORDS.get(job["id"])
            if not secret_info:
                _update_job(job["id"], status="waiting_password", error="请重新输入提取码")
                return
            target_dir = f"echolingo-imports/{job['id']}"
            # 即使转存结果校验失败，也能清理整个任务目录。
            remote_temp_dir = f"/apps/bdpan/{target_dir}"
            with _TRANSFER_LOCK:
                files = transfer_share(job["source_ref"], secret_info[0], target_dir)
            item = _validate_single_item(files)
            normal = _normalise_item(item)
            remote_path = normal["path"]
            relative_parent = str(PurePosixPath(remote_path).parent).strip("./")
            remote_temp_dir = "/apps/bdpan" + (f"/{relative_parent}" if relative_parent else "")
            _update_job(job["id"], filename=normal["name"], size=normal["size"], mtime=normal["mtime"],
                        remote_temp_dir=remote_temp_dir)
            job.update(filename=normal["name"], size=normal["size"], mtime=normal["mtime"],
                       remote_temp_dir=remote_temp_dir)
        else:
            item = _locate_drive_item(job)
            # `_locate_drive_item` returns the public, already-normalised shape.
            # Normalising it again would discard `file_id` (the raw CLI uses
            # `fs_id`) and replace it with a path hash, making every direct
            # drive import look as if the selected file had changed.
            normal = item
            _verify_unchanged(job, normal)
            remote_path = normal["path"]
        size = int(normal["size"])
        if not job.get("is_admin") and not int(job.get("quota_reserved") or 0):
            if not _ensure_quota(job, size):
                return
        if not _enough_space(Path(job["uploads_root"]), size):
            _release_reserved_quota(job)
            _update_job(job["id"], status="waiting_space", error="磁盘空间不足：需保留文件大小 + 2GB")
            return
        if _cancel_requested(job["id"]):
            raise BaiduPanCancelled("任务已取消")
        _count_quota_started(job)
        _update_job(job["id"], status="downloading", error="")
        folder.mkdir(parents=True, exist_ok=True)
        name = str(normal["name"])
        safe_name = (Path(name.replace("\\", "/")).name or "reading.txt") if Path(name).suffix.lower() in _TEXT_EXTS else _safe_upload_filename(name)
        local_path = folder / safe_name
        download_file(remote_path, local_path)
        if _cancel_requested(job["id"]):
            raise BaiduPanCancelled("任务已取消")
        _update_job(job["id"], status="processing", progress_bytes=size)
        if Path(name).suffix.lower() in _TEXT_EXTS:
            _update_job(job["id"], status="ready", file_kind="text", local_path=str(local_path),
                        finished_at=_now())
        else:
            duration, media_kind = _probe_uploaded_media(local_path)
            upload_id = folder.name
            record = db.create_v2_media_upload(
                upload_id, name, f"{upload_id}/{safe_name}", media_kind, size, duration,
            )
            _update_job(
                job["id"], status="ready", file_kind="media", upload_id=record["id"],
                duration_seconds=duration, media_kind=media_kind,
                quote_json=json.dumps(media_upload_quote(duration), ensure_ascii=False), finished_at=_now(),
            )
    except BaiduPanCancelled:
        shutil.rmtree(folder, ignore_errors=True)
        _release_reserved_quota(job)
        _update_job(job["id"], status="cancelled", error="用户取消", finished_at=_now())
    except Exception as exc:
        shutil.rmtree(folder, ignore_errors=True)
        _release_reserved_quota(job)
        _update_job(job["id"], status="failed", error=friendly_message(exc), finished_at=_now())
    finally:
        if remote_temp_dir:
            with contextlib.suppress(Exception):
                remove_remote(remote_temp_dir)
        with _MEMORY_LOCK:
            _DIRECT_PATHS.pop(job["id"], None)
            _PENDING_PASSWORDS.pop(job["id"], None)
            _CANCEL_REQUESTS.discard(job["id"])
        _WORKER_LOCAL.job_id = ""
        if token is not None:
            db.reset_current_db_path(token)
        _QUEUE_WAKE.set()


def _validate_single_item(files: list[dict]) -> dict:
    file_items = [item for item in files if not bool(item.get("is_dir") or item.get("isdir"))]
    if len(files) != 1 or len(file_items) != 1:
        raise ValueError("仅支持单文件分享：请不要分享文件夹或多选文件")
    normal = _normalise_item(file_items[0])
    if not normal["selectable"]:
        raise ValueError(normal["disabled_reason"] or "请选择单个受支持文件")
    return file_items[0]


def _locate_drive_item(job: dict) -> dict:
    with _MEMORY_LOCK:
        path = _DIRECT_PATHS.get(job["id"])
    if path:
        # `bdpan ls <文件>` 返回文件自身，能精确复核 fs_id/大小/mtime；
        # 对深层目录先列 parent 既慢又可能因分页漏掉目标文件。
        direct = _normalise_allowed_items(
            _parse_list_payload(_run_cli(["ls", path, "--json"], timeout=30)))
        if direct:
            # 精确文件路径只可能返回该文件自身；统一由 _verify_unchanged
            # 校验 ID/大小/mtime，避免同名全局搜索掩盖变化。
            return direct[0]
    results = search_drive(str(job.get("filename") or job["source_ref"]), page_size=50)["items"]
    for item in results:
        if item["file_id"] == str(job["source_ref"]):
            return item
    raise ValueError("网盘文件已移动或删除，请重新选择")


def _verify_unchanged(job: dict, item: dict) -> None:
    if str(item["file_id"]) != str(job["source_ref"]):
        raise ValueError("网盘文件 ID 已变化，请重新选择")
    if int(job.get("size") or 0) and int(item["size"]) != int(job["size"]):
        raise ValueError("网盘文件大小已变化，请重新选择")
    if job.get("mtime") and item.get("mtime") and str(item["mtime"]) != str(job["mtime"]):
        raise ValueError("网盘文件修改时间已变化，请重新选择")


def _cleanup_job_files(job: dict) -> None:
    local = str(job.get("local_path") or "")
    if local:
        shutil.rmtree(Path(local).parent, ignore_errors=True)
    remote = str(job.get("remote_temp_dir") or "")
    if remote:
        with contextlib.suppress(Exception):
            remove_remote(remote)


# 旧测试辅助接口：轮询持久化状态。
def _wait_job(import_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _jobs_db() as conn:
            row = conn.execute("SELECT * FROM baidu_pan_jobs WHERE id=?", (import_id,)).fetchone()
        if row and row["status"] in _TERMINAL:
            return _serialise_job(dict(row))
        time.sleep(0.05)
    raise TimeoutError(f"job {import_id} 未在 {timeout}s 内结束")


def _wait_job_idle() -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with _jobs_db() as conn:
            active = conn.execute(
                "SELECT COUNT(*) FROM baidu_pan_jobs WHERE status NOT IN ('ready','failed','cancelled')"
            ).fetchone()[0]
        if not active:
            return
        time.sleep(0.05)
