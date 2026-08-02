from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Browser smoke checks for the homepage generation flow.")
    parser.add_argument("--server", default="http://localhost:5173", help="Running Flask server URL.")
    parser.add_argument("--url", default=os.environ.get("SMOKE_BROWSER_URL", ""), help="URL/path to submit.")
    parser.add_argument("--submit", action="store_true", help="Click generate and wait for a lesson link.")
    parser.add_argument("--timeout-seconds", type=int, default=240, help="Generation wait timeout.")
    parser.add_argument("--headed", action="store_true", help="Run with a visible browser window.")
    args = parser.parse_args()

    if args.submit and not args.url.strip():
        print("[failed] --submit requires --url or SMOKE_BROWSER_URL", file=sys.stderr)
        return 2

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        js_errors: list[str] = []
        page.on("console", lambda m: js_errors.append(f"[{m.type}] {m.text}") if is_console_error(m.type, m.text) else None)
        page.on("pageerror", lambda e: js_errors.append(f"[PAGE_ERR] {e}"))
        try:
            run_checks(page, args.server.rstrip("/"), args.url.strip(), args.submit, args.timeout_seconds)
            if js_errors:
                raise AssertionError("browser console errors:\n" + "\n".join(js_errors[:10]))
        finally:
            browser.close()

    print("\n[ok] browser smoke checks passed")
    return 0


def is_console_error(message_type: str, text: str) -> bool:
    if message_type != "error":
        return False
    if text.startswith("Failed to load resource: the server responded with a status of 404"):
        return False
    return True


def run_checks(page: Page, server: str, url: str, submit: bool, timeout_seconds: int) -> None:
    print(f"[browser] opening {server}/")
    page.goto(f"{server}/", wait_until="networkidle")
    page.evaluate("() => { window.__smokeAlerts = []; window.alert = (msg) => window.__smokeAlerts.push(String(msg)); }")
    expect(page.locator("#url-input")).to_be_visible()
    expect(page.locator("#reading-text-input")).to_be_visible()
    expect(page.locator("#reading-file-input")).to_be_attached()
    expect(page.locator("#gen-btn")).to_be_visible()
    expect(page.locator("#lesson-grid")).to_be_visible()
    print("[browser] homepage controls visible")

    detection_cases = [
        ("https://www.bilibili.com/video/BV1SmokeTest1", "B站"),
        ("https://b23.tv/smoke", "B站"),
        ("C:\\tmp\\sample.mp4", "本地音视频"),
        ("https://example.com/article", "文章"),
    ]
    for value, expected in detection_cases:
        page.fill("#url-input", value)
        expect(page.locator("#source-badge")).to_contain_text(expected)
    print("[browser] source detection ok")
    check_reading_entrypoints(page)
    check_error_recovery(page)

    if not submit:
        print("[skip] generation click disabled. Use --submit --url ... to run the full UI flow.")
        return

    page.fill("#url-input", url)
    page.eval_on_selector("#analysis-mode", "el => { el.value = 'mock'; el.dispatchEvent(new Event('change', { bubbles: true })); }")
    page.eval_on_selector("#whisper-model", "el => { el.value = 'base'; el.dispatchEvent(new Event('change', { bubbles: true })); }")
    check_running_cancel(page, url)
    page.fill("#url-input", url)
    page.eval_on_selector("#analysis-mode", "el => { el.value = 'mock'; el.dispatchEvent(new Event('change', { bubbles: true })); }")
    page.eval_on_selector("#whisper-model", "el => { el.value = 'base'; el.dispatchEvent(new Event('change', { bubbles: true })); }")
    print(f"[browser] submitting {url}")
    page.click("#gen-btn")

    timeout_ms = timeout_seconds * 1000
    try:
        page.wait_for_selector("#progress-done:not(.hidden)", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        error_text = page.locator("#progress-error").inner_text(timeout=1000) if page.locator("#progress-error").count() else ""
        log_text = page.locator("#progress-log").inner_text(timeout=1000) if page.locator("#progress-log").count() else ""
        raise AssertionError(f"generation did not finish within {timeout_seconds}s\n{error_text}\n{log_text[-2000:]}")

    lesson_href = page.locator("#lesson-link").get_attribute("href")
    if not lesson_href:
        raise AssertionError("generation finished but #lesson-link has no href")
    print(f"[browser] generated lesson link: {lesson_href}")

    card_link = page.locator(f'#lesson-grid a.btn-open[href="{lesson_href}"]').first
    expect(card_link).to_be_visible()
    print("[browser] generated lesson card visible")

    card_link.click()
    page.wait_for_load_state("networkidle")
    if "/output/" not in page.url:
        raise AssertionError(f"lesson link did not open an output page: {page.url}")
    print(f"[browser] lesson opened: {page.url}")


def check_error_recovery(page: Page) -> None:
    missing_path = "C:\\tmp\\elt-browser-missing-file.mp4"
    page.fill("#url-input", missing_path)
    expect(page.locator("#source-badge")).to_contain_text("本地音视频")
    page.evaluate("() => { window.__smokeAlerts = []; }")
    page.click("#gen-btn")
    expect(page.locator("#inline-progress")).to_be_visible()
    page.wait_for_function(
        "() => document.getElementById('inline-progress')?.classList.contains('error')",
        timeout=5000,
    )
    progress_class = page.locator("#inline-progress").get_attribute("class") or ""
    if "error" not in progress_class:
        raise AssertionError(f"inline progress did not enter error state: {progress_class}")
    alerts = page.evaluate("() => window.__smokeAlerts || []")
    if not alerts:
        raise AssertionError("expected v2 local error alert")
    expect(page.locator("#gen-btn")).to_be_enabled()
    expect(page.locator("#cancel-btn")).to_be_hidden()
    expect(page.locator("#inline-progress-label")).not_to_have_text("准备中…")
    print("[browser] v2 local error recovery controls ok")
    page.evaluate("resetForm()")


def check_reading_entrypoints(page: Page) -> None:
    page.fill("#url-input", "")
    page.fill("#reading-text-input", "A short IELTS reading passage.\n\nAnother paragraph.")
    expect(page.locator("#reading-source-badge")).to_contain_text("粘贴文本")
    page.fill("#url-input", "C:\\tmp\\sample.mp4")
    page.evaluate("() => { window.__smokeAlerts = []; startGeneration(); }")
    alerts = page.evaluate("() => window.__smokeAlerts || []")
    if not any("分开生成" in alert for alert in alerts):
        raise AssertionError(f"unexpected conflict alerts: {alerts}")
    expect(page.locator("#gen-btn")).to_be_enabled()
    expect(page.locator("#reading-source-badge")).to_contain_text("粘贴文本")
    print("[browser] reading entrypoints and source conflict guard ok")
    page.evaluate("resetForm()")


def check_running_cancel(page: Page, url: str) -> None:
    print("[browser] checking running job cancel")
    page.click("#gen-btn")
    expect(page.locator("#cancel-btn")).to_be_visible()
    page.once("dialog", lambda dialog: dialog.accept())
    page.click("#cancel-btn")
    expect(page.locator("#gen-btn")).to_be_enabled()
    expect(page.locator("#cancel-btn")).to_be_hidden()
    print("[browser] running job cancel ok")
    page.evaluate("resetForm()")


if __name__ == "__main__":
    raise SystemExit(main())
