"""elt_credits.js 公开库边界契约测试（Codex Task11 复审阻塞项修复）。

背景：共享模板（index/workspace/intensive/lesson/vocab.html）直接调用
`eltCredits.billableFetch`。若 elt_credits.js 被当作私有资产排除出公开库，
公开库用户点击相关动作会抛 ReferenceError——404 不是无害失败。

三层契约：
1. 两份 sync-to-public skill 文档（.claude 与 .agents）不得再把 elt_credits.js
   列为排除项，且必须说明其必须同步的原因；
2. 每个直接调用 eltCredits.* 的共享模板都必须先引入 /static/elt_credits.js；
3. 行为证明：credits API 不存在（rates 404）时，billableFetch 仍按原样调用
   目标 API（以 workspace.html 中 lesson_chat 的真实调用形状为代表），
   且 rates() 静默返回 null。用 Node vm 沙箱执行真实 elt_credits.js。

本测试不 import 任何私有模块，可同步到公开库。
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
JS_PATH = REPO / "frontend" / "static" / "elt_credits.js"
SKILL_CLAUDE = REPO / ".claude" / "skills" / "sync-to-public" / "SKILL.md"
SKILL_AGENTS = REPO / ".agents" / "skills" / "sync-to-public" / "SKILL.md"

SHARED_TEMPLATES = [
    REPO / "frontend" / "templates" / name
    for name in ("index.html", "workspace.html", "intensive.html",
                 "lesson.html", "vocab.html")
]


def _existing_skill_copies(candidates):
    """返回实际存在的 skill 副本（公开库两份都不存在时为空，属正常状态）。"""
    return [p for p in candidates if p.exists()]


def test_skill_docs_do_not_exclude_elt_credits():
    copies = _existing_skill_copies((SKILL_CLAUDE, SKILL_AGENTS))
    if not copies:
        pytest.skip("公开库检出不含 sync-to-public skill 文档，边界契约无需校验")
    for skill in copies:
        text = skill.read_text(encoding="utf-8")
        assert "elt_credits.js" in text, f"{skill} 必须提及 elt_credits.js 的同步要求"
        for line in text.splitlines():
            if "elt_credits" not in line:
                continue
            # 排除清单行 / Step2 正则行 / “仅私有” 描述都不允许出现
            assert "elt_credits\\.js$" not in line, f"{skill} 仍含排除正则: {line.strip()}"
            assert "仅私有" not in line, f"{skill} 仍称 elt_credits.js 仅私有: {line.strip()}"
            for bad in ("404 无害", "404 为无害", "无害失败"):
                assert bad not in line, f"{skill} 仍称 404 无害: {line.strip()}"
    # .claude 版存在时，Step 2 排除正则整体不得匹配 elt_credits.js 路径
    if SKILL_CLAUDE.exists():
        text = SKILL_CLAUDE.read_text(encoding="utf-8")
        m = re.search(r"\$rel -notmatch '([^']*elt_credits[^']*)'", text)
        assert m is None, f"Step 2 排除正则仍排除 elt_credits.js: {m.group(1)}"


def test_skill_doc_skip_only_when_neither_copy_exists(tmp_path):
    """公开库检出条件：两份 skill 文档都不存在时 doc 测试才允许跳过。"""
    assert _existing_skill_copies((tmp_path / "a.md", tmp_path / "b.md")) == []
    present = tmp_path / "one.md"
    present.write_text("x", encoding="utf-8")
    assert _existing_skill_copies((present, tmp_path / "b.md")) == [present]


def test_shared_templates_include_script_before_use():
    for tpl in SHARED_TEMPLATES:
        text = tpl.read_text(encoding="utf-8")
        uses = [m.start() for m in re.finditer(r"eltCredits\.", text)]
        if not uses:
            continue
        inc = text.find("/static/elt_credits.js")
        assert inc != -1, f"{tpl.name} 调用 eltCredits 但未引入 /static/elt_credits.js"
        assert inc < min(uses), f"{tpl.name} 中 elt_credits.js 引入晚于首次调用"


NODE_BEHAVIOR_SCRIPT = r"""
const fs = require('fs');
const vm = require('vm');

const jsPath = process.argv[2];
const code = fs.readFileSync(jsPath, 'utf-8');

const calls = [];
const sandbox = {
  console,
  crypto: require('crypto').webcrypto,
  FormData: class FormData {},  // billableFetch 仅做 instanceof 判断
};
// fetch stub：credits API 不存在（404），目标 API 正常 200
sandbox.fetch = async (url, opts) => {
  calls.push({ url: String(url), headers: (opts && opts.headers) || {}, method: (opts && opts.method) || 'GET' });
  if (String(url).startsWith('/api/credits/')) {
    return { ok: false, status: 404, json: async () => ({}), clone() { return this; } };
  }
  return { ok: true, status: 200, json: async () => ({ answer: 'ok' }), clone() { return this; } };
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(code, sandbox);

(async () => {
  const ec = sandbox.window.eltCredits;
  if (!ec) throw new Error('eltCredits 未定义');

  // rates 404 → null（静默降级，不抛错）
  const r = await ec.rates();
  if (r !== null) throw new Error('rates 404 应返回 null，实际 ' + JSON.stringify(r));

  // 代表调用：workspace.html lesson_chat 的 billableFetch 形状
  const resp = await ec.billableFetch('lesson_chat', '/api/v2/chat', {
    method: 'POST',
    body: JSON.stringify({ lesson_id: 1, question: 'hi' }),
  });
  if (resp.status !== 200) throw new Error('billableFetch 未透传目标响应');
  const target = calls.find(c => c.url === '/api/v2/chat');
  if (!target) throw new Error('目标 API 未被调用');
  if (target.method !== 'POST') throw new Error('method 未透传');
  if (!target.headers['Idempotency-Key']) throw new Error('缺少 Idempotency-Key 头');
  if (target.headers['Content-Type'] !== 'application/json') throw new Error('JSON body 未补 Content-Type');

  console.log('ELT_CREDITS_FAILOPEN_OK');
})().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
"""


def test_billable_fetch_works_with_credits_api_absent():
    # elt_credits.js 是被共享模板直接调用的公开资产：缺失必须失败，不得 skip
    assert JS_PATH.exists(), (
        "frontend/static/elt_credits.js 缺失——共享模板直接调用 eltCredits.billableFetch，"
        "缺失会让公开库用户动作抛 ReferenceError（同步边界回归）")
    with tempfile.NamedTemporaryFile(
            "w", suffix=".cjs", delete=False, encoding="utf-8") as f:
        f.write(NODE_BEHAVIOR_SCRIPT)
        script = f.name
    try:
        proc = subprocess.run(
            ["node", script, str(JS_PATH)],
            capture_output=True, text=True, timeout=60)
    finally:
        Path(script).unlink(missing_ok=True)
    assert proc.returncode == 0, (
        f"billableFetch 在 credits API 缺失时未 fail-open:\n{proc.stdout}\n{proc.stderr}")
    assert "ELT_CREDITS_FAILOPEN_OK" in proc.stdout


def test_elt_credits_js_present_for_public_sync():
    """elt_credits.js 必须存在且无私有依赖（不 import/auth 引用，纯静态助手）。"""
    text = JS_PATH.read_text(encoding="utf-8")
    assert "eltCredits" in text and "billableFetch" in text
    assert "/api/auth" not in text, "共享资产不得依赖私有 auth API"
