"""
端到端验证专注视图：导航 / 按钮 / 渲染
运行：conda run -n english-tool python scripts/test_focus_view.py
"""
import sys
import time
from playwright.sync_api import sync_playwright, expect

BASE = "http://localhost:5173"
LESSON = f"{BASE}/rerender/learn-to-learn-in-46-minutes-98352583.html"

PASS = "✅"
FAIL = "❌"
results = []

def check(label, ok, detail=""):
    status = PASS if ok else FAIL
    results.append((status, label, detail))
    print(f"  {status}  {label}" + (f"  ({detail})" if detail else ""))


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=200)
        page = browser.new_page()

        # ── 1. 加载页面 ──────────────────────────────────────────
        print("\n[1] 加载课程页面")
        page.goto(LESSON, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        check("页面 title 存在", bool(page.title()))

        # ── 2. 切到 Phase B ──────────────────────────────────────
        print("\n[2] 切换到 Phase B")
        phase_b_tab = page.locator('[data-phase-tab="phase-b"]')
        phase_b_tab.click()
        page.wait_for_timeout(800)
        phase_b = page.locator('#phase-b')
        check("Phase B 激活", "active" in (phase_b.get_attribute("class") or ""))

        # ── 3. 点第一个 ⤢ 按钮 ─────────────────────────────────
        print("\n[3] 打开专注视图")
        # 懒加载：JS 强制渲染前几张卡片（绕过 IntersectionObserver 触发限制）
        page.evaluate("""
            const placeholders = document.querySelectorAll('.sentence-card-placeholder');
            for (let i = 0; i < Math.min(3, placeholders.length); i++) {
                const el = placeholders[i];
                const idx = parseInt(el.dataset.cardIdx, 10);
                if (typeof renderOneCard === 'function') {
                    el.outerHTML = renderOneCard(idx);
                }
            }
        """)
        page.wait_for_timeout(800)
        first_focus_btn = page.locator('[data-open-focus]').first
        first_focus_btn.wait_for(state="visible", timeout=10000)
        card_idx = int(first_focus_btn.get_attribute("data-open-focus"))
        first_focus_btn.click()
        page.wait_for_timeout(500)

        focus_view = page.locator('#focus-view')
        check("专注视图可见", not focus_view.get_attribute("class") or "hidden" not in (focus_view.get_attribute("class") or ""))

        # ── 4. 渲染验证 ─────────────────────────────────────────
        print("\n[4] 渲染内容验证")
        container = page.locator('#focus-card-container')
        check("卡片已渲染", container.locator('.sentence-card').count() == 1)
        check("卡片展开(open)", "open" in (container.locator('.sentence-card').get_attribute("class") or ""))
        check("IPA 音标存在", container.locator('.pron-guide').count() > 0)
        check("词汇区域存在", container.locator('.word-list').count() > 0)

        # ── 5. 位置显示 ─────────────────────────────────────────
        print("\n[5] 位置显示")
        pos_text = page.locator('#focus-position').text_content()
        check("位置格式 X / N", "/" in (pos_text or ""), pos_text)
        pos_num = int(pos_text.split("/")[0].strip()) if pos_text else 0
        total = int(pos_text.split("/")[1].strip()) if pos_text and "/" in pos_text else 0
        check("total > 0", total > 0, f"total={total}")

        # ── 6. 导航：下一句 ─────────────────────────────────────
        print("\n[6] 导航测试")
        prev_btn = page.locator('#focus-prev')
        next_btn = page.locator('#focus-next')
        check("首句时 prev 禁用", prev_btn.is_disabled())
        next_btn.click()
        page.wait_for_timeout(400)
        new_pos = page.locator('#focus-position').text_content()
        new_num = int(new_pos.split("/")[0].strip()) if new_pos else 0
        check("点 → 后位置+1", new_num == pos_num + 1, f"{pos_num} → {new_num}")
        check("点 → 后 prev 不再禁用", not prev_btn.is_disabled())

        # ── 7. 键盘导航 ─────────────────────────────────────────
        print("\n[7] 键盘导航")
        page.keyboard.press("ArrowLeft")
        page.wait_for_timeout(400)
        kb_pos = page.locator('#focus-position').text_content()
        kb_num = int(kb_pos.split("/")[0].strip()) if kb_pos else 0
        check("ArrowLeft 回到首句", kb_num == pos_num, f"→{kb_num}")

        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(300)
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(300)
        kb_pos2 = page.locator('#focus-position').text_content()
        kb_num2 = int(kb_pos2.split("/")[0].strip()) if kb_pos2 else 0
        check("连按两次 ArrowRight +2", kb_num2 == pos_num + 2, f"→{kb_num2}")

        # ── 8. 尾句时 next 禁用 ─────────────────────────────────
        print("\n[8] 边界测试")
        # 跳到最后一句
        for _ in range(total):
            if next_btn.is_disabled():
                break
            next_btn.click()
            page.wait_for_timeout(150)
        check("末句时 next 禁用", next_btn.is_disabled())

        # ── 9. Escape 关闭 ──────────────────────────────────────
        print("\n[9] 关闭专注视图")
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        check("Escape 关闭视图", "hidden" in (focus_view.get_attribute("class") or ""))

        # ── 10. ✕ 按钮重新打开后关闭 ───────────────────────────
        print("\n[10] ✕ 按钮关闭")
        first_focus_btn.click()
        page.wait_for_timeout(400)
        page.locator('#focus-close').click()
        page.wait_for_timeout(400)
        check("✕ 按钮关闭视图", "hidden" in (focus_view.get_attribute("class") or ""))

        browser.close()

    # ── 汇总 ────────────────────────────────────────────────────
    print("\n" + "─" * 50)
    passed = sum(1 for r in results if r[0] == PASS)
    failed = sum(1 for r in results if r[0] == FAIL)
    print(f"结果：{passed} 通过 / {failed} 失败")
    if failed:
        print("失败项：")
        for r in results:
            if r[0] == FAIL:
                print(f"  {r[1]}" + (f": {r[2]}" if r[2] else ""))
    return failed


if __name__ == "__main__":
    sys.exit(run())
