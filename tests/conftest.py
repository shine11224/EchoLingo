import os

# 测试套件默认禁用 Docling 子进程（torch 加载慢且本机才装）；
# 需要真实 Docling 的测试自行 monkeypatch 打开并跳过不可用环境。
os.environ.setdefault("ELT_DOCLING", "off")
