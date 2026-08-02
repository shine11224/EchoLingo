"""
EchoLingo — 浏览器全量诊断脚本
用法: python scripts/playwright_qa.py [--lesson <filename>] [--check all|ipa|focus|render]

设计原则: 一次运行拿全部诊断数据，禁止为同一问题迭代多个脚本。
"""

import sys
import argparse
from playwright.sync_api import sync_playwright

BASE = "http://localhost:5173"
DEFAULT_LESSON = "learn-to-learn-in-46-minutes-98352583.html"

PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "

results = []


def check(label, ok, detail=""):
    status = PASS if ok else FAIL
    results.append((status, label, detail))
    suffix = f"  ({detail})" if detail else ""
    print(f"  {status}  {label}{suffix}")
    return ok


def warn(label, detail=""):
    results.append((WARN, label, detail))
    suffix = f"  ({detail})" if detail else ""
    print(f"  {WARN}  {label}{suffix}")


def render_first_cards(page, count=3):
    """强制渲染前 N 张懒加载卡片"""
    page.evaluate(f"""() => {{
        const placeholders = document.querySelectorAll('.sentence-card-placeholder');
        for (let i = 0; i < Math.min({count}, placeholders.length); i++) {{
            const el = placeholders[i];
            const idx = parseInt(el.dataset.cardIdx, 10);
            if (typeof renderOneCard === 'function') el.outerHTML = renderOneCard(idx);
        }}
    }}""")
    page.wait_for_timeout(400)


def check_char_codes(page, selector, attr="class"):
    """检查 DOM 属性里是否有非 ASCII 引号（弯引号 bug）"""
    result = page.evaluate(f"""() => {{
        const els = document.querySelectorAll('{selector}');
        const suspicious = [];
        els.forEach(el => {{
            const val = el.getAttribute('{attr}') || '';
            const codes = Array.from(val).map(c => c.charCodeAt(0));
            const bad = codes.filter(c => c > 127 && (c === 8220 || c === 8221 || c === 8216 || c === 8217));
            if (bad.length > 0) suspicious.push({{ selector: el.tagName + '.' + val.substring(0,30), badCodes: bad }});
        }});
        return suspicious;
    }}""")
    return result


# ── 模块：IPA 渲染检查 ──────────────────────────────────────────────────────

def check_ipa(page):
    print("\n[IPA] Phase A 音标渲染检查")

    # 检查 pron-guide 类是否存在
    render_first_cards(page, 1)
    page.locator('[data-open-focus]').first.click()
    page.wait_for_timeout(500)

    container = page.locator('#focus-card-container')
    check("pron-guide 元素存在", container.locator('.pron-guide').count() > 0)
    check("phase-a-paired 存在（词级对齐）", container.locator('.phase-a-paired').count() > 0)

    # charCode 检查：pron-guide 的 class 属性
    suspicious = page.evaluate("""() => {
        const container = document.getElementById('focus-card-container');
        if (!container) return [];
        const els = container.querySelectorAll('[class]');
        const bad = [];
        els.forEach(el => {
            const cls = el.getAttribute('class') || '';
            const codes = Array.from(cls).map(c => c.charCodeAt(0));
            const weird = codes.filter(c => c > 127);
            if (weird.length) bad.push({ class: cls.substring(0,40), codes: weird });
        });
        return bad;
    }""")
    if suspicious:
        check("class 属性无弯引号", False,
              f"发现 {len(suspicious)} 个异常 class，codes={suspicious[0]['codes']}")
        print(f"    修复命令：PowerShell: $c=Get-Content templates\\lesson.html -Encoding UTF8 -Raw; $c.Replace([char]8221,'\"').Replace([char]8220,'\"') | Set-Content templates\\lesson.html -Encoding UTF8 -NoNewline")
    else:
        check("class 属性无弯引号", True)

    # IPA 内容检查
    ipa_info = page.evaluate("""() => {
        const paired = document.querySelector('#focus-card-container .phase-a-paired');
        if (!paired) return {count: 0};
        const pairs = paired.querySelectorAll('.word-ipa-pair');
        const sample = pairs[0];
        return {
            count: pairs.length,
            firstText: sample?.querySelector('.pair-text')?.textContent || '',
            firstIpa: sample?.querySelector('.pair-ipa')?.textContent || '',
            hasLink: paired.querySelectorAll('.pron-link').length,
            hasStrong: paired.querySelectorAll('.pron-strong').length
        };
    }""")
    check("词级 IPA 对数 > 0", ipa_info.get('count', 0) > 0, f"count={ipa_info.get('count')}")
    if ipa_info.get('count', 0) > 0:
        check("IPA 内容非空", bool(ipa_info.get('firstIpa')), f"sample: {ipa_info.get('firstIpa','')[:20]}")

    # 关闭专注视图
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)


# ── 模块：专注视图导航检查 ────────────────────────────────────────────────

def check_focus(page):
    print("\n[FOCUS] 专注视图完整体验")

    render_first_cards(page, 3)

    # 打开
    first_btn = page.locator('[data-open-focus]').first
    first_btn.wait_for(state="visible", timeout=8000)
    first_btn.click()
    page.wait_for_timeout(500)

    focus_view = page.locator('#focus-view')
    ok = check("专注视图打开", "hidden" not in (focus_view.get_attribute("class") or ""))
    if not ok:
        return

    # 渲染完整性
    container = page.locator('#focus-card-container')
    check("卡片已渲染且展开", container.locator('.sentence-card.open').count() == 1)
    check("pron-guide 存在", container.locator('.pron-guide').count() > 0)
    check("词汇区域存在", container.locator('.word-list').count() > 0)
    check("口语解析区域存在", container.locator('[data-oral-result]').count() > 0)
    check("情景造句区域存在", container.locator('[data-practice-input]').count() > 0)

    # 位置显示
    pos = page.locator('#focus-position').text_content() or ''
    check("位置格式 X / N", "/" in pos, pos)
    total = int(pos.split("/")[1].strip()) if "/" in pos else 0
    pos_num = int(pos.split("/")[0].strip()) if "/" in pos else 0
    check("total > 0", total > 0)

    # 导航按钮
    prev_btn = page.locator('#focus-prev')
    next_btn = page.locator('#focus-next')
    check("首句时 prev 禁用", prev_btn.is_disabled())

    next_btn.click(); page.wait_for_timeout(300)
    new_pos = page.locator('#focus-position').text_content() or ''
    new_num = int(new_pos.split("/")[0].strip()) if "/" in new_pos else 0
    check("→ 按钮位置+1", new_num == pos_num + 1, f"{pos_num}→{new_num}")

    # 键盘导航
    page.keyboard.press("ArrowLeft"); page.wait_for_timeout(300)
    kb_pos = page.locator('#focus-position').text_content() or ''
    kb_num = int(kb_pos.split("/")[0].strip()) if "/" in kb_pos else 0
    check("ArrowLeft 后退", kb_num == pos_num, f"→{kb_num}")

    page.keyboard.press("ArrowRight"); page.wait_for_timeout(200)
    page.keyboard.press("ArrowRight"); page.wait_for_timeout(200)
    kb2 = page.locator('#focus-position').text_content() or ''
    kb2_num = int(kb2.split("/")[0].strip()) if "/" in kb2 else 0
    check("连按两次 ArrowRight +2", kb2_num == pos_num + 2, f"→{kb2_num}")

    # 末句边界
    for _ in range(total):
        if next_btn.is_disabled():
            break
        next_btn.click(); page.wait_for_timeout(100)
    check("末句时 next 禁用", next_btn.is_disabled())

    # 关闭
    page.keyboard.press("Escape"); page.wait_for_timeout(300)
    check("Escape 关闭", "hidden" in (focus_view.get_attribute("class") or ""))

    # ✕ 按钮
    first_btn.click(); page.wait_for_timeout(400)
    page.locator('#focus-close').click(); page.wait_for_timeout(300)
    check("✕ 按钮关闭", "hidden" in (focus_view.get_attribute("class") or ""))


# ── 模块：Phase B 卡片渲染完整性 ────────────────────────────────────────

def check_render(page):
    print("\n[RENDER] Phase B 卡片渲染完整性")

    render_first_cards(page, 3)
    page.wait_for_timeout(300)

    info = page.evaluate("""() => {
        const cards = document.querySelectorAll('.sentence-card');
        if (!cards.length) return {error: 'no cards'};
        const c = cards[0];
        return {
            count: cards.length,
            hasHead: !!c.querySelector('.sentence-head'),
            hasFocusBtn: !!c.querySelector('[data-open-focus]'),
            hasPlayBtn: !!c.querySelector('.mini-play-btn'),
            hasBadge: !!c.querySelector('.badge'),
            hasBody: !!c.querySelector('.sentence-body')
        };
    }""")
    if 'error' in info:
        check("卡片已渲染", False, info['error'])
        return

    check("至少 1 张卡片", info['count'] > 0)
    check("卡片头部（句子文本）", info['hasHead'])
    check("专注模式按钮 ⤢", info['hasFocusBtn'])
    check("mini 播放按钮", info['hasPlayBtn'])
    check("难度徽标", info['hasBadge'])
    check("卡片体（可展开）", info['hasBody'])

    # JS console 错误
    # （错误已在 run() 层面收集）


# ── 主流程 ───────────────────────────────────────────────────────────────

def run(lesson: str, check_mode: str):
    url = f"{BASE}/rerender/{lesson}"
    print(f"\n{'='*55}")
    print(f"Playwright QA — {lesson}")
    print(f"URL: {url}")
    print(f"模式: {check_mode}")
    print('='*55)

    js_errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, slow_mo=100)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.on("console", lambda m: js_errors.append(f"[{m.type}] {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: js_errors.append(f"[PAGE_ERR] {e}"))

        # 加载页面
        print("\n[LOAD] 加载页面")
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        check("页面加载成功", bool(page.title()), page.title()[:40])

        # 切换到 Phase B
        page.locator('[data-phase-tab="phase-b"]').click()
        page.wait_for_timeout(600)
        phase_b_active = "active" in (page.locator('#phase-b').get_attribute("class") or "")
        check("Phase B 激活", phase_b_active)

        # 按 check_mode 执行
        if check_mode in ("all", "render"):
            check_render(page)

        if check_mode in ("all", "ipa"):
            check_ipa(page)
            # 重新切 Phase B（ipa 检查后页面状态可能变）
            page.locator('[data-phase-tab="phase-b"]').click()
            page.wait_for_timeout(400)

        if check_mode in ("all", "focus"):
            check_focus(page)

        browser.close()

    # JS 错误汇总
    print(f"\n[JS ERRORS] 共 {len(js_errors)} 条")
    if js_errors:
        for e in js_errors[:5]:
            print(f"  {e}")
        if len(js_errors) > 5:
            print(f"  ...（还有 {len(js_errors)-5} 条）")

    # 最终汇总
    print(f"\n{'─'*55}")
    passed = sum(1 for r in results if r[0] == PASS)
    failed = sum(1 for r in results if r[0] == FAIL)
    warned = sum(1 for r in results if r[0] == WARN)
    print(f"结果：{passed} 通过 / {failed} 失败 / {warned} 警告")
    if failed:
        print("失败项：")
        for r in results:
            if r[0] == FAIL:
                detail = f": {r[2]}" if r[2] else ""
                print(f"  {r[1]}{detail}")
    return failed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Playwright QA for EchoLingo")
    parser.add_argument("--lesson", default=DEFAULT_LESSON, help="课程 HTML 文件名")
    parser.add_argument("--check", default="all", choices=["all", "ipa", "focus", "render"],
                        help="检查模块：all / ipa / focus / render")
    args = parser.parse_args()
    sys.exit(run(args.lesson, args.check))
