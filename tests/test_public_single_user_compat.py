"""公开库/单用户兼容测试：私有模块（webapp.auth、auth/admin_private/credits 路由）
缺失时 fastapi_server 仍能以单用户模式启动。

模拟方式：在子进程中安装 meta_path blocker，对私有模块名抛 ImportError，
等价于公开库不含这些文件；然后 import fastapi_server 并 create_app。
该测试本身可同步到公开库（不 import 任何私有模块）。
"""
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

CHILD_SCRIPT = textwrap.dedent(
    """
    import importlib.abc
    import sys

    BLOCKED_PREFIXES = (
        "webapp.auth",
        "webapp.fastapi_routes.auth",
        "webapp.fastapi_routes.admin_private",
        "webapp.fastapi_routes.credits",
    )

    class _Blocker(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if any(fullname == p or fullname.startswith(p + ".") for p in BLOCKED_PREFIXES):
                raise ImportError(f"blocked private module: {fullname}")
            return None

    sys.meta_path.insert(0, _Blocker())
    sys.path.insert(0, "__BACKEND_DIR__")

    import fastapi_server

    assert fastapi_server.auth_router is None, "auth_router 应降级为 None"
    assert fastapi_server.admin_private_router is None, "admin_private_router 应降级为 None"
    assert fastapi_server.credits_router is None, "credits_router 应降级为 None"

    app = fastapi_server.create_app()
    paths = {route.path for route in app.routes}
    assert "/health" in paths, f"/health 缺失: {sorted(paths)}"
    assert not any(p.startswith("/api/auth") for p in paths), "公开库不应注册 auth 路由"
    assert not any(p.startswith("/api/credits") for p in paths), "公开库不应注册 credits 路由"

    from webapp.runtime import access, credit_meter
    from webapp.storage import user_assets

    assert access.multiuser_enabled() is False, "无 auth 模块时应为单用户模式"
    assert credit_meter.mode() == "off", f"无 auth 模块时计费应 off，实际 {credit_meter.mode()}"
    assert credit_meter.billing_active() is False

    from pathlib import Path
    root = user_assets.current_output_root(Path("output"))
    assert root == Path("output"), f"单用户 output 应回落全局目录，实际 {root}"

    print("PUBLIC_SINGLE_USER_COMPAT_OK")
    """
)


def test_fastapi_server_starts_single_user_without_private_modules():
    backend = str(REPO / "backend")
    script = CHILD_SCRIPT.replace("__BACKEND_DIR__", backend.replace("\\", "\\\\"))
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"单用户兼容子进程失败:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    assert "PUBLIC_SINGLE_USER_COMPAT_OK" in proc.stdout
