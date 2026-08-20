import os

# 测试套件默认禁用 Docling 子进程（torch 加载慢且本机才装）；
# 需要真实 Docling 的测试自行 monkeypatch 打开并跳过不可用环境。
os.environ.setdefault("ELT_DOCLING", "off")

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


@pytest.fixture(autouse=True)
def _reset_rate_limit_state():
    """每个测试前清零内存限流器：register/login 的 IP 限额是全局状态，
    不重置会让整套件里大量注册/登录用例在 10 次后集体 429。"""
    try:
        from webapp.runtime import rate_limit
    except ImportError:
        yield
        return
    rate_limit._reset_all()
    yield
    rate_limit._reset_all()
