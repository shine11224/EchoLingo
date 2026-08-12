"""百度网盘分享链接导入（v1 单服务器账号）：转存 → 下载 → 注册为媒体上传记录。

用户粘贴分享链接 → bdpan transfer select 转存到服务器账号沙箱
→ bdpan download 下载到当前用户 uploads 目录 → ffprobe 校验
→ db.create_v2_media_upload 注册 → 用户走现有 uploaded_media 建课链路。
下载结束（成功或失败）都尽力删除网盘转存副本。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path, PurePosixPath

import db
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
_MEDIA_EXTS = {".mp3", ".mp4", ".m4a", ".wav", ".mov"}
_TEXT_EXTS = {".txt", ".md", ".doc", ".docx", ".pdf"}

_ERRNO_MESSAGES = {
    "-9": "提取码错误，请核对后重试",
    "-7": "分享链接已删除或取消",
    "-8": "分享链接已过期，请让分享者重新生成",
    "-32": "服务器网盘空间不足，请联系管理员",
    "-10": "服务器网盘空间不足，请联系管理员",
    "13045": "不能转存自己分享的文件，请确认分享链接来自他人（或换一个网盘账号的分享）",
    "13077": "服务器网盘空间不足，请联系管理员",
    "13041": "转存失败：文件不属于该分享链接",
}


class BaiduPanError(RuntimeError):
    """网盘 CLI 层错误，message 为原始输出。"""


class BaiduPanBusyError(RuntimeError):
    """导入排队超限。"""


def parse_share_link(raw: str, pwd: str) -> tuple[str, str]:
    """校验并规范化分享链接；返回 (不带 pwd 的链接, 提取码)。链接内 pwd 优先。"""
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
        return int(os.environ.get("ELT_BAIDU_PAN_MAX_MB", "500")) * 1024 * 1024
    except ValueError:
        return 500 * 1024 * 1024


def _bin() -> str:
    return os.environ.get("ELT_BAIDU_PAN_BIN") or shutil.which("bdpan") or ""


def capability() -> dict:
    """前端据此决定是否展示网盘导入入口；结果缓存 60s（whoami 有一次进程开销）。"""
    if os.environ.get("ELT_BAIDU_PAN_ENABLED", "1") != "1":
        return {"enabled": False, "reason": "disabled"}
    now = time.monotonic()
    cached = _CAPABILITY_CACHE.get("value")
    if cached and now - _CAPABILITY_CACHE["at"] < 60:
        return cached
    result = _probe_capability()
    _CAPABILITY_CACHE.update({"value": result, "at": now})
    return result


_CAPABILITY_CACHE: dict = {}


def _probe_capability() -> dict:
    if not _bin():
        return {"enabled": False, "reason": "bdpan 未安装"}
    try:
        out = _run_cli(["whoami", "--json"], timeout=15)
        info = json.loads(out)
        if info.get("authenticated") and info.get("has_valid_token"):
            return {"enabled": True}
        return {"enabled": False, "reason": "bdpan 未登录或 token 失效"}
    except Exception as e:
        return {"enabled": False, "reason": friendly_message(e)}


# ── bdpan CLI 子进程层 ─────────────────────────────────────────


def _run_cli(args: list[str], *, timeout: int) -> str:
    """执行 bdpan 子命令；非零退出/超时 → BaiduPanError（保留原始输出供 errno 映射）。"""
    binary = _bin()
    if not binary:
        raise BaiduPanError("bdpan 未安装")
    try:
        proc = subprocess.run(
            [binary, *args],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise BaiduPanError(f"网盘操作超时（{timeout}s）")
    output = f"{proc.stdout or ''}\n{proc.stderr or ''}".strip()
    if proc.returncode != 0:
        raise BaiduPanError(output or f"bdpan 退出码 {proc.returncode}")
    return proc.stdout or ""


def transfer_share(url: str, pwd: str, target_dir: str) -> list[dict]:
    """转存整个分享链接到 target_dir（相对应用根目录），返回文件列表。

    本版 bdpan CLI 无 transfer list/select 子命令，`transfer --json` 一步完成
    转存并返回 [{name, path, size, is_dir}]，path 为网盘内绝对路径。
    """
    args = ["transfer", url, "-d", target_dir, "--json"]
    if pwd:
        args += ["-p", pwd]
    out = _run_cli(args, timeout=180)
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise BaiduPanError(f"转存结果解析失败：{out[:200]}")
    # bdpan 3.8.x 失败时退出码仍为 0，输出失败信封 {"code":1,"error":"...","data":null}；
    # 不识别会把真实错误吞掉，误报「仅支持单文件分享」（2026-08-12 云端实测）。
    if not isinstance(data, dict):
        raise BaiduPanError(f"转存结果解析失败：{out[:200]}")
    if data.get("error") or data.get("code") not in (None, 0):
        raise BaiduPanError(str(data.get("error") or f"网盘转存失败（code={data.get('code')}）"))
    return list(data.get("files") or [])


def download_file(remote_path: str, local_path: Path, *, timeout: int = 1800) -> None:
    local_path = Path(local_path)
    _run_cli(["download", remote_path, str(local_path)], timeout=timeout)
    # bdpan 3.8.x 失败也可能退出 0（仅打印 Error + usage），须以下载产物校验
    if not local_path.exists() or local_path.stat().st_size == 0:
        raise BaiduPanError(f"下载失败或产物为空：{remote_path}")


def remove_remote(remote_path: str) -> None:
    # -f：CLI 默认交互确认，非交互环境下会挂起到超时
    _run_cli(["rm", "-f", remote_path], timeout=60)


# ── import job 状态机（镜像 _READING_UPLOAD_JOBS 模式） ──────────

_IMPORT_JOBS: dict[str, dict] = {}
_IMPORT_JOBS_LOCK = threading.Lock()
_IMPORT_JOB_LIMIT = 5
_TRANSFER_LOCK = threading.Lock()  # 单账号：同时只允许一个转存+下载


def _set_job(import_id: str, **changes) -> dict:
    with _IMPORT_JOBS_LOCK:
        job = _IMPORT_JOBS.get(import_id, {"job_id": import_id})
        job.update(changes)
        _IMPORT_JOBS[import_id] = job
        return dict(job)


def get_import_status(import_id: str, *, username: str) -> dict:
    with _IMPORT_JOBS_LOCK:
        job = dict(_IMPORT_JOBS.get(str(import_id)) or {})
    if not job or job.get("username") != username:
        raise ValueError("Import not found")
    job.pop("username", None)
    return job


def start_import(share_link: str, pwd: str, *, username: str) -> dict:
    """同步转存+校验（链接/单文件/类型/大小）→ 创建 job → 后台线程下载。

    转存是网盘服务端秒级复制，放在请求路径内同步完成，可从返回的文件列表
    直接校验；校验失败时尽力删除已转存副本。
    """
    if not capability().get("enabled"):
        raise ValueError(f"网盘导入不可用（{capability().get('reason', '未知原因')}）")
    url, pwd = parse_share_link(share_link, pwd)

    with _IMPORT_JOBS_LOCK:
        active = [j for j in _IMPORT_JOBS.values() if j.get("status") not in {"ready", "failed"}]
        if len(active) >= _IMPORT_JOB_LIMIT:
            raise BaiduPanBusyError("网盘导入排队中，请稍后重试")

    import_id = uuid.uuid4().hex
    transfer_dir = f"echolingo-imports/{import_id}"
    with _TRANSFER_LOCK:
        files = transfer_share(url, pwd, transfer_dir)

    file_items = [item for item in files if not item.get("is_dir")]
    item = file_items[0] if len(file_items) == 1 else None
    error: str | None = None
    if len(files) != 1 or item is None:
        error = "v1 仅支持单文件分享：请分享单个音视频或文本文件（不要选文件夹或多选）"
    else:
        name = str(item.get("name") or "")
        suffix = Path(name).suffix.lower()
        if suffix not in _MEDIA_EXTS | _TEXT_EXTS:
            error = (
                f"仅支持音视频或文本文件（{ '/'.join(sorted(_MEDIA_EXTS | _TEXT_EXTS)) }）：{name}"
            )
        else:
            size = int(item.get("size") or 0)
            if size <= 0:
                error = "无法读取分享文件大小"
            elif size > _max_bytes():
                error = f"文件超过大小限制 {_max_bytes() // (1024 * 1024)} MB"
    if error is not None:
        _cleanup_remote(files, transfer_dir)
        raise ValueError(error)

    name = str(item["name"])
    size = int(item["size"])
    remote_path = str(item["path"])
    remote_dir = str(PurePosixPath(remote_path).parent)
    with _IMPORT_JOBS_LOCK:
        active = [j for j in _IMPORT_JOBS.values() if j.get("status") not in {"ready", "failed"}]
        if len(active) >= _IMPORT_JOB_LIMIT:
            _cleanup_remote(files, transfer_dir)
            raise BaiduPanBusyError("网盘导入排队中，请稍后重试")
        terminal = [k for k, j in _IMPORT_JOBS.items() if j.get("status") in {"ready", "failed"}]
        while len(_IMPORT_JOBS) >= 50 and terminal:
            _IMPORT_JOBS.pop(terminal.pop(0), None)
        job = {
            "job_id": import_id, "username": username, "status": "queued",
            "filename": name, "size": size,
        }
        _IMPORT_JOBS[import_id] = job
    db.spawn_with_db_context(
        _run_import, import_id, remote_path, remote_dir, name, size, username,
        name=f"baidu-pan-{import_id[:8]}",
    )
    return {k: v for k, v in job.items() if k != "username"}


def _cleanup_remote(files: list[dict], transfer_dir: str) -> None:
    """校验失败时尽力删除已转存副本。"""
    remote_dir = _remote_dir_of(files, transfer_dir)
    try:
        remove_remote(remote_dir)
    except Exception:
        pass


def _remote_dir_of(files: list[dict], transfer_dir: str) -> str:
    for item in files:
        path = str(item.get("path") or "")
        if path:
            return str(PurePosixPath(path).parent)
    return f"/apps/bdpan/{transfer_dir}"


def _wait_job(import_id: str, timeout: float = 30.0) -> dict:
    """测试辅助：轮询直到 terminal。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _IMPORT_JOBS_LOCK:
            job = dict(_IMPORT_JOBS.get(import_id) or {})
        if job.get("status") in {"ready", "failed"}:
            return job
        time.sleep(0.05)
    raise TimeoutError(f"job {import_id} 未在 {timeout}s 内结束")


def _wait_job_idle() -> None:
    """测试辅助：等所有 job 到 terminal。"""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with _IMPORT_JOBS_LOCK:
            if all(j.get("status") in {"ready", "failed"} for j in _IMPORT_JOBS.values()):
                return
        time.sleep(0.05)


def _run_import(import_id: str, remote_path: str, remote_dir: str,
                name: str, size: int, username: str) -> None:
    upload_id = uuid.uuid4().hex
    folder = user_assets.current_uploads_root() / upload_id
    try:
        with _TRANSFER_LOCK:
            _set_job(import_id, status="downloading")
            folder.mkdir(parents=True, exist_ok=True)
            if Path(name).suffix.lower() in _TEXT_EXTS:
                # 文本类不走 _safe_upload_filename 的媒体扩展名白名单，仅剥离路径成分
                safe_name = Path(name.replace("\\", "/")).name or "reading.txt"
            else:
                safe_name = _safe_upload_filename(name)
            local_path = folder / safe_name
            download_file(remote_path, local_path)
        if Path(name).suffix.lower() in _TEXT_EXTS:
            # 文本类（txt/md/docx/pdf）：无需 ffprobe 与媒体注册，
            # ready 后由前端按 local_path 走 reading_file 建课。
            _set_job(
                import_id, status="ready",
                file_kind="text", local_path=str(local_path),
            )
            return
        duration, media_kind = _probe_uploaded_media(local_path)
        record = db.create_v2_media_upload(
            upload_id, name, f"{upload_id}/{safe_name}", media_kind, size, duration,
        )
        _set_job(
            import_id, status="ready",
            file_kind="media",
            upload_id=record["id"],
            duration_seconds=duration, media_kind=media_kind,
            quote=media_upload_quote(duration),
        )
    except Exception as e:
        shutil.rmtree(folder, ignore_errors=True)
        _set_job(import_id, status="failed", error=friendly_message(e))
    finally:
        try:
            remove_remote(remote_dir)
        except Exception:
            pass  # 网盘残留副本下次同名目录覆盖，尽力清理即可
