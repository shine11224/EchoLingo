import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from fastapi.testclient import TestClient


def test_workspace_page_renders(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    app = create_app()
    client = TestClient(app)

    lesson = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=abc123def45",
        video_id="abc123def45",
        title="Demo",
    )

    resp = client.get(f"/workspace/{lesson['id']}")
    assert resp.status_code == 200
    assert 'class="home-link" href="/" aria-label="返回首页"' in resp.text
    assert "youtube-player" in resp.text
    assert "subtitle-timeline" in resp.text
    assert "chat-panel" in resp.text
    assert "Current sentence" in resp.text
    assert "Content outline" in resp.text
    assert "save-sentence" in resp.text
    assert "vocab-chips" in resp.text
    assert "star-save" in resp.text
    assert "&#x52A0;&#x5165;&#x7CBE;&#x8BFB;" in resp.text
    assert "prev-sentence" in resp.text
    assert "next-sentence" in resp.text
    assert "jumpSentence" in resp.text
    assert "playback-rate" in resp.text
    assert "onPlaybackRateChange" in resp.text
    assert "learning-controls" in resp.text
    assert "word-popover" in resp.text
    assert "showWordMeaning" in resp.text
    assert "toggleWordSave" in resp.text
    assert "saved-word" in resp.text
    assert "readingHiddenWordSet.has(lower)" in resp.text
    assert "word-save-btn" in resp.text
    assert "ruby-position: under" in resp.text
    assert '<ruby class="reading-word-wrap">' in resp.text
    assert '<ruby class="lookup-word-wrap">' in resp.text
    assert '<rt class="word-gloss"' in resp.text
    assert "function formatReadingGloss" in resp.text
    assert "formatReadingGloss(gloss)" in resp.text
    assert "const isSaved = savedWordSet.has(lower);" in resp.text
    assert "const savedMeaning = typeof cachedMeaning === 'string'" in resp.text
    assert "mode === 'lookup' && isSavedWord && !readingHiddenWordSet.has(normalized)" in resp.text
    assert "chipsEl.innerHTML = '';" in resp.text
    assert "max-height: calc(100vh - 24px)" in resp.text
    assert "overscroll-behavior: contain" in resp.text
    assert "const popRect = pop.getBoundingClientRect();" in resp.text
    assert "rect.top - gap - popRect.height" in resp.text
    assert "function closeWordPopover()" in resp.text
    assert "if (!target.isConnected) return;" in resp.text
    assert "window.addEventListener('scroll', closeWordPopover, true);" in resp.text
    assert "window.addEventListener('resize', closeWordPopover);" in resp.text
    update_current_subtitle = resp.text.split("function updateCurrentSubtitle()", 1)[1].split(
        "function currentSentenceKey", 1
    )[0]
    assert "closeWordPopover();" in update_current_subtitle
    assert "reviewBookWordSet" in resp.text
    assert "addSavedWordToReview" in resp.text
    assert "/api/vocab-review/activate" in resp.text
    assert "加入复习本" in resp.text
    assert "local-media-player" in resp.text
    assert "initLocalMedia" in resp.text
    assert "media_url" in resp.text
    assert "Export HTML" in resp.text
    assert "exportReviewHtml" in resp.text
    assert "/review-export" in resp.text
    assert "sentence-tag-panel" in resp.text
    assert "/sentence-tags" in resp.text
    assert "addSentenceTag" in resp.text
    assert "function sentenceTagsForCategory(category)" in resp.text
    assert ".filter(tag => tag.category === category)" in resp.text
    assert 'onchange="updateSentenceTagNameOptions(this)"' in resp.text
    assert 'onchange="updateSentenceCustomTagVisibility(this)"' in resp.text
    assert "CUSTOM_SENTENCE_TAG_VALUE" in resp.text
    assert "#study-collection {" in resp.text
    assert ".study-collection-card .review-body { flex: 1 1 auto; }" in resp.text
    assert '<option value="chinese">中文</option>' in resp.text
    assert '<option value="bilingual">中英双语</option>' in resp.text
    assert 'id="translate-sentences-btn"' not in resp.text
    assert 'id="show-translation"' not in resp.text
    assert "applyTranslationSubtitleAvailability" in resp.text
    assert "sentenceUnits = data.sentence_units || buildSentenceUnits(segments)" in resp.text
    assert "renderReadingBlockTranslation" in resp.text
    assert 'class="reading-translation"' in resp.text
    assert "availableStudyModes.includes(lessonData.lesson_mode)" in resp.text
    assert "lessonData?.subtitle_status === 'pending'" in resp.text


def test_workspace_reading_batch_renderer_is_not_duplicated(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:batch-renderer",
        title="Reading passage",
        lesson_mode="reading",
    )

    resp = client.get(f"/workspace/{lesson['id']}")
    assert resp.status_code == 200
    render_reading_blocks = resp.text.split(
        "function renderReadingPassage", 1
    )[1].split("function renderReadingBlockText", 1)[0]

    assert render_reading_blocks.count("root.appendChild(fragment);") == 1
    assert render_reading_blocks.count("requestAnimationFrame(renderBatch);") == 2


def test_workspace_contains_reading_mode_mount_points(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    app = create_app()
    client = TestClient(app)

    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:test",
        title="Reading Passage 1",
        lesson_mode="reading",
    )

    resp = client.get(f"/workspace/{lesson['id']}")

    assert resp.status_code == 200
    assert 'id="reading-phase-a"' in resp.text
    assert 'id="reading-phase-b"' in resp.text
    assert 'id="study-mode-switch"' in resp.text
    assert 'data-study-mode="listening"' in resp.text
    assert 'data-study-mode="reading"' in resp.text
    assert "switchStudyMode" in resp.text
    assert "fetch(`/api/v2/lessons/${LESSON_ID}/mode`" in resp.text
    assert "function isReadingLesson" not in resp.text
    assert "availableStudyModes" in resp.text
    assert "activeStudyMode" in resp.text
    assert "applyStudyMode" in resp.text
    assert "loadReadingLesson" in resp.text
    assert "visibleText.matchAll(wordPattern)" in resp.text
    assert "classes.push('highlight-word')" in resp.text
    assert ".reading-passage .highlight-word" in resp.text
    assert "background: transparent;" in resp.text
    assert "/api/v2/lessons/${LESSON_ID}/words" in resp.text
    assert "readingHiddenWordSet.has(normalized)" in resp.text
    assert 'id="reading-selection-popover"' in resp.text
    assert 'data-action="save-target"' in resp.text
    assert 'data-action="translate"' in resp.text
    assert 'data-action="analyze"' in resp.text
    assert 'data-action="ask"' in resp.text
    assert 'data-action="play-sentence"' in resp.text
    assert "captureReadingSelection" in resp.text
    assert "matchSelectedReadingSentence" in resp.text
    assert 'data-segment-index=' in resp.text
    assert 'data-start-seconds=' in resp.text
    assert 'data-end-seconds=' in resp.text
    assert ".reading-play-button" in resp.text
    assert "playReadingRange" in resp.text
    assert "if (lessonData.video_id)" in resp.text
    assert "if (!lessonData.media_url) return;" in resp.text
    assert "audio.src = src;" in resp.text
    assert "startPendingYouTubeReadingRange" in resp.text
    assert "initPlayer({allowReading: true})" in resp.text
    assert "stopReadingRange" in resp.text
    assert "readingPlaybackEnd" in resp.text
    assert "readingPlaybackTimeHandler" in resp.text
    assert "audio.removeEventListener('timeupdate', readingPlaybackTimeHandler)" in resp.text
    assert "block.sentences" in resp.text
    assert "offscreen-playback-engine" in resp.text
    assert "reading_selection" in resp.text
    assert "selected_text" in resp.text
    assert "requestReadingSelectionTranslation" in resp.text
    assert "/translate-selection" in resp.text
    assert "正在使用混元翻译" in resp.text
    assert "data.cached && data.translation_status === 'ready'" in resp.text
    assert 'id="reading-context-clear"' in resp.text
    assert "clearReadingChatSelection" in resp.text
    assert "readingSelectionRequestToken" in resp.text
    assert "loadChatHistory" in resp.text
    assert "/api/v2/chat/history/${LESSON_ID}" in resp.text
    assert "AI 正在思考" in resp.text
    assert "AbortController" in resp.text
    assert 'id="content-outline-panel"' in resp.text
    assert 'id="outline-resize-handle"' in resp.text
    assert 'data-resize-panel="study-collection-panel"' in resp.text
    assert 'id="collection-resize-handle"' in resp.text
    assert "initSidebarPanelResizers" in resp.text
    assert "english-tool:sidebar-panel-heights" in resp.text
    assert 'role="separator"' in resp.text
    assert 'aria-orientation="horizontal"' in resp.text
    assert 'id="ai-focus-toggle"' in resp.text
    assert "toggleAiFocusMode" in resp.text
    assert "SIDEBAR_PANEL_HEIGHTS_KEY" in resp.text
    assert "SIDEBAR_PANEL_CONFIG" in resp.text
    assert "sidebarPreferredPanelHeights" in resp.text
    assert "applySidebarPanelHeights" in resp.text
    assert "setPointerCapture" in resp.text
    assert "window.addEventListener('pointermove'" in resp.text
    assert "lostpointercapture" in resp.text
    assert "event.key === 'End'" in resp.text
    assert "localStorage.setItem" in resp.text
    assert "ai-focus-mode" in resp.text
    assert ".reading-saved-list {\n      flex: 1;\n      min-height: 0;\n      overflow-y: auto;" in resp.text
    assert "READING_RENDER_BATCH_SIZE" in resp.text
    assert "let readingLoadPromise = null;" in resp.text
    assert "const readingPromise = loadReadingLesson({render: activeStudyMode === 'reading'});" in resp.text
    assert "const listeningPromise = loadSubtitles({render: activeStudyMode !== 'reading'});" in resp.text
    assert "await (activeStudyMode === 'reading' ? readingPromise : listeningPromise);" in resp.text
    assert "if (!readingLoadPromise)" in resp.text
    assert "requestAnimationFrame" in resp.text
    assert 'id="chat-session-select"' in resp.text
    assert 'id="chat-new-session"' in resp.text
    assert 'id="chat-export-session"' in resp.text
    assert "loadChatSessions" in resp.text
    assert "newChatSession" in resp.text
    assert "exportChatSession" in resp.text
    assert "/api/v2/chat/sessions/${LESSON_ID}" in resp.text
    assert "session_id: sessionId" in resp.text
    assert '<script src="/static/voice-input.js?v=20260715-1"></script>' in resp.text
    assert 'id="chat-voice-language"' in resp.text
    assert 'id="chat-voice-toggle"' in resp.text
    assert "createVoiceInput" in resp.text
    assert "onInterim" in resp.text
    assert "onFinal" in resp.text
    assert "SpeechRecognition" not in resp.text
    assert "formatChatMessageContent" in resp.text
    assert 'class="chat-message-body"' in resp.text
    assert ".chat-message-body p" in resp.text
    assert ".chat-message-body ul" in resp.text
    assert ".chat-message-body blockquote" in resp.text
    assert "white-space: pre-wrap" in resp.text
    assert 'id="generate-outline-summary"' in resp.text
    assert 'id="document-summary"' in resp.text
    assert "requestDocumentOutline" in resp.text
    assert "loadCachedDocumentOutline" in resp.text
    assert "loadCachedDocumentOutline();" in resp.text
    assert "renderDocumentOutline(data.outline, true)" in resp.text
    assert "jumpToDocumentAnchor" in resp.text
    assert 'id="study-collection"' in resp.text
    assert 'class="review-action review-action--intensive"' in resp.text
    assert "openIntensiveExport" in resp.text
    assert "`/workspace/${LESSON_ID}/intensive`" in resp.text
    assert "进入精读" in resp.text
    assert "<h2>Quick review</h2>" not in resp.text
    assert resp.text.index('id="chat-messages"') < resp.text.index('class="chat-context-row"')
    assert resp.text.index('class="chat-context-row"') < resp.text.index('class="chat-input-row"')

    toggle_word_save = resp.text.split("async function toggleWordSave", 1)[1].split(
        "function updateReviewSummary", 1
    )[0]
    assert "refreshReadingWordStates();" in toggle_word_save
    assert "renderReadingPassage();" not in toggle_word_save


def test_reading_sentences_have_explicit_bookmark_controls(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from fastapi.templating import Jinja2Templates
    from webapp.fastapi_routes import pages

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    template_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "templates")
    monkeypatch.setattr(pages, "templates", Jinja2Templates(directory=template_dir))
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:sentence-bookmarks",
        title="Sentence bookmarks",
        lesson_mode="reading",
    )

    resp = client.get(f"/workspace/{lesson['id']}")

    assert resp.status_code == 200
    assert 'class="reading-sentence"' in resp.text
    assert 'class="reading-sentence-bookmark' in resp.text
    assert 'aria-label="${saved ? \'取消收藏整句\' : \'收藏整句\'}"' in resp.text
    assert "data-sentence-key=" in resp.text
    assert "toggleReadingSentenceSave" in resp.text
    assert "refreshReadingSentenceSaveStates();" in resp.text
    assert "Save selected sentence" not in resp.text


def test_reading_and_listening_selection_only_offer_explicit_word_or_phrase_save(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from fastapi.templating import Jinja2Templates
    from webapp.fastapi_routes import pages

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    template_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "templates")
    monkeypatch.setattr(pages, "templates", Jinja2Templates(directory=template_dir))
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=selection123",
        video_id="selection123",
        title="Selection targets",
    )

    resp = client.get(f"/workspace/{lesson['id']}")

    assert resp.status_code == 200
    assert 'id="reading-selection-kind"' in resp.text
    assert 'data-action="save-target"' in resp.text
    assert "收藏短语" in resp.text
    assert "captureReadingSelection" in resp.text
    assert "captureListeningSelection" in resp.text
    assert "readingSelectionTargetType" in resp.text
    assert "target_type: readingSelectionTargetType" in resp.text
    assert "mode: readingSelectionMode" in resp.text
    assert "sentence_key: readingSelectionSentenceKey" in resp.text
    assert "lesson_id: LESSON_ID" in resp.text


def test_word_lookup_never_implicitly_activates_review(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app
    from fastapi.templating import Jinja2Templates
    from webapp.fastapi_routes import pages

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    template_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "templates")
    monkeypatch.setattr(pages, "templates", Jinja2Templates(directory=template_dir))
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=lookup123",
        video_id="lookup123",
        title="Lookup boundary",
    )

    resp = client.get(f"/workspace/{lesson['id']}")
    show_word_meaning = resp.text.split("async function showWordMeaning", 1)[1].split(
        "function closeWordPopover", 1
    )[0]

    assert "activateWordReview" not in show_word_meaning
    assert "toggleWordSave" in resp.text


def test_reading_selection_can_jump_to_matching_intensive_sentence(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="reading_text",
        source_url="manual:selection-jump",
        title="Selection Jump",
        lesson_mode="reading",
    )

    workspace = client.get(f"/workspace/{lesson['id']}")
    intensive = client.get(f"/workspace/{lesson['id']}/intensive")

    assert workspace.status_code == 200
    assert 'data-action="intensive"' in workspace.text
    assert "matchSelectedReadingSentence" in workspace.text
    assert "sentence_key" in workspace.text
    assert "explicitSentenceKey" in workspace.text
    assert "readingSelectionRange" in workspace.text
    assert intensive.status_code == 200
    assert "targetSentenceKey" in intensive.text
    assert "focusTargetSentence" in intensive.text
    assert "scrollIntoView" in intensive.text


def test_index_hides_fixed_ai_course_settings(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    app = create_app()
    client = TestClient(app)

    resp = client.get("/")

    assert resp.status_code == 200
    assert 'id="ai-segmentation"' not in resp.text
    assert 'id="ai-translation"' not in resp.text
    assert 'id="ai-ipa"' not in resp.text
    assert "ai_segmentation: false" in resp.text
    assert "ai_ipa: false" in resp.text


def test_index_contains_reading_text_controls(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    app = create_app()
    client = TestClient(app)

    resp = client.get("/")

    assert resp.status_code == 200
    assert 'id="reading-text-input"' in resp.text
    assert "reading_text" in resp.text
    assert 'id="reading-file-input"' in resp.text
    assert 'id="text-tts"' in resp.text
    assert 'id="text-tts" disabled' not in resp.text
    assert 'id="text-translation"' not in resp.text
    assert 'id="text-ipa"' not in resp.text
    assert "tts: document.getElementById('text-tts').checked" in resp.text
    assert "form.append('tts', document.getElementById('text-tts').checked ? 'true' : 'false')" in resp.text
    assert "['youtube', 'bilibili', 'local'].includes(_detectedSource)" in resp.text
    assert '.txt,.md,.docx,.pdf' in resp.text


def test_index_contains_recent_cards_and_paginated_course_library(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())

    resp = client.get("/")

    assert resp.status_code == 200
    assert 'id="lesson-grid"' in resp.text
    assert 'id="course-library-list"' in resp.text
    assert 'id="course-search"' in resp.text
    assert 'id="course-mode-filter"' in resp.text
    assert 'id="course-status-filter"' in resp.text
    assert 'id="course-page-prev"' in resp.text
    assert 'id="course-page-next"' in resp.text
    assert "COURSE_PAGE_SIZE = 15" in resp.text
    assert "slice(0, 4)" in resp.text
    assert 'id="lesson-count"' not in resp.text
    assert 'id="course-tag-filter"' in resp.text
    assert "buildCourseMenu" in resp.text
    assert "courseChipsHtml" in resp.text
    assert 'data-course-manage="delete"' in resp.text
    assert 'data-course-manage="tags"' in resp.text
    assert "重点词" in resp.text
    assert "/api/v2/lessons/library?include_archived=1" in resp.text
    assert "openCourseMode" in resp.text
    assert "renderCourseLibrary" in resp.text


def test_index_keeps_vocab_review_and_adds_sentence_review_tab(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())

    resp = client.get("/")

    assert resp.status_code == 200
    assert 'id="tab-btn-vocab"' not in resp.text
    assert 'id="tab-btn-sentences"' not in resp.text
    assert '>⚙️ 设置</button>' in resp.text
    assert 'class="review-entry-grid"' in resp.text
    assert "词汇记忆工坊" in resp.text
    assert "句子复习" in resp.text
    assert "今日任务" not in resp.text
    assert 'id="tab-sentences"' in resp.text
    assert 'id="sentence-library-shell"' in resp.text
    assert 'id="sentence-review-list"' in resp.text
    assert 'id="sentence-review-search"' in resp.text
    assert 'id="sentence-review-time-filter"' in resp.text
    assert "/api/v2/lessons/sentence-review" in resp.text
    assert 'id="sentence-filter-bar"' in resp.text
    assert "speakReviewSentence" in resp.text
    assert "recordSentenceListeningResult" in resp.text
    assert "openSentenceAnalysis" in resp.text
    assert "toggleSentenceVoiceInput" in resp.text
    assert "批改表达" in resp.text
    assert 'id="story-word-input"' in resp.text
    assert "STORY_WORD_LIMIT = 20" in resp.text
    assert "VOCAB_PAGE_SIZE = 30" in resp.text
    assert "加入记忆故事" in resp.text
    assert "uploadReadingFile" in resp.text
    assert "/api/v2/lessons/reading/upload" in resp.text
    assert "pollReadingUpload" in resp.text
    assert "/api/v2/lessons/reading/upload-status/" in resp.text
    assert 'id="ai-translation"' not in resp.text
    assert 'id="ai-ipa"' not in resp.text
    assert "translate: true" in resp.text
    assert 'id="whisper-model"' in resp.text
    assert "waitForTranslationReadiness" in resp.text
    assert "translation_ready" in resp.text
