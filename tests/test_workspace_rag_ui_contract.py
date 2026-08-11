"""Task 10：workspace.html RAG 证据卡与一键跳转的静态契约。

锁定 Codex 审核要检查的行为实现点：
- coverage 状态徽标（full/partial/none）与外部知识标签渲染；
- 证据卡最多 3 张、coverage=none 不渲染任何卡片/链接；
- 视频卡跳转：start_seconds-2 且 clamp 到 0、seek 后播放、用 sentenceUnits 的
  segment_ids 映射真实字幕句并闪烁（不得用 excerpt 文本当选择器）；
- Reading 卡跳转：优先 data-sentence-key，缺失才回退 data-block-index；
- CSS.escape 走现有 cssEscape 兜底封装；
- 历史消息恢复走同一个 addChatMessage meta 渲染路径，刷新后仍可点击；
- 事件委托挂在 #chat-messages 上，卡片为 button 语义 + aria-label；
- 模型输出一律 textContent 注入，不进 innerHTML；
- external knowledge 开关默认关、仅勾选时上送 allow_external_knowledge、
  每次发送后复位；
- 跳转不得 reload 页面或清空对话。
"""
from pathlib import Path
import re

WORKSPACE_HTML = (
    Path(__file__).parents[1] / "frontend" / "templates" / "workspace.html"
).read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    marker = f"function {name}("
    start = WORKSPACE_HTML.find(marker)
    assert start >= 0, f"{name} 未定义"
    brace = WORKSPACE_HTML.find("{", start)
    assert brace >= 0
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


# ---------- coverage 与外部知识标签 ----------

def test_coverage_badge_renders_full_partial_none():
    body = _function_body("renderAiMessageExtras")
    for status in ("full", "partial", "none"):
        assert status in body, f"coverage {status} 分支缺失"
    assert "coverage-badge" in body


def test_external_knowledge_label_rendered_when_used():
    body = _function_body("renderAiMessageExtras")
    assert "external_knowledge_used" in body
    assert "chat-ext-label" in body


def test_model_text_injected_via_textcontent_not_innerhtml():
    body = _function_body("renderAiMessageExtras")
    # 答案/摘录/章节标题全部 textContent；extras 区域不得出现 innerHTML 拼接模型内容
    assert "textContent" in body
    assert "innerHTML" not in body


# ---------- 证据卡 ----------

def test_citation_cards_capped_at_three():
    body = _function_body("renderCitationCards")
    assert ".slice(0, 3)" in body


def test_none_coverage_renders_no_cards():
    body = _function_body("renderAiMessageExtras")
    assert "'none'" in body
    # none 分支不得渲染证据卡
    assert re.search(r"none[^;]*return", body, re.S) or "coverage !== 'none'" in body


def test_video_card_shows_time_chapter_excerpt():
    body = _function_body("renderCitationCards")
    assert "formatTime(" in body
    assert "chapter_title" in body
    assert "excerpt" in body
    assert "citation-time" in body


def test_reading_card_shows_chapter_and_excerpt():
    body = _function_body("renderCitationCards")
    assert "sentence_key" in body
    assert "block_index" in body


def test_cards_use_button_semantics_and_aria_label():
    body = _function_body("renderCitationCards")
    assert "createElement('button')" in body
    assert "aria-label" in body or "setAttribute('aria-label'" in body
    assert "citation-card" in body


# ---------- 跳转 ----------

def test_video_jump_seeks_start_minus_2_clamped():
    body = _function_body("jumpToCitation")
    assert re.search(r"Math\.max\(\s*0\s*,[^)]*-\s*2\s*\)", body), "缺 start-2 clamp 到 0"


def test_video_jump_plays_and_flashes_real_segment():
    body = _function_body("jumpToCitation")
    assert "playMediaPlayback(" in body
    # 真实字幕句通过 sentenceUnits 的 segment_ids 映射，不用 excerpt 文本定位
    assert "segment_ids" in body
    assert "sentenceUnits" in body
    flash = _function_body("flashCitationTarget")
    assert "scrollIntoView" in flash
    assert "citation-target-flash" in flash


def test_playback_helper_covers_local_and_youtube():
    body = _function_body("playMediaPlayback")
    assert "localMedia" in body
    assert "playVideo" in body


def test_reading_jump_prefers_sentence_key_then_block_fallback():
    body = _function_body("jumpToCitation")
    assert "findReadingAnchor(" in body
    helper = _function_body("findReadingAnchor")
    sentence_pos = helper.find("data-sentence-key")
    block_pos = helper.find("data-block-index")
    assert sentence_pos >= 0 and block_pos >= 0
    assert sentence_pos < block_pos, "必须先查 data-sentence-key，再回退 block_index"


def test_reading_anchor_lookup_is_injection_safe():
    helper = _function_body("findReadingAnchor")
    # CSS.escape 可用时在 try 内使用；不可用时逐元素比对 dataset，任意键不进入选择器
    assert "CSS.escape" in helper
    assert "try" in helper
    assert "dataset.sentenceKey" in helper
    assert "dataset.blockIndex" in helper
    assert "querySelectorAll" in helper


def test_citation_flash_has_css_animation():
    assert ".citation-target-flash" in WORKSPACE_HTML
    assert "@keyframes citationFlash" in WORKSPACE_HTML


# ---------- 事件委托与历史恢复 ----------

def test_delegated_click_handler_on_chat_messages():
    body = _function_body("initCitationDelegation")
    assert "getElementById('chat-messages')" in body
    assert "addEventListener('click', onCitationCardClick)" in body
    handler = _function_body("onCitationCardClick")
    assert ".closest(" in handler
    assert "citation-card" in handler


def test_history_restore_uses_same_renderer_with_meta():
    start = WORKSPACE_HTML.find("async function loadChatHistory(")
    body = WORKSPACE_HTML[start:]
    assert "addChatMessage('ai', item.ai_response || '', item)" in body, \
        "历史消息必须带 meta（coverage/citations）走同一渲染器"


def test_send_message_passes_message_meta_to_renderer():
    start = WORKSPACE_HTML.find("async function sendMessage(")
    body = WORKSPACE_HTML[start:]
    assert "renderAiMessageExtras(pendingMessage, data.message" in body


def test_jump_does_not_reload_or_clear_chat():
    body = _function_body("jumpToCitation")
    assert "location.reload" not in body
    assert "chat-messages" not in body, "跳转不得触碰对话容器"
    assert "innerHTML = ''" not in body


# ---------- external knowledge 开关 ----------

def test_external_toggle_defaults_off_and_states_scope():
    assert 'id="chat-external-knowledge" type="checkbox">' in WORKSPACE_HTML
    assert "默认仅依据当前视频/文章内容" in WORKSPACE_HTML


def test_external_toggle_only_sent_when_checked_and_resets():
    start = WORKSPACE_HTML.find("async function sendMessage(")
    end = WORKSPACE_HTML.find("function formatChatInline", start)
    body = WORKSPACE_HTML[start:end]
    assert "allow_external_knowledge" in body
    # 仅勾选时写入 payload
    assert re.search(r"if\s*\(allowExternal\)[^}]*allow_external_knowledge\s*=\s*true", body), \
        "allow_external_knowledge 只能在勾选时上送 true"
    # 发送结束（成功或失败）后复位
    assert "extToggle.checked = false" in body


def test_external_toggle_resets_even_when_session_creation_fails():
    start = WORKSPACE_HTML.find("async function sendMessage(")
    end = WORKSPACE_HTML.find("function formatChatInline", start)
    body = WORKSPACE_HTML[start:end]
    toggle_pos = body.find("chat-external-knowledge")
    session_pos = body.find("ensureActiveChatSession")
    assert toggle_pos >= 0 and session_pos >= 0
    assert toggle_pos < session_pos, "必须先读开关再建会话，失败路径才能复位"
    assert re.search(r"!\s*sessionId[\s\S]*?extToggle\.checked\s*=\s*false", body), \
        "会话创建失败的提前返回也必须复位开关"


def test_unsupported_boundaries_rendered_for_partial_and_none():
    body = _function_body("renderAiMessageExtras")
    assert "chat-unsupported" in body
    assert "未被当前材料支持" in body
    assert "meta.unsupported" in body
    assert ".slice(0, 5)" in body, "unsupported 渲染必须有界"
    assert "'partial'" in body and "'none'" in body
    section = WORKSPACE_HTML.find(".chat-unsupported")
    assert section >= 0, "缺 chat-unsupported 样式"


def test_existing_range_behavior_preserved():
    start = WORKSPACE_HTML.find("async function sendMessage(")
    end = WORKSPACE_HTML.find("function formatChatInline", start)
    body = WORKSPACE_HTML[start:end]
    for field in ("context_mode", "selected_start_seconds", "selected_end_seconds",
                  "selected_segment_ids", "selected_text"):
        assert field in body
