"""当前用户的媒体资产根目录解析。

多用户模式（认证中间件已设置 db contextvar）：
    resources/users/<username>/output 与 resources/users/<username>/uploads
单用户 / 公开库模式：
    仓库根 output/（或调用方传入的 fallback，保持模块级 OUTPUT_DIR 可被测试 monkeypatch）。

所有资产生产/读取模块必须通过这里解析路径，禁止直接拼接全局 OUTPUT_DIR；
/output/... URL 形式保持不变，由 output 路由按当前用户 root 二次解析。
共享模型/工具缓存（whisper-models、mfa-root、.cache/youtube）不走本模块，保持全局。
"""
from __future__ import annotations

from pathlib import Path

import db

BASE_DIR = Path(__file__).resolve().parents[3]
GLOBAL_OUTPUT_DIR = BASE_DIR / "output"


def is_multi_user_context() -> bool:
    """当前是否处于多用户请求/线程上下文（中间件已设置用户 DB）。"""
    return db.current_user_root() is not None


def current_scope_key() -> str:
    """进程内共享状态（进行中集合、inflight、job 表）的用户隔离键；单用户为 ''。

    容量类限制（信号量、job 上限）仍保持全局，本键只用于归属判断。
    """
    root = db.current_user_root()
    return str(root) if root is not None else ""


def current_output_root(fallback: Path | str | None = None) -> Path:
    """当前上下文应使用的 output 根目录；多用户时自动创建用户目录。"""
    user_root = db.current_user_root()
    if user_root is not None:
        root = user_root / "output"
        root.mkdir(parents=True, exist_ok=True)
        return root
    if fallback is not None:
        return Path(fallback)
    return GLOBAL_OUTPUT_DIR


def current_uploads_root() -> Path:
    """普通用户浏览器上传的暂存根目录（Task 3 上传 API 使用）。"""
    user_root = db.current_user_root()
    root = (user_root / "uploads") if user_root is not None else (GLOBAL_OUTPUT_DIR / "uploads")
    root.mkdir(parents=True, exist_ok=True)
    return root


def user_output_subdir(*parts: str, fallback: Path | str, create: bool = True) -> Path:
    """多用户：当前用户 output 下的子目录；单用户：原样返回 fallback。

    fallback 由各调用模块用自己的模块级 OUTPUT_DIR 拼接，保留既有测试
    monkeypatch 模块常量的兼容路径。
    """
    if db.current_user_root() is not None:
        path = current_output_root().joinpath(*parts)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path
    return Path(fallback)


def resolve_output_file(relpath: str, fallback: Path | str | None = None) -> Path | None:
    """把 /output/ 相对路径安全解析到当前用户 output root；越界或不存在返回 None。

    多用户模式下全局 output/ 与其他用户的文件都不可达（返回 None → 路由 404）。
    """
    if not relpath or "\x00" in relpath:
        return None
    root = current_output_root(fallback)
    candidate = (root / relpath).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate
