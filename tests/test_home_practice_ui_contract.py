from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest


INDEX_HTML = (
    Path(__file__).parents[1] / "frontend" / "templates" / "index.html"
).read_text(encoding="utf-8")


def test_sentence_library_is_browse_first_and_has_inline_listening_controls():
    assert "今日复习" not in INDEX_HTML


def test_planning_filters_are_not_exposed_in_vocab_or_sentence_libraries():
    assert 'data-filter="today-plan"' not in INDEX_HTML
    assert 'data-filter="ai-recommended"' not in INDEX_HTML
    assert 'id="sentence-today-plan-filter"' not in INDEX_HTML
    assert 'id="sentence-ai-recommended-filter"' not in INDEX_HTML
    assert "currentFilters.add('today-plan')" not in INDEX_HTML


def test_sentence_library_toolbar_has_separate_playback_and_filter_groups():
    assert 'class="sentence-library-heading"' in INDEX_HTML
    assert 'class="sentence-library-tools sentence-playback-tools"' in INDEX_HTML
    assert 'class="sentence-library-tools sentence-filter-tools"' in INDEX_HTML


def test_vocab_wordlist_filters_use_compiled_membership_instead_of_level_labels():
    assert "/api/wordlists/membership" in INDEX_HTML
    assert "_vocabWordlistMembership" in INDEX_HTML
    assert "vocabMatchesWordlist" in INDEX_HTML


def test_resources_tab_has_wordlist_selection_and_priority_ui():
    assert 'id="wordlist-config-list"' in INDEX_HTML
    assert 'id="wordlist-priority-row"' in INDEX_HTML
    assert "loadWordlistConfigUI" in INDEX_HTML
    assert "renderWordlistConfigUI" in INDEX_HTML
    # 与学习页共用同一个 localStorage 选择键
    assert "phase_a_user_wordlists" in INDEX_HTML
    # 可选范围：内置词表、上传词表、我的生词本
    assert "builtin_" in INDEX_HTML
    assert "my_vocab" in INDEX_HTML
    # 上传词表的同步/全选逻辑不得清掉内置词表与生词本的选择
    assert "!key.startsWith('user_') || available.includes(key)" in INDEX_HTML
    assert "selectedResourceWordlistKeys().filter(k => !k.startsWith('user_'))" in INDEX_HTML
    assert 'id="start-listening-practice"' not in INDEX_HTML
    assert 'id="sentence-listening-session"' not in INDEX_HTML
    assert 'id="sentence-library-shell"' in INDEX_HTML
    assert 'id="sentence-listening-result-filter"' in INDEX_HTML
    assert 'id="sentence-archive-filter"' in INDEX_HTML
    assert 'id="sentence-pattern-filter"' in INDEX_HTML


def test_sentence_library_rows_record_binary_listening_result_inline():
    assert "听懂" in INDEX_HTML
    assert "听不懂" in INDEX_HTML
    assert "recordSentenceListeningResult" in INDEX_HTML
    assert "result === 'understood'" in INDEX_HTML
    assert "result === 'not_understood'" in INDEX_HTML
    assert "renderSentenceReviewList();" in INDEX_HTML


def test_sentence_library_rows_can_hide_original_for_inline_listening_practice():
    assert "_sentenceHiddenOriginals" in INDEX_HTML
    assert "toggleSentenceOriginal" in INDEX_HTML
    assert "原文已隐藏，请先听声音再显示核对。" in INDEX_HTML
    assert "显示原文" in INDEX_HTML
    assert "隐藏原文" in INDEX_HTML
    assert "🔊 播放" in INDEX_HTML
    assert 'aria-label="调整播放速度"' in INDEX_HTML
    assert "setReviewAudioRate(Number(this.value))" in INDEX_HTML


def test_sentence_library_offers_spoken_retell_with_ai_comparison():
    assert "toggleSentenceRetell" in INDEX_HTML
    assert "renderSentenceRetellPanel" in INDEX_HTML
    assert "toggleSentenceRetellRecording" in INDEX_HTML
    assert "/api/transcribe" in INDEX_HTML
    assert "AI 对比分析" in INDEX_HTML
    assert "/api/listening-retell-analysis" in INDEX_HTML
    assert "renderSentenceRetellAnalysis" in INDEX_HTML
    assert "整句复述" in INDEX_HTML


def test_sentence_course_filter_is_searchable_dropdown():
    assert 'id="sentence-course-combo"' in INDEX_HTML
    assert "toggleSentenceCourseCombo" in INDEX_HTML
    assert "combo-panel" in INDEX_HTML
    assert "combo-option" in INDEX_HTML
    assert "搜索课程…" in INDEX_HTML
    assert "全部课程" in INDEX_HTML
    assert "已选 " in INDEX_HTML
    assert "toggleSentenceCourseFilter" in INDEX_HTML


def test_sentence_rows_merge_analysis_and_practice_in_one_expanded_flow():
    assert "toggleSentenceArchive" in INDEX_HTML
    assert "openSentenceAnalysis" in INDEX_HTML
    assert "AI 句式分析" in INDEX_HTML
    assert "查看句式分析与练习" in INDEX_HTML
    assert "openSentencePatternPractice" not in INDEX_HTML
    assert "renderSentencePatternPractice(sentence)}`" in INDEX_HTML
    assert "返回原课程" in INDEX_HTML


def test_sentence_rows_reuse_or_offer_analysis_and_pattern_focused_ai_practice():
    assert "sentenceOralAnalysis" in INDEX_HTML
    assert "renderSentenceAnalysisPanel" in INDEX_HTML
    assert "renderSentencePatternPractice" in INDEX_HTML
    assert "先凭记忆自由复用" in INDEX_HTML
    assert "默认自由复用；需要提示时再生成中文情景。" in INDEX_HTML
    assert "查看句式骨架" in INDEX_HTML
    assert ".pattern-reuse-strip[hidden] { display: none; }" in INDEX_HTML
    assert "AI 中文提示" in INDEX_HTML
    assert "换一个提示" in INDEX_HTML
    assert "输入或用语音说出你的英文表达" in INDEX_HTML
    assert "submitSentencePatternPractice" in INDEX_HTML
    assert "submitSentencePatternExample" in INDEX_HTML
    assert "提交造句后开放" in INDEX_HTML
    assert "viewed_example:true" in INDEX_HTML
    assert "toggleSentenceVoiceInput" in INDEX_HTML
    assert "practice_type:'pattern'" in INDEX_HTML
    assert "确认句式骨架" not in INDEX_HTML
    assert "确认或编辑句式骨架" not in INDEX_HTML


def test_direct_sentence_library_navigation_checks_ai_availability():
    assert "async function ensureAiAvailability()" in INDEX_HTML
    assert "fetch('/health', {cache:'no-store'})" in INDEX_HTML
    load_review = INDEX_HTML.split("async function loadSentenceReview()", 1)[1].split(
        "function sentenceListeningPriority", 1
    )[0]
    assert "ensureAiAvailability()" in load_review
    assert "请先在“设置”中检查配置" in INDEX_HTML


def test_ai_result_sentences_offer_translate_speak_and_favorite():
    assert "aiSentenceActions" in INDEX_HTML
    assert "translateAiSentence" in INDEX_HTML
    assert "favoriteAiSentence" in INDEX_HTML
    assert "🌐 翻译" in INDEX_HTML
    assert "⭐ 收藏句子" in INDEX_HTML
    assert "✓ 已收藏" in INDEX_HTML
    assert "/api/story-translate" in INDEX_HTML
    assert "/api/v2/lessons/sentence-review/manual" in INDEX_HTML
    assert "ai-sentence-translation" in INDEX_HTML


def test_vocab_workshop_keeps_story_as_secondary_subpage():
    assert 'data-workshop-view="practice"' in INDEX_HTML
    assert 'data-workshop-view="story"' in INDEX_HTML
    assert 'id="vocab-practice-view"' in INDEX_HTML
    assert 'id="vocab-story-view"' in INDEX_HTML
    assert "记忆故事" in INDEX_HTML
    assert "继续词汇练习" in INDEX_HTML


def test_memory_story_matches_reading_interaction_tools():
    assert 'id="story-selection-popover"' in INDEX_HTML
    assert "captureStorySelection" in INDEX_HTML
    assert "openStoryWordTools" in INDEX_HTML
    assert "/api/story-word-meaning/" in INDEX_HTML
    assert "/api/vocab-review/activate" in INDEX_HTML
    assert "source: _storySelectionScope === 'story' ? 'story' : 'manual'" in INDEX_HTML
    assert "/api/story-translate" in INDEX_HTML
    assert "NaturalTTS.speak(_storySelectionText" in INDEX_HTML
    assert "neural:true" in INDEX_HTML
    assert 'id="story-chat-panel"' in INDEX_HTML
    assert "/api/story-chat" in INDEX_HTML
    assert "sendStoryChatMessage" in INDEX_HTML


def test_memory_story_can_browse_and_restore_history():
    assert 'id="story-history-panel"' in INDEX_HTML
    assert 'id="story-history-list"' in INDEX_HTML
    assert 'id="story-history-count"' in INDEX_HTML
    assert 'id="story-history-pagination"' in INDEX_HTML
    assert 'id="story-history-prev"' in INDEX_HTML
    assert 'id="story-history-next"' in INDEX_HTML
    assert "const STORY_HISTORY_PAGE_SIZE = 9" in INDEX_HTML
    assert "page_size=${STORY_HISTORY_PAGE_SIZE}" in INDEX_HTML
    assert "renderStoryHistory" in INDEX_HTML
    assert "changeStoryHistoryPage" in INDEX_HTML
    assert "deleteStoryHistoryItem" in INDEX_HTML
    assert "method:'DELETE'" in INDEX_HTML
    assert "openStoryHistoryItem" in INDEX_HTML
    assert "applyStoryHistorySettings" in INDEX_HTML
    assert "if (view === 'story') loadStoryHistory();" in INDEX_HTML
    assert INDEX_HTML.index('id="story-history-panel"') > INDEX_HTML.index('id="story-area"')
    assert "_storyWordSet = new Set((item.words || []).slice(0, STORY_WORD_LIMIT))" in INDEX_HTML


def test_vocab_cards_reuse_reading_tools_and_offer_sentence_playback():
    assert "renderVocabReadingText" in INDEX_HTML
    assert "captureVocabCardSelection" in INDEX_HTML
    assert "captureStorySelection(card)" in INDEX_HTML
    assert "_storySelectionScope = event.currentTarget.closest('.word-card') ? 'vocab' : 'story'" in INDEX_HTML
    assert "data-story-action=\"lookup\"" in INDEX_HTML
    assert "/api/story-translate" in INDEX_HTML
    assert "selectedWordCount === 1 ? '🔊 发音' : '🔊 朗读选区'" in INDEX_HTML
    assert "speakVocabSentence" in INDEX_HTML
    assert "context.audio || {}" in INDEX_HTML
    assert "正在播放原音" in INDEX_HTML
    assert "playReviewYouTubeAudio(audio)" in INDEX_HTML
    assert "_playReviewAudio(audio.url, start, end)" in INDEX_HTML
    assert "editVocabTags" in INDEX_HTML
    assert "/api/vocab-review/${encodeURIComponent(word)}/tags" in INDEX_HTML
    assert "entry.tags || []" in INDEX_HTML
    assert "f.startsWith('tag:')" in INDEX_HTML
    assert "🔊 播放整句" in INDEX_HTML
    assert "NaturalTTS.speak(text,{lang:'en-US',rate:0.94,neural:true" in INDEX_HTML
    assert "data-toggle-vocab-card" in INDEX_HTML
    assert "toggleVocabCardFromButton" in INDEX_HTML


def test_vocab_card_supports_context_first_assessment_and_explicit_lifecycle():
    assert "highlightVocabTarget" in INDEX_HTML
    assert "不认识" in INDEX_HTML
    assert "模糊" in INDEX_HTML
    assert "认识" in INDEX_HTML
    assert "归档词汇" in INDEX_HTML
    assert "标记已掌握" in INDEX_HTML
    assert "重新加入生词表" in INDEX_HTML
    assert "返回原句" in INDEX_HTML


def test_sentence_making_separates_correction_hint_and_example():
    assert "AI 情景造句" in INDEX_HTML
    assert "查看 AI 示例" in INDEX_HTML
    assert "显示目标词" in INDEX_HTML
    assert "submitVocabPracticeExample" in INDEX_HTML
    assert "disabled" in INDEX_HTML
    assert "user_answer: answer" in INDEX_HTML
    assert "viewed_example" in INDEX_HTML
    assert 'id="vocab-hint-btn-' in INDEX_HTML
    assert "hintBtn.textContent=data.hint?'换一个提示':'重新生成提示'" in INDEX_HTML


def test_frontend_calls_the_confirmed_review_contracts():
    assert "/api/v2/lessons/sentence-review?include_archived=1" in INDEX_HTML
    assert "/listening-result" in INDEX_HTML
    assert "/api/vocab-review/${encodeURIComponent(word)}/familiarity" in INDEX_HTML
    assert "/api/vocab-review/${encodeURIComponent(word)}/lifecycle" in INDEX_HTML
    assert "action:'example'" in INDEX_HTML
    assert "user_answer:''" in INDEX_HTML
    assert "/api/v2/lessons/sentence-review/${sentence.id}/tags" in INDEX_HTML
    assert "fetch('/api/v2/lessons/sentence-tags')" in INDEX_HTML
    assert "toggleSentenceLibraryTagEditor" in INDEX_HTML
    assert "addSentenceLibraryTag" in INDEX_HTML
    assert "removeSentenceLibraryTag" in INDEX_HTML


def test_vocab_familiarity_buttons_expose_visible_and_accessible_selected_state():
    assert ".assess-btn.unknown.active" in INDEX_HTML
    assert ".assess-btn.fuzzy.active" in INDEX_HTML
    assert ".assess-btn.know.active" in INDEX_HTML
    assert 'aria-pressed="${entry.familiarity===\'unknown\'?\'true\':\'false\'}"' in INDEX_HTML
    assert "button.setAttribute('aria-pressed', String(isActive))" in INDEX_HTML


def test_vocab_meaning_stays_hidden_until_current_session_assessment():
    assert "const assessed = _assessedWords.has(word) ? 'true' : 'false';" in INDEX_HTML
    assert '<span class="word-meaning meaning-hidden">' in INDEX_HTML
    assert '<span class="word-ipa meaning-hidden">' not in INDEX_HTML
    assert 'class="meaning-prompt" aria-live="polite">自评后显示释义' in INDEX_HTML
    assert "card.dataset.assessed = 'true';" in INDEX_HTML


def test_vocab_card_can_generate_and_cache_missing_ai_analysis_after_assessment():
    assert "hasDeepAnalysis" in INDEX_HTML
    assert "AI 生成词语解析" in INDEX_HTML
    assert "data-ai-analysis-button" in INDEX_HTML
    assert "generateVocabAnalysis" in INDEX_HTML
    assert "sentence:context.sentence || entry.sentence || ''" in INDEX_HTML
    assert "target_type:entry.target_type || 'word'" in INDEX_HTML
    assert "analysisButton.disabled = false" in INDEX_HTML
    assert "该词由手动加入，未关联课程原句。" in INDEX_HTML


def test_manual_vocab_without_course_sentence_does_not_use_ai_example_as_context():
    assert "function resolveVocabContextSentence(entry, context, analysis)" in INDEX_HTML
    assert "const courseSentence = context.sentence || entry.sentence;" in INDEX_HTML
    assert "if (entry.review_source === 'manual')" in INDEX_HTML
    assert "return (analysis.examples || [])[0] || '暂时没有收藏语境。';" in INDEX_HTML
    assert "const contextSentence = resolveVocabContextSentence(entry, context, an);" in INDEX_HTML


def test_frontend_accepts_nested_pattern_and_new_correction_payloads():
    assert "data.pattern && typeof data.pattern === 'object'" in INDEX_HTML
    assert "sentence.pattern?.pattern_template" in INDEX_HTML
    assert "data.mode==='example' && data.example_sentence" in INDEX_HTML
    assert "makeClickableWords(data.example_sentence)" in INDEX_HTML
    assert "data.revised_sentence" in INDEX_HTML
    assert "data.naturalness_analysis" in INDEX_HTML
    assert "data.improvement_points" in INDEX_HTML
    assert "为什么不够地道" in INDEX_HTML
    assert "需要注意和改进" in INDEX_HTML


def test_ai_generated_vocab_example_has_natural_tts_control():
    assert "function speakAiExample" in INDEX_HTML
    assert "NaturalTTS.speak" in INDEX_HTML
    assert "neural:true" in INDEX_HTML
    assert "🔊 朗读示例" in INDEX_HTML
    assert "data.idiomatic_suggestion" in INDEX_HTML
    assert "data.status_label" in INDEX_HTML


def test_sentence_audio_prefers_original_media_and_uses_natural_browser_tts_fallback():
    assert 'audio.kind === \'youtube\'' in INDEX_HTML
    assert "playReviewYouTubeAudio" in INDEX_HTML
    assert "audio.url" in INDEX_HTML
    assert "speakReviewSentenceWithBrowserTts" in INDEX_HTML
    assert '/static/natural-tts.js' in INDEX_HTML
    assert "NaturalTTS.speak" in INDEX_HTML
    assert "/api/v2/lessons/sentence-audio/${sentence.id}" not in INDEX_HTML


def test_practice_cards_can_open_cached_attempt_history_and_review_errors():
    assert "查看练习记录" in INDEX_HTML
    assert "toggleVocabPracticeHistory" in INDEX_HTML
    assert "toggleSentencePracticeHistory" in INDEX_HTML
    assert "/api/practice/history" in INDEX_HTML
    assert "renderPracticeHistory" in INDEX_HTML
    assert "你的表达" in INDEX_HTML
    assert "最关键的问题" in INDEX_HTML
    assert "参考改写" in INDEX_HTML
    assert "更地道的表达" in INDEX_HTML
    assert "练习记录只保存用户实际提交的造句" in INDEX_HTML


def test_inline_javascript_is_syntax_valid_when_node_is_available():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    script = INDEX_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as tmp:
        tmp.write(script)
        path = Path(tmp.name)
    try:
        result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    finally:
        path.unlink(missing_ok=True)
