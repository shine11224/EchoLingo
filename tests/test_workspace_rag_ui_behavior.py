"""Task 10 返修 Blocker 3：证据卡跳转的行为级测试（Playwright 真实浏览器）。

不只断言字符串：从 workspace.html 抽取真实函数注入最小 DOM，
三张卡分别点击验证各自独立跳转：
- 视频卡：seek 到 start-2（clamp 0）、播放、闪烁经 segment_ids 映射的真实字幕句；
- Reading 句卡：复杂 sentence_key 精确命中（含 CSS.escape 不可用时的 dataset 回退）；
- Reading 块卡：无 sentence_key 时回退 data-block-index；
- coverage=none 不渲染任何卡。
"""
from pathlib import Path
import re

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright

WORKSPACE_HTML = (
    Path(__file__).parents[1] / "frontend" / "templates" / "workspace.html"
).read_text(encoding="utf-8")


def _extract(name: str) -> str:
    marker = f"function {name}("
    start = WORKSPACE_HTML.find(marker)
    assert start >= 0, f"{name} 未在 workspace.html 定义"
    brace = WORKSPACE_HTML.find("{", start)
    depth = 0
    for i in range(brace, len(WORKSPACE_HTML)):
        ch = WORKSPACE_HTML[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return WORKSPACE_HTML[start:i + 1]
    raise AssertionError(f"{name} 函数体未闭合")


EXTRACTED = "\n".join(_extract(n) for n in (
    "formatTime",
    "renderCitationCards",
    "renderAiMessageExtras",
    "findReadingAnchor",
    "flashCitationTarget",
    "playMediaPlayback",
    "jumpToCitation",
    "onCitationCardClick",
    "initCitationDelegation",
))

STUBS = """
var sentenceUnits = [
  {start: 763.4, end: 772.1, segment_ids: [87, 88], text: 'real subtitle'},
  {start: 10.0, end: 12.0, segment_ids: [3], text: 'other'},
];
var activeStudyMode = 'listening';
var activeSentenceIdx = -1;
var localMedia = {play: function(){ window.__played = (window.__played||0)+1; return Promise.resolve(); }};
var ytPlayer = null;
window.__seeks = [];
function seekTo(s) { window.__seeks.push(s); }
function updateCurrentSubtitle() { window.__subtitleUpdates = (window.__subtitleUpdates||0)+1; }
function scrollReadingToTime() { return false; }
"""

PAGE_HTML = f"""<!doctype html><html><body>
<div id="chat-messages">
  <div class="chat-msg ai"><div class="role">AI</div><div class="chat-message-body"></div></div>
</div>
<div id="current-subtitle">subtitle</div>
<article id="reading-passage">
  <p data-block-index="12"><span data-sentence-key="12003">Exact sentence.</span></p>
  <p data-block-index="13"><span data-sentence-key="13001">Block fallback sentence.</span></p>
</article>
<script>{STUBS}\n{EXTRACTED}\ninitCitationDelegation();</script>
</body></html>"""


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture()
def page(browser):
    pg = browser.new_page()
    pg.set_content(PAGE_HTML)
    yield pg
    pg.close()


def _render(page, meta):
    page.evaluate(
        """(meta) => {
            const item = document.querySelector('.chat-msg.ai');
            item.querySelector('.chat-extras')?.remove();
            renderAiMessageExtras(item, meta);
        }""",
        meta,
    )


THREE_CITATION_META = {
    "coverage_status": "full",
    "external_knowledge_used": False,
    "unsupported": [],
    "citations": [
        {"anchor_type": "time", "segment_index": 87, "start_seconds": 763.4,
         "end_seconds": 772.1, "chapter_title": "习惯形成机制",
         "excerpt": "The difficulty is not motivation"},
        {"anchor_type": "sentence", "block_index": 12, "sentence_key": 12003,
         "chapter_title": "作者的反驳", "excerpt": "Exact sentence."},
        {"anchor_type": "block", "block_index": 13,
         "chapter_title": "结尾", "excerpt": "Block fallback sentence."},
    ],
}


def test_three_cards_render_and_jump_independently(page):
    _render(page, THREE_CITATION_META)
    cards = page.query_selector_all(".citation-card")
    assert len(cards) == 3

    # 1) 视频卡：seek 到 763.4-2=761.4、播放、闪烁 current-subtitle（真实句映射）
    cards[0].click()
    assert page.evaluate("window.__seeks") == [pytest.approx(761.4)]
    assert page.evaluate("window.__played") == 1
    assert page.evaluate("window.activeSentenceIdx") == 0
    assert page.evaluate("window.__subtitleUpdates") == 1
    assert "citation-target-flash" in (
        page.get_attribute("#current-subtitle", "class") or "")

    # 2) Reading 句卡：精确命中 data-sentence-key=12003 的 span
    cards[1].click()
    assert "citation-target-flash" in (
        page.get_attribute('#reading-passage [data-sentence-key="12003"]', "class") or "")

    # 3) Reading 块卡：无 sentence_key → 回退 block 13 的段落
    cards[2].click()
    assert "citation-target-flash" in (
        page.get_attribute('#reading-passage [data-block-index="13"]', "class") or "")


def test_video_card_seek_clamped_to_zero(page):
    meta = dict(THREE_CITATION_META, citations=[
        {"anchor_type": "time", "segment_index": 87, "start_seconds": 1.0,
         "end_seconds": 2.0, "chapter_title": "", "excerpt": "x"},
    ])
    _render(page, meta)
    page.query_selector(".citation-card").click()
    assert page.evaluate("window.__seeks") == [0]


def test_complex_sentence_key_exact_match_without_css_escape(page):
    weird_key = 'a"b\\c][d'
    page.evaluate(
        """(key) => {
            const p = document.querySelector('[data-block-index="13"]');
            const span = document.createElement('span');
            span.setAttribute('data-sentence-key', key);
            span.id = 'weird-target';
            p.appendChild(span);
        }""",
        weird_key,
    )
    # CSS.escape 不可用：必须走 dataset 逐一比对，任意键不得破坏选择器
    page.evaluate("() => { window.CSS = undefined; }")
    _render(page, dict(THREE_CITATION_META, citations=[
        {"anchor_type": "sentence", "block_index": 12, "sentence_key": weird_key,
         "chapter_title": "", "excerpt": "weird"},
    ]))
    page.query_selector(".citation-card").click()
    assert "citation-target-flash" in (page.get_attribute("#weird-target", "class") or "")
    # 不得误闪同 block 的其他句子
    assert "citation-target-flash" not in (
        page.get_attribute('#reading-passage [data-sentence-key="13001"]', "class") or "")


def test_none_coverage_renders_no_cards(page):
    _render(page, dict(THREE_CITATION_META, coverage_status="none"))
    assert page.query_selector_all(".citation-card") == []
    assert page.query_selector(".coverage-badge.none") is not None


def test_unsupported_boundaries_rendered_as_safe_text(page):
    _render(page, dict(THREE_CITATION_META,
                       coverage_status="partial",
                       unsupported=['<img src=x onerror=alert(1)>', "缺口二"]))
    box = page.query_selector(".chat-unsupported")
    assert box is not None
    items = box.query_selector_all("li")
    assert len(items) == 2
    assert items[0].text_content() == '<img src=x onerror=alert(1)>'
    # textContent 注入：不得生成 img 元素
    assert box.query_selector("img") is None


def test_collapsed_native_selection_does_not_close_pinned_action_popover(browser):
    selection_script = "\n".join((
        _extract("closeReadingSelectionPopover"),
        _extract("captureStudySelection"),
        "async " + _extract("runReadingSelectionAction"),
    ))
    pg = browser.new_page()
    page_errors = []
    pg.on("pageerror", lambda error: page_errors.append(str(error)))
    pg.set_content(f"""<!doctype html><html><body>
      <article id="reading-passage">Readable sentence.</article>
      <div id="reading-selection-popover" class="reading-selection-popover open"></div>
      <script>
        var activeStudyMode = 'reading';
        var readingSelectionText = 'Readable sentence.';
        var readingSelectionSentenceKey = null;
        var readingSelectionRange = null;
        var readingSelectionTargetType = 'phrase';
        var readingSelectionMode = 'reading';
        var readingSelectionRequestToken = 0;
        var readingSelectionPopoverPinned = false;
        function matchSelectedReadingSentence() {{ return null; }}
        async function requestReadingSelectionTranslation() {{}}
        {selection_script}
      </script>
    </body></html>""")
    try:
        assert page_errors == []
        assert pg.evaluate("window.getSelection().isCollapsed") is True
        pg.evaluate("runReadingSelectionAction('translate')")
        assert pg.evaluate("readingSelectionPopoverPinned") is True
        pg.evaluate("captureStudySelection('reading')")
        assert "open" in (pg.get_attribute("#reading-selection-popover", "class") or "")

        pg.evaluate("closeReadingSelectionPopover(true)")
        assert pg.evaluate("readingSelectionPopoverPinned") is False
    finally:
        pg.close()
