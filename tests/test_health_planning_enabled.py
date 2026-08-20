"""健康检查的 planning_enabled 信号。

个人档案页（/planning）仅私有库/云端注册；公开库缺少
webapp.fastapi_routes.planning 模块，/health 需明确返回 False，
前端据此隐藏 tab 入口（fail-visible：字段缺失时保持显示，
避免私有单用户模式被误隐藏）。
"""
import importlib
import sys
from pathlib import Path

from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _build_app(tmp_path, monkeypatch):
    monkeypatch.delenv("ELT_AUTH_ENABLED", raising=False)
    import db as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "default" / "vocab.db")
    db_mod._initialized_paths.clear()
    import fastapi_server
    importlib.reload(fastapi_server)
    return fastapi_server.create_app()


def test_health_reports_planning_enabled_when_module_present(tmp_path, monkeypatch):
    client = TestClient(_build_app(tmp_path, monkeypatch))
    assert client.get("/health").json()["planning_enabled"] is True


def test_health_reports_planning_disabled_when_module_missing(tmp_path, monkeypatch):
    client = TestClient(_build_app(tmp_path, monkeypatch))
    # 模拟公开库：模块不在包属性里，且 sys.modules 置 None 使导入抛 ImportError
    import webapp.fastapi_routes as routes_pkg
    monkeypatch.delattr(routes_pkg, "planning", raising=False)
    monkeypatch.setitem(sys.modules, "webapp.fastapi_routes.planning", None)
    assert client.get("/health").json()["planning_enabled"] is False
