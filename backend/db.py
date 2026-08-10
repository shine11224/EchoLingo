import os
import sqlite3, json
import re
import contextvars
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from contextlib import contextmanager
from typing import Optional

DB_PATH = Path(__file__).resolve().parents[1] / "resources" / "vocab.db"
V2_TAG_CATEGORIES = {"vocabulary", "pronunciation", "structure", "expression", "practice"}

# ── 多用户：数据库路径三级解析 contextvar → ELT_USER_DB → 默认 ──
_current_db_path: contextvars.ContextVar[Optional[Path]] = contextvars.ContextVar(
    "elt_db_path", default=None
)
_initialized_paths: set = set()


def current_db_path() -> Path:
    """当前请求/线程应使用的 SQLite 路径。"""
    p = _current_db_path.get()
    if p is not None:
        return p
    env = os.environ.get("ELT_USER_DB")
    if env:
        return Path(env)
    return DB_PATH


def set_current_db_path(path: Path) -> contextvars.Token:
    return _current_db_path.set(Path(path))


def reset_current_db_path(token: contextvars.Token) -> None:
    _current_db_path.reset(token)


def current_user_root() -> Optional[Path]:
    """多用户请求上下文返回 resources/users/<username>/；单用户（含 ELT_USER_DB 覆盖）返回 None。

    判定依据是 contextvar 是否被中间件显式设置，与 ELT_USER_DB/默认路径无关。
    """
    p = _current_db_path.get()
    return p.parent if p is not None else None


def spawn_with_db_context(target, *args, name: str | None = None, **kwargs) -> threading.Thread:
    """后台线程不继承 contextvar，用 copy_context 显式传播用户数据库上下文。"""
    ctx = contextvars.copy_context()
    t = threading.Thread(target=lambda: ctx.run(target, *args, **kwargs), daemon=True, name=name)
    t.start()
    return t


@contextmanager
def _db():
    path = current_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path not in _initialized_paths:
        init_db(path)
        _initialized_paths.add(path)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path: Path | None = None):
    _token = None
    if path is not None:
        _token = set_current_db_path(path)
        _initialized_paths.add(path)
    try:
        with _db() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS words (
                    word             TEXT PRIMARY KEY,
                    count            INTEGER NOT NULL DEFAULT 0,
                    first_studied    TEXT    NOT NULL DEFAULT '',
                    last_studied     TEXT    NOT NULL DEFAULT '',
                    level            TEXT    NOT NULL DEFAULT '',
                    cached_analysis  TEXT
                );
                CREATE TABLE IF NOT EXISTS contexts (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    word     TEXT NOT NULL REFERENCES words(word) ON DELETE CASCADE,
                    lesson   TEXT NOT NULL DEFAULT '',
                    sentence TEXT NOT NULL DEFAULT '',
                    UNIQUE(word, lesson, sentence)
                );
                CREATE TABLE IF NOT EXISTS stories (
                    cache_key  TEXT PRIMARY KEY,
                    words_json TEXT NOT NULL,
                    story      TEXT NOT NULL,
                    date       TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS story_history (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    cache_key     TEXT NOT NULL,
                    words_json    TEXT NOT NULL,
                    story         TEXT NOT NULL,
                    date          TEXT NOT NULL,
                    learner_level TEXT NOT NULL DEFAULT '',
                    theme         TEXT NOT NULL DEFAULT '',
                    created_at    TEXT NOT NULL DEFAULT '',
                    UNIQUE(cache_key, story)
                );
                CREATE INDEX IF NOT EXISTS idx_story_history_created
                    ON story_history(created_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS known_words (
                    word      TEXT PRIMARY KEY,
                    added_at  TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS lessons (
                    filename       TEXT PRIMARY KEY,
                    title          TEXT NOT NULL DEFAULT '',
                    source_type    TEXT NOT NULL DEFAULT 'local_video',
                    source_url     TEXT NOT NULL DEFAULT '',
                    sentence_count INTEGER NOT NULL DEFAULT 0,
                    duration       INTEGER NOT NULL DEFAULT 0,
                    created_at     TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS study_sessions (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_filename      TEXT    NOT NULL,
                    started_at           TEXT    NOT NULL DEFAULT '',
                    last_active_at       TEXT    NOT NULL DEFAULT '',
                    current_sentence_idx INTEGER NOT NULL DEFAULT 0,
                    total_sentences      INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(lesson_filename)
                );
                CREATE TABLE IF NOT EXISTS sentence_marks (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_filename TEXT    NOT NULL,
                    sentence_idx    INTEGER NOT NULL,
                    marked_at       TEXT    NOT NULL DEFAULT '',
                    UNIQUE(lesson_filename, sentence_idx)
                );
                CREATE TABLE IF NOT EXISTS lesson_reflections (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename    TEXT NOT NULL,
                    reflection  TEXT NOT NULL DEFAULT '',
                    created_at  TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS practice_attempts (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename     TEXT NOT NULL,
                    sentence_idx INTEGER NOT NULL,
                    user_input   TEXT NOT NULL DEFAULT '',
                    ai_feedback  TEXT NOT NULL DEFAULT '',
                    created_at   TEXT NOT NULL DEFAULT ''
                );

                -- v2 tables for video workspace
                CREATE TABLE IF NOT EXISTS v2_lessons (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type     TEXT NOT NULL,
                    source_url      TEXT NOT NULL,
                    video_id        TEXT NOT NULL DEFAULT '',
                    lesson_mode     TEXT NOT NULL DEFAULT 'listening',
                    media_url       TEXT NOT NULL DEFAULT '',
                    media_kind      TEXT NOT NULL DEFAULT '',
                    title           TEXT NOT NULL DEFAULT '',
                    duration        REAL NOT NULL DEFAULT 0,
                    subtitle_status TEXT NOT NULL DEFAULT 'pending',
                    summary_status  TEXT NOT NULL DEFAULT 'pending',
                    subtitle_error  TEXT NOT NULL DEFAULT '',
                    summary_error   TEXT NOT NULL DEFAULT '',
                    translation_requested INTEGER NOT NULL DEFAULT 0,
                    translation_status TEXT NOT NULL DEFAULT 'disabled',
                    translation_done INTEGER NOT NULL DEFAULT 0,
                    translation_total INTEGER NOT NULL DEFAULT 0,
                    translation_buffer_seconds REAL NOT NULL DEFAULT 0,
                    translation_rate REAL NOT NULL DEFAULT 0,
                    translation_ready INTEGER NOT NULL DEFAULT 0,
                    translation_error TEXT NOT NULL DEFAULT '',
                    created_at      TEXT NOT NULL DEFAULT '',
                    updated_at      TEXT NOT NULL DEFAULT '',
                    archived        INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(source_type, source_url)
                );

                CREATE TABLE IF NOT EXISTS v2_media_uploads (
                    id                TEXT PRIMARY KEY,
                    original_filename TEXT NOT NULL DEFAULT '',
                    stored_relpath    TEXT NOT NULL DEFAULT '',
                    media_kind        TEXT NOT NULL DEFAULT '',
                    size_bytes        INTEGER NOT NULL DEFAULT 0,
                    duration_seconds  REAL NOT NULL DEFAULT 0,
                    status            TEXT NOT NULL DEFAULT 'ready',
                    created_at        TEXT NOT NULL DEFAULT '',
                    consumed_at       TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS v2_subtitle_segments (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_id  INTEGER NOT NULL REFERENCES v2_lessons(id) ON DELETE CASCADE,
                    idx        INTEGER NOT NULL,
                    start      REAL NOT NULL DEFAULT 0,
                    end        REAL NOT NULL DEFAULT 0,
                    text       TEXT NOT NULL DEFAULT '',
                    normalized TEXT NOT NULL DEFAULT '',
                    UNIQUE(lesson_id, idx)
                );

                CREATE TABLE IF NOT EXISTS v2_lesson_summaries (
                    lesson_id    INTEGER PRIMARY KEY REFERENCES v2_lessons(id) ON DELETE CASCADE,
                    summary      TEXT NOT NULL DEFAULT '',
                    outline_json TEXT NOT NULL DEFAULT '[]',
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    status       TEXT NOT NULL DEFAULT 'pending',
                    updated_at   TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS v2_lesson_progress (
                    lesson_id             INTEGER PRIMARY KEY REFERENCES v2_lessons(id) ON DELETE CASCADE,
                    last_position_seconds REAL NOT NULL DEFAULT 0,
                    last_segment_index    INTEGER NOT NULL DEFAULT 0,
                    updated_at            TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS v2_chat_sessions (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_id  INTEGER NOT NULL REFERENCES v2_lessons(id) ON DELETE CASCADE,
                    title      TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS v2_chat_messages (
                    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_id              INTEGER NOT NULL REFERENCES v2_lessons(id) ON DELETE CASCADE,
                    session_id             INTEGER REFERENCES v2_chat_sessions(id) ON DELETE CASCADE,
                    timestamp_seconds      REAL NOT NULL DEFAULT 0,
                    selected_start_seconds REAL,
                    selected_end_seconds   REAL,
                    selected_segment_ids   TEXT NOT NULL DEFAULT '[]',
                    user_message           TEXT NOT NULL DEFAULT '',
                    ai_response            TEXT NOT NULL DEFAULT '',
                    context_mode           TEXT NOT NULL DEFAULT 'auto',
                    coverage_status        TEXT NOT NULL DEFAULT '',
                    external_knowledge_used INTEGER NOT NULL DEFAULT 0,
                    citations_json         TEXT NOT NULL DEFAULT '[]',
                    unsupported_json       TEXT NOT NULL DEFAULT '[]',
                    created_at             TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS v2_phase_b_sentences (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_id     INTEGER NOT NULL REFERENCES v2_lessons(id) ON DELETE CASCADE,
                    sentence_id   INTEGER REFERENCES v2_sentences(id) ON DELETE SET NULL,
                    segment_index INTEGER NOT NULL,
                    start_seconds REAL NOT NULL DEFAULT 0,
                    end_seconds   REAL NOT NULL DEFAULT 0,
                    text          TEXT NOT NULL DEFAULT '',
                    created_at    TEXT NOT NULL DEFAULT '',
                    UNIQUE(lesson_id, segment_index)
                );

                CREATE TABLE IF NOT EXISTS v2_sentences (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    normalized_text TEXT NOT NULL UNIQUE,
                    text            TEXT NOT NULL DEFAULT '',
                    translation     TEXT NOT NULL DEFAULT '',
                    phonetics       TEXT NOT NULL DEFAULT '',
                    audio_url       TEXT NOT NULL DEFAULT '',
                    review_count    INTEGER NOT NULL DEFAULT 0,
                    listening_result TEXT NOT NULL DEFAULT 'untested',
                    archived        INTEGER NOT NULL DEFAULT 0,
                    last_reviewed_at TEXT NOT NULL DEFAULT '',
                    next_review     TEXT NOT NULL DEFAULT '',
                    first_seen_at   TEXT NOT NULL DEFAULT '',
                    last_seen_at    TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS v2_sentence_listening_attempts (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    sentence_id INTEGER NOT NULL REFERENCES v2_sentences(id) ON DELETE CASCADE,
                    result      TEXT NOT NULL CHECK(result IN ('understood', 'not_understood')),
                    created_at  TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS v2_sentence_patterns (
                    sentence_id      INTEGER PRIMARY KEY REFERENCES v2_sentences(id) ON DELETE CASCADE,
                    pattern_template TEXT NOT NULL DEFAULT '',
                    scenario_cn      TEXT NOT NULL DEFAULT '',
                    analysis_json    TEXT NOT NULL DEFAULT '{}',
                    updated_at       TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS v2_tags (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    category   TEXT NOT NULL,
                    name       TEXT NOT NULL,
                    source     TEXT NOT NULL DEFAULT 'user',
                    created_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(category, name)
                );

                CREATE TABLE IF NOT EXISTS v2_sentence_tags (
                    sentence_id INTEGER NOT NULL REFERENCES v2_sentences(id) ON DELETE CASCADE,
                    tag_id      INTEGER NOT NULL REFERENCES v2_tags(id) ON DELETE CASCADE,
                    created_at  TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(sentence_id, tag_id)
                );

                CREATE TABLE IF NOT EXISTS v2_lesson_words (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_id  INTEGER NOT NULL REFERENCES v2_lessons(id) ON DELETE CASCADE,
                    word       TEXT NOT NULL REFERENCES words(word) ON DELETE CASCADE,
                    sentence   TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(lesson_id, word)
                );

                CREATE TABLE IF NOT EXISTS word_review_items (
                    word       TEXT PRIMARY KEY REFERENCES words(word) ON DELETE CASCADE,
                    source     TEXT NOT NULL DEFAULT '',
                    lesson_id  INTEGER REFERENCES v2_lessons(id) ON DELETE SET NULL,
                    target_type TEXT NOT NULL DEFAULT 'word',
                    lemma       TEXT NOT NULL DEFAULT '',
                    display_text TEXT NOT NULL DEFAULT '',
                    familiarity TEXT NOT NULL DEFAULT 'unrated',
                    archived    INTEGER NOT NULL DEFAULT 0,
                    mastered    INTEGER NOT NULL DEFAULT 0,
                    tags        TEXT NOT NULL DEFAULT '[]',
                    added_at   TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS v2_practice_attempts (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    practice_type   TEXT NOT NULL,
                    target          TEXT NOT NULL DEFAULT '',
                    target_type     TEXT NOT NULL DEFAULT 'word',
                    sentence_id     INTEGER REFERENCES v2_sentences(id) ON DELETE SET NULL,
                    lesson_id       INTEGER REFERENCES v2_lessons(id) ON DELETE SET NULL,
                    user_input      TEXT NOT NULL,
                    input_method    TEXT NOT NULL DEFAULT 'keyboard',
                    hint_used       INTEGER NOT NULL DEFAULT 0,
                    scenario_cn     TEXT NOT NULL DEFAULT '',
                    hint_text       TEXT NOT NULL DEFAULT '',
                    source_context  TEXT NOT NULL DEFAULT '',
                    verdict         TEXT NOT NULL,
                    key_issue       TEXT NOT NULL DEFAULT '',
                    revised_sentence TEXT NOT NULL DEFAULT '',
                    idiomatic_suggestion TEXT NOT NULL DEFAULT '',
                    result_json     TEXT NOT NULL,
                    created_at      TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS v2_lesson_hidden_words (
                    lesson_id  INTEGER NOT NULL REFERENCES v2_lessons(id) ON DELETE CASCADE,
                    word       TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(lesson_id, word)
                );

                CREATE TABLE IF NOT EXISTS v2_reading_blocks (
                    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_id          INTEGER NOT NULL REFERENCES v2_lessons(id) ON DELETE CASCADE,
                    block_index        INTEGER NOT NULL,
                    text               TEXT NOT NULL DEFAULT '',
                    start_seconds      REAL,
                    end_seconds        REAL,
                    sentences_json     TEXT NOT NULL DEFAULT '[]',
                    source_segment_ids TEXT NOT NULL DEFAULT '[]',
                    created_at         TEXT NOT NULL DEFAULT '',
                    UNIQUE(lesson_id, block_index)
                );

                CREATE TABLE IF NOT EXISTS v2_document_outlines (
                    lesson_id    INTEGER PRIMARY KEY REFERENCES v2_lessons(id) ON DELETE CASCADE,
                    content_hash TEXT NOT NULL DEFAULT '',
                    outline_json TEXT NOT NULL DEFAULT '{}',
                    updated_at   TEXT NOT NULL DEFAULT ''
                );
            """)
            conn.execute(
                """
                INSERT OR IGNORE INTO story_history
                    (cache_key, words_json, story, date, learner_level, theme, created_at)
                SELECT cache_key, words_json, story, date, '', '', date || 'T00:00:00+00:00'
                FROM stories
                """
            )
            # 迁移：给已有 lessons 表加 archived 字段
            try:
                conn.execute("ALTER TABLE lessons ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
            except Exception:
                pass
            # 迁移：给已有 words 表加 next_review 字段
            try:
                conn.execute("ALTER TABLE words ADD COLUMN next_review TEXT NOT NULL DEFAULT ''")
            except Exception:
                pass
            # 迁移：字幕段存词级时间戳（paraformer words），用于精确断句
            try:
                conn.execute("ALTER TABLE v2_subtitle_segments ADD COLUMN words_json TEXT NOT NULL DEFAULT ''")
            except Exception:
                pass
            # 旧 words 记录无法区分“导入词库”和“主动深入学习”，不能批量迁入复习本。
            conn.execute("DELETE FROM word_review_items WHERE source = 'legacy_review'")
            # 查词和旧批改接口都不是显式收藏；清理旧版本误写的成员关系。
            conn.execute("DELETE FROM word_review_items WHERE source IN ('lookup', 'practice')")
            practice_attempt_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(v2_practice_attempts)").fetchall()
            }
            for column_sql in (
                "ALTER TABLE v2_lessons ADD COLUMN lesson_mode TEXT NOT NULL DEFAULT 'listening'",
                "ALTER TABLE v2_lessons ADD COLUMN media_url TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE v2_lessons ADD COLUMN media_kind TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE v2_phase_b_sentences ADD COLUMN sentence_id INTEGER REFERENCES v2_sentences(id) ON DELETE SET NULL",
                "ALTER TABLE v2_chat_messages ADD COLUMN session_id INTEGER REFERENCES v2_chat_sessions(id) ON DELETE CASCADE",
                "ALTER TABLE v2_chat_messages ADD COLUMN coverage_status TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE v2_chat_messages ADD COLUMN external_knowledge_used INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE v2_chat_messages ADD COLUMN citations_json TEXT NOT NULL DEFAULT '[]'",
                "ALTER TABLE v2_chat_messages ADD COLUMN unsupported_json TEXT NOT NULL DEFAULT '[]'",
                "ALTER TABLE v2_reading_blocks ADD COLUMN start_seconds REAL",
                "ALTER TABLE v2_reading_blocks ADD COLUMN end_seconds REAL",
                "ALTER TABLE v2_reading_blocks ADD COLUMN sentences_json TEXT NOT NULL DEFAULT '[]'",
                "ALTER TABLE v2_reading_blocks ADD COLUMN source_segment_ids TEXT NOT NULL DEFAULT '[]'",
                "ALTER TABLE v2_lessons ADD COLUMN translation_requested INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE v2_lessons ADD COLUMN translation_status TEXT NOT NULL DEFAULT 'disabled'",
                "ALTER TABLE v2_lessons ADD COLUMN translation_done INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE v2_lessons ADD COLUMN translation_total INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE v2_lessons ADD COLUMN translation_buffer_seconds REAL NOT NULL DEFAULT 0",
                "ALTER TABLE v2_lessons ADD COLUMN translation_rate REAL NOT NULL DEFAULT 0",
                "ALTER TABLE v2_lessons ADD COLUMN translation_ready INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE v2_lessons ADD COLUMN translation_error TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE v2_lessons ADD COLUMN archived INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE v2_lessons ADD COLUMN tags TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE v2_sentences ADD COLUMN last_reviewed_at TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE v2_sentences ADD COLUMN next_review TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE v2_sentences ADD COLUMN phonetics_source TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE v2_sentences ADD COLUMN listening_result TEXT NOT NULL DEFAULT 'untested'",
                "ALTER TABLE v2_sentences ADD COLUMN archived INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE v2_sentences ADD COLUMN saved_manually INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE word_review_items ADD COLUMN target_type TEXT NOT NULL DEFAULT 'word'",
                "ALTER TABLE word_review_items ADD COLUMN lemma TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE word_review_items ADD COLUMN display_text TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE word_review_items ADD COLUMN familiarity TEXT NOT NULL DEFAULT 'unrated'",
                "ALTER TABLE word_review_items ADD COLUMN archived INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE word_review_items ADD COLUMN mastered INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE word_review_items ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'",
                "ALTER TABLE v2_practice_attempts ADD COLUMN target_type TEXT NOT NULL DEFAULT 'word'",
                "ALTER TABLE v2_practice_attempts ADD COLUMN hint_text TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE v2_practice_attempts ADD COLUMN source_context TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE v2_practice_attempts ADD COLUMN key_issue TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE v2_practice_attempts ADD COLUMN revised_sentence TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE v2_practice_attempts ADD COLUMN idiomatic_suggestion TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE v2_sentence_patterns ADD COLUMN analysis_json TEXT NOT NULL DEFAULT '{}'",
            ):
                try:
                    conn.execute(column_sql)
                except Exception:
                    pass
            if "target_type" not in practice_attempt_columns:
                conn.execute(
                    "UPDATE v2_practice_attempts SET target_type=practice_type"
                    " WHERE practice_type IN ('word', 'phrase', 'pattern')"
                )
            # 旧版只保存 good/hard/again 对应的下次复习日；迁移为听力二态。
            legacy_reviews = conn.execute(
                """
                SELECT id, review_count, last_reviewed_at, next_review, listening_result
                FROM v2_sentences
                WHERE review_count > 0 AND listening_result='untested'
                """
            ).fetchall()
            for legacy in legacy_reviews:
                result = "not_understood"
                try:
                    reviewed_day = date.fromisoformat(str(legacy["last_reviewed_at"])[:10])
                    next_day = date.fromisoformat(str(legacy["next_review"])[:10])
                    if (next_day - reviewed_day).days > 3:
                        result = "understood"
                except (TypeError, ValueError):
                    pass
                conn.execute(
                    "UPDATE v2_sentences SET listening_result=? WHERE id=?",
                    (result, legacy["id"]),
                )
            conn.execute("""
                INSERT INTO v2_chat_sessions (lesson_id, title, created_at, updated_at)
                SELECT messages.lesson_id, 'Legacy conversation',
                       COALESCE(MIN(messages.created_at), ''),
                       COALESCE(MAX(messages.created_at), '')
                FROM v2_chat_messages AS messages
                WHERE messages.session_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM v2_chat_sessions AS sessions
                      WHERE sessions.lesson_id = messages.lesson_id
                        AND sessions.title = 'Legacy conversation'
                  )
                GROUP BY messages.lesson_id
            """)
            conn.execute("""
                UPDATE v2_chat_messages
                SET session_id = (
                    SELECT sessions.id FROM v2_chat_sessions AS sessions
                    WHERE sessions.lesson_id = v2_chat_messages.lesson_id
                      AND sessions.title = 'Legacy conversation'
                    ORDER BY sessions.id ASC LIMIT 1
                )
                WHERE session_id IS NULL
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_v2_chat_messages_lesson_session_id"
                " ON v2_chat_messages (lesson_id, session_id, id)"
            )
            for category, names in {
                "vocabulary": ("重点词汇", "搭配", "短语动词", "习语表达", "学术表达"),
                "pronunciation": ("连读", "弱读", "略读/吞音", "重音", "语调", "意群停顿"),
                "structure": ("可复用句式", "从句结构", "比较对比", "因果逻辑", "让步转折", "条件假设"),
                "expression": ("提出观点", "解释原因", "举例说明", "总结归纳", "反驳/让步", "描述趋势", "提出建议"),
                "practice": ("跟读", "听写", "中译英", "背诵", "口语复用", "写作复用"),
            }.items():
                for name in names:
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO v2_tags (category, name, source, created_at) VALUES (?, ?, 'system', ?)",
                            (category, name, _now_iso()),
                        )
                    except Exception:
                        pass
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO v2_lesson_words (lesson_id, word, sentence, created_at)
                    SELECT v2_lessons.id, contexts.word, contexts.sentence, COALESCE(v2_lessons.updated_at, '')
                    FROM contexts
                    JOIN v2_lessons ON v2_lessons.title = contexts.lesson
                    JOIN words ON words.word = contexts.word
                    WHERE contexts.lesson != ''
                """)
            except Exception:
                pass
    finally:
        if _token is not None:
            reset_current_db_path(_token)


def get_all_words() -> dict:
    """Reconstruct the old vocab_log.json dict format (for frontend compatibility)."""
    with _db() as conn:
        words    = conn.execute("SELECT * FROM words").fetchall()
        contexts = conn.execute(
            "SELECT word, lesson, sentence FROM contexts ORDER BY id"
        ).fetchall()

    ctx_map: dict[str, list] = {}
    for row in contexts:
        ctx_map.setdefault(row["word"], []).append(
            {"lesson": row["lesson"], "sentence": row["sentence"]}
        )

    return {
        row["word"]: {
            "count":           row["count"],
            "first_studied":   row["first_studied"],
            "last_studied":    row["last_studied"],
            "next_review":     row["next_review"],
            "level":           row["level"],
            "cached_analysis": json.loads(row["cached_analysis"]) if row["cached_analysis"] else None,
            "contexts":        ctx_map.get(row["word"], []),
        }
        for row in words
    }


def normalize_vocab_target(
    target: str,
    *,
    target_type: str = "word",
    lemma: str = "",
) -> str:
    kind = str(target_type or "word").strip().lower()
    if kind not in {"word", "phrase"}:
        raise ValueError("target_type must be word or phrase")
    source = lemma if kind == "word" and str(lemma or "").strip() else target
    normalized = " ".join(str(source or "").replace("’", "'").lower().split())
    if kind == "word":
        normalized = re.sub(r"[^a-z'-]", "", normalized).strip("'-")
    if not normalized:
        raise ValueError("word required")
    return normalized


def cache_word_analysis(
    word: str,
    analysis: dict,
    *,
    target_type: str = "word",
    lemma: str = "",
) -> Optional[dict]:
    """Merge an AI analysis into an existing word without changing study count."""
    normalized = normalize_vocab_target(
        word,
        target_type=target_type,
        lemma=lemma,
    )
    with _db() as conn:
        row = conn.execute(
            "SELECT cached_analysis FROM words WHERE word=?",
            (normalized,),
        ).fetchone()
        if row is None:
            return None
        existing = json.loads(row["cached_analysis"]) if row["cached_analysis"] else {}
        merged = {**existing, **analysis}
        conn.execute(
            "UPDATE words SET cached_analysis=? WHERE word=?",
            (json.dumps(merged, ensure_ascii=False), normalized),
        )
    return merged


def get_cached_word_analysis(
    word: str,
    *,
    target_type: str = "word",
    lemma: str = "",
) -> Optional[dict]:
    """读取已缓存的 /analyze-word 深度解析（cache-before-reserve 用）。

    只认深度解析签名（en_definition/ielts_note），课程构建写入的 vocabulary
    列表形态不算命中，避免把浅层缓存当深度解析返回。"""
    normalized = normalize_vocab_target(word, target_type=target_type, lemma=lemma)
    with _db() as conn:
        row = conn.execute(
            "SELECT cached_analysis FROM words WHERE word=?",
            (normalized,),
        ).fetchone()
    if row is None or not row["cached_analysis"]:
        return None
    try:
        data = json.loads(row["cached_analysis"])
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not data:
        return None
    if "en_definition" not in data and "ielts_note" not in data:
        return None
    return data


def get_review_words(
    *,
    include_archived: bool = False,
    include_mastered: bool = False,
) -> dict:
    """Return explicitly admitted vocabulary targets with lifecycle metadata."""
    with _db() as conn:
        words = conn.execute(
            """
            SELECT w.*, review.source AS review_source,
                   review.added_at AS review_added_at,
                   review.target_type, review.lemma, review.display_text,
                   review.familiarity, review.archived, review.mastered, review.tags
            FROM word_review_items review
            JOIN words w ON w.word = review.word
            WHERE (? OR review.archived=0)
              AND (? OR review.mastered=0)
            ORDER BY
                CASE review.familiarity
                    WHEN 'unknown' THEN 0
                    WHEN 'fuzzy' THEN 1
                    WHEN 'unrated' THEN 2
                    ELSE 3
                END,
                review.updated_at ASC,
                w.word ASC
            """,
            (1 if include_archived else 0, 1 if include_mastered else 0),
        ).fetchall()
        contexts = conn.execute(
            """
            SELECT contexts.word, contexts.lesson, contexts.sentence
            FROM contexts
            JOIN word_review_items review ON review.word = contexts.word
            ORDER BY contexts.id
            """
        ).fetchall()
    ctx_map: dict[str, list] = {}
    for row in contexts:
        ctx_map.setdefault(row["word"], []).append(
            {"lesson": row["lesson"], "sentence": row["sentence"]}
        )
    ordered = {
        row["word"]: {
            "count": row["count"],
            "first_studied": row["first_studied"],
            "last_studied": row["last_studied"],
            "next_review": row["next_review"],
            "level": row["level"],
            "cached_analysis": json.loads(row["cached_analysis"]) if row["cached_analysis"] else None,
            "contexts": ctx_map.get(row["word"], []),
            "review_source": row["review_source"],
            "review_added_at": row["review_added_at"],
            "target_type": row["target_type"] or "word",
            "lemma": row["lemma"] or row["word"],
            "display_text": row["display_text"] or row["word"],
            "familiarity": row["familiarity"] or "unrated",
            "archived": bool(row["archived"]),
            "mastered": bool(row["mastered"]),
            "tags": json.loads(row["tags"] or "[]"),
        }
        for row in words
    }
    try:
        from webapp.services.dicts import ecdict_meta_map

        meta = ecdict_meta_map(list(ordered.keys()))
        if meta:
            for word, entry in ordered.items():
                info = meta.get(word.lower())
                if info:
                    entry["frq"] = info["frq"]
                    entry["exam_tags"] = info["exam_tags"]
            fam_rank = {"unknown": 0, "fuzzy": 1, "unrated": 2}
            ordered = dict(
                sorted(
                    ordered.items(),
                    key=lambda kv: (
                        fam_rank.get(kv[1].get("familiarity"), 3),
                        (meta.get(kv[0].lower()) or {}).get("frq") or 999999,
                    ),
                )
            )
    except Exception:
        pass
    return ordered


def set_word_review_tags(word: str, tags) -> list[str] | None:
    normalized = normalize_lesson_tags(tags)
    with _db() as conn:
        result = conn.execute(
            "UPDATE word_review_items SET tags=?, updated_at=? WHERE word=?",
            (json.dumps(normalized, ensure_ascii=False), _now_iso(), word.strip().lower()),
        )
    return normalized if result.rowcount else None


def activate_word_review(
    word: str,
    *,
    source: str,
    lesson_id: Optional[int] = None,
    analysis: Optional[dict] = None,
    target_type: str = "word",
    lemma: str = "",
    display_text: str = "",
) -> dict:
    kind = str(target_type or "word").strip().lower()
    normalized = normalize_vocab_target(word, target_type=kind, lemma=lemma)
    display = " ".join(str(display_text or word).strip().split()) or normalized
    today = date.today().isoformat()
    now = _now_iso()
    analysis_json = json.dumps(analysis) if analysis is not None else None
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO words
                (word, count, first_studied, last_studied, level, cached_analysis)
            VALUES (?, 1, ?, ?, '', ?)
            ON CONFLICT(word) DO UPDATE SET
                count = CASE WHEN words.count < 1 THEN 1 ELSE words.count END,
                first_studied = CASE WHEN words.first_studied = '' THEN excluded.first_studied ELSE words.first_studied END,
                last_studied = excluded.last_studied,
                cached_analysis = COALESCE(excluded.cached_analysis, words.cached_analysis)
            """,
            (normalized, today, today, analysis_json),
        )
        conn.execute(
            """
            INSERT INTO word_review_items
                (word, source, lesson_id, target_type, lemma, display_text,
                 familiarity, archived, mastered, added_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'unrated', 0, 0, ?, ?)
            ON CONFLICT(word) DO UPDATE SET
                source = excluded.source,
                lesson_id = COALESCE(excluded.lesson_id, word_review_items.lesson_id),
                target_type = excluded.target_type,
                lemma = excluded.lemma,
                display_text = excluded.display_text,
                mastered = 0,
                updated_at = excluded.updated_at
            """,
            (normalized, source, lesson_id, kind, normalized, display, now, now),
        )
        conn.execute("DELETE FROM known_words WHERE word=?", (normalized,))
        row = conn.execute(
            """
            SELECT w.word, w.count, w.next_review, review.source,
                   review.lesson_id, review.added_at, review.target_type,
                   review.lemma, review.display_text, review.familiarity,
                   review.archived, review.mastered
            FROM word_review_items review
            JOIN words w ON w.word = review.word
            WHERE review.word=?
            """,
            (normalized,),
        ).fetchone()
    item = dict(row)
    item["archived"] = bool(item["archived"])
    item["mastered"] = bool(item["mastered"])
    return item


def is_word_in_review(
    word: str,
    *,
    target_type: str = "word",
    lemma: str = "",
) -> bool:
    try:
        normalized = normalize_vocab_target(word, target_type=target_type, lemma=lemma)
    except ValueError:
        return False
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM word_review_items WHERE word=? AND mastered=0",
            (normalized,),
        ).fetchone()
    return row is not None


def get_review_word_set() -> set[str]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT word FROM word_review_items WHERE mastered=0"
        ).fetchall()
    return {row["word"] for row in rows}


def get_mastered_review_targets() -> set[str]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT word FROM word_review_items WHERE mastered=1"
        ).fetchall()
    return {row["word"] for row in rows}


def get_review_word_item(word: str) -> dict | None:
    normalized = " ".join(str(word or "").replace("’", "'").lower().split())
    with _db() as conn:
        row = conn.execute(
            """
            SELECT review.*, w.count, w.next_review, w.cached_analysis
            FROM word_review_items AS review
            JOIN words AS w ON w.word=review.word
            WHERE review.word=?
            """,
            (normalized,),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["archived"] = bool(item["archived"])
    item["mastered"] = bool(item["mastered"])
    item["cached_analysis"] = (
        json.loads(item["cached_analysis"]) if item["cached_analysis"] else None
    )
    return item


def set_review_word_familiarity(word: str, familiarity: str) -> dict | None:
    value = str(familiarity or "").strip().lower()
    if value not in {"unknown", "fuzzy", "known"}:
        raise ValueError("Invalid familiarity")
    normalized = " ".join(str(word or "").replace("’", "'").lower().split())
    with _db() as conn:
        result = conn.execute(
            """
            UPDATE word_review_items
            SET familiarity=?, updated_at=?
            WHERE word=? AND mastered=0
            """,
            (value, _now_iso(), normalized),
        )
    return get_review_word_item(normalized) if result.rowcount else None


def set_review_word_lifecycle(
    word: str,
    *,
    archived: Optional[bool] = None,
    mastered: Optional[bool] = None,
) -> dict | None:
    normalized = " ".join(str(word or "").replace("’", "'").lower().split())
    current = get_review_word_item(normalized)
    if not current and mastered:
        # 课程页直接标记已掌握时词可能还没进复习本，先激活再置为已掌握
        activate_word_review(normalized, source="mastered")
        current = get_review_word_item(normalized)
    if not current:
        return None
    new_archived = bool(current["archived"]) if archived is None else bool(archived)
    new_mastered = bool(current["mastered"]) if mastered is None else bool(mastered)
    if new_mastered:
        new_archived = False
    with _db() as conn:
        conn.execute(
            """
            UPDATE word_review_items
            SET archived=?, mastered=?, updated_at=?
            WHERE word=?
            """,
            (1 if new_archived else 0, 1 if new_mastered else 0, _now_iso(), normalized),
        )
        if new_mastered:
            conn.execute(
                "INSERT OR REPLACE INTO known_words (word, added_at) VALUES (?, ?)",
                (normalized, date.today().isoformat()),
            )
        else:
            conn.execute("DELETE FROM known_words WHERE word=?", (normalized,))
    return get_review_word_item(normalized)


def upsert_word(word: str, today: str, level: str = "",
                analysis: Optional[dict] = None) -> tuple[int, bool]:
    """Increment study count. Returns (new_count, is_new)."""
    analysis_json = json.dumps(analysis) if analysis is not None else None
    with _db() as conn:
        try:
            conn.execute(
                "INSERT INTO words (word, count, first_studied, last_studied, level, cached_analysis)"
                " VALUES (?, 1, ?, ?, ?, ?)",
                (word, today, today, level, analysis_json)
            )
            return 1, True
        except sqlite3.IntegrityError:
            pass

        conn.execute(
            """
            UPDATE words
            SET count = count + 1,
                last_studied = ?,
                level = CASE WHEN ? != '' THEN ? ELSE level END,
                cached_analysis = COALESCE(?, cached_analysis)
            WHERE word = ?
            """,
            (today, level, level, analysis_json, word)
        )
        row = conn.execute("SELECT count FROM words WHERE word = ?", (word,)).fetchone()
        return row["count"], False


def review_word(word: str, today: str) -> Optional[int]:
    """Mark an existing word as reviewed today. Returns new count, or None when absent."""
    word = word.strip().lower()
    if not word:
        return None
    with _db() as conn:
        row = conn.execute("SELECT count, last_studied FROM words WHERE word = ?", (word,)).fetchone()
        if not row:
            return None
        if row["last_studied"] == today:
            return row["count"]
        new_count = int(row["count"] or 0) + 1
        conn.execute(
            "UPDATE words SET count = ?, last_studied = ? WHERE word = ?",
            (new_count, today, word),
        )
        return new_count


def sentence_contains_word(sentence: str, word: str) -> bool:
    """词边界匹配：sentence 中是否出现 word（容忍变形：conjugated/studies 视为命中词族）。

    前缀规则：词长 >=4 时取 max(4, len-2) 前缀做词干匹配，覆盖常见屈折变化
    （conjugated/studies）；前缀过短会误配（viral→virgin），故下限 4 字符。
    不规则变形（go/went）不命中——此时守卫会尝试换句而不是误删正确语境的风险
    由调用方权衡（见 find_v2_sentence_containing 的失败兜底）。
    """
    text = (sentence or "").lower()
    w = (word or "").strip().lower()
    if not text or not w:
        return False
    if " " in w:
        return w in text
    tokens = re.findall(r"[a-z]+(?:'[a-z]+)?", text)
    if w in tokens:
        return True
    if len(w) < 5:
        return False
    prefix = w[: max(4, len(w) - 2)]
    return any(t.startswith(prefix) for t in tokens)


def find_v2_sentence_containing(word: str) -> str | None:
    """在 v2_sentences 中找一句包含该词（词族）的例句，候选按长度升序避免取到 merged 长句。"""
    w = (word or "").strip().lower()
    if not w:
        return None
    needle = w if " " in w or len(w) < 5 else w[: max(4, len(w) - 2)]
    with _db() as conn:
        rows = conn.execute(
            "SELECT text FROM v2_sentences WHERE lower(text) LIKE ? ORDER BY length(text) LIMIT 50",
            (f"%{needle}%",),
        ).fetchall()
    for row in rows:
        if sentence_contains_word(row["text"], w):
            return row["text"]
    return None


def add_context(word: str, lesson: str, sentence: str):
    """Add context, deduplicate; keep last 5 per word."""
    with _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO contexts (word, lesson, sentence) VALUES (?, ?, ?)",
            (word, lesson, sentence)
        )
        conn.execute("""
            DELETE FROM contexts
            WHERE word = ? AND id NOT IN (
                SELECT id FROM contexts WHERE word = ? ORDER BY id DESC LIMIT 5
            )
        """, (word, word))


def get_story(cache_key: str) -> Optional[str]:
    with _db() as conn:
        row = conn.execute(
            "SELECT story FROM stories WHERE cache_key = ?", (cache_key,)
        ).fetchone()
    return row["story"] if row else None


def add_known_word(word: str, today: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO known_words (word, added_at) VALUES (?, ?)",
            (word.lower(), today)
        )


def remove_known_word(word: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM known_words WHERE word = ?", (word.lower(),))


def set_next_review(word: str, date_str: str) -> bool:
    """Set explicit next review date (YYYY-MM-DD). Pass '' to clear override."""
    with _db() as conn:
        result = conn.execute(
            "UPDATE words SET next_review = ? WHERE word = ?", (date_str, word.lower())
        )
    return result.rowcount > 0


def delete_word(word: str) -> bool:
    w = word.lower()
    with _db() as conn:
        result = conn.execute("DELETE FROM words WHERE word = ?", (w,))
        conn.execute("DELETE FROM contexts WHERE word = ?", (w,))
    return result.rowcount > 0


def save_v2_lesson_word(lesson_id: int, word: str, sentence: str = "") -> dict:
    normalized = word.strip().lower()
    if not normalized:
        raise ValueError("word required")
    now = _now_iso()
    with _db() as conn:
        conn.execute(
            "DELETE FROM v2_lesson_hidden_words WHERE lesson_id=? AND word=?",
            (lesson_id, normalized),
        )
        conn.execute(
            "INSERT INTO v2_lesson_words (lesson_id, word, sentence, created_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(lesson_id, word) DO UPDATE SET"
            " sentence = CASE WHEN excluded.sentence != '' THEN excluded.sentence ELSE v2_lesson_words.sentence END",
            (lesson_id, normalized, sentence, now),
        )
        row = conn.execute(
            "SELECT * FROM v2_lesson_words WHERE lesson_id=? AND word=?",
            (lesson_id, normalized),
        ).fetchone()
    return dict(row)


def get_v2_lesson_word(lesson_id: int, word: str) -> Optional[dict]:
    normalized = word.strip().lower()
    if not normalized:
        return None
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM v2_lesson_words WHERE lesson_id=? AND word=?",
            (lesson_id, normalized),
        ).fetchone()
    return dict(row) if row else None


def get_v2_lesson_words(lesson_id: int) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT lw.word, lw.sentence, lw.created_at, w.cached_analysis, w.count, w.level
            FROM v2_lesson_words lw
            JOIN words w ON w.word = lw.word
            WHERE lw.lesson_id=?
            ORDER BY lw.created_at ASC, lw.id ASC
            """,
            (lesson_id,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["cached_analysis"] = json.loads(item["cached_analysis"]) if item["cached_analysis"] else None
        result.append(item)
    return result


def delete_v2_lesson_word(lesson_id: int, word: str) -> bool:
    normalized = word.strip().lower()
    if not normalized:
        return False
    with _db() as conn:
        result = conn.execute(
            "DELETE FROM v2_lesson_words WHERE lesson_id=? AND word=?",
            (lesson_id, normalized),
        )
    return result.rowcount > 0


def hide_v2_lesson_word(lesson_id: int, word: str) -> None:
    normalized = word.strip().lower()
    if not normalized:
        return
    with _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO v2_lesson_hidden_words (lesson_id, word, created_at) VALUES (?, ?, ?)",
            (lesson_id, normalized, _now_iso()),
        )


def get_v2_lesson_hidden_words(lesson_id: int) -> set[str]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT word FROM v2_lesson_hidden_words WHERE lesson_id=?",
            (lesson_id,),
        ).fetchall()
    return {row["word"] for row in rows}


def get_known_words() -> set:
    with _db() as conn:
        rows = conn.execute("SELECT word FROM known_words").fetchall()
    return {row["word"] for row in rows}


def save_story(cache_key: str, words: list, story: str, date: str):
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO stories (cache_key, words_json, story, date) VALUES (?, ?, ?, ?)",
            (cache_key, json.dumps(words), story, date)
        )
        # Keep newest 60 stories
        conn.execute("""
            DELETE FROM stories WHERE cache_key NOT IN (
                SELECT cache_key FROM stories ORDER BY date DESC, cache_key DESC LIMIT 60
            )
        """)


def save_story_history(
    cache_key: str,
    words: list,
    story: str,
    date: str,
    *,
    learner_level: str = "",
    theme: str = "",
) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO story_history
                (cache_key, words_json, story, date, learner_level, theme, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cache_key,
                json.dumps(words, ensure_ascii=False),
                story,
                date,
                learner_level,
                theme,
                _now_iso(),
            ),
        )
        conn.execute(
            """
            DELETE FROM story_history WHERE id NOT IN (
                SELECT id FROM story_history ORDER BY created_at DESC, id DESC LIMIT 100
            )
            """
        )


def list_story_history(limit: int = 30, offset: int = 0) -> list[dict]:
    size = max(1, min(int(limit or 30), 100))
    start = max(0, int(offset or 0))
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT id, cache_key, words_json, story, date,
                   learner_level, theme, created_at
            FROM story_history
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (size, start),
        ).fetchall()
    return [dict(row) for row in rows]


def count_story_history() -> int:
    with _db() as conn:
        row = conn.execute("SELECT COUNT(*) AS total FROM story_history").fetchone()
    return int(row["total"] if row else 0)


def delete_story_history(story_id: int) -> bool:
    with _db() as conn:
        row = conn.execute(
            "SELECT cache_key, story FROM story_history WHERE id=?",
            (story_id,),
        ).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM story_history WHERE id=?", (story_id,))
        # The latest story cache is also the source for legacy backfill. Remove the
        # matching cache row so an intentionally deleted story cannot reappear.
        conn.execute(
            "DELETE FROM stories WHERE cache_key=? AND story=?",
            (row["cache_key"], row["story"]),
        )
    return True


def upsert_lesson(filename: str, title: str, source_type: str, source_url: str,
                  sentence_count: int, duration: int, created_at: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO lessons "
            "(filename,title,source_type,source_url,sentence_count,duration,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (filename, title, source_type, source_url, sentence_count, duration, created_at),
        )


def delete_lesson_meta(filename: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM lessons WHERE filename=?", (filename,))


# ── Lesson 归档 / 重命名 ──────────────────────────────────────

def rename_lesson(filename: str, new_title: str) -> None:
    with _db() as conn:
        conn.execute("UPDATE lessons SET title=? WHERE filename=?", (new_title, filename))


def set_lesson_archived(filename: str, archived: bool) -> None:
    with _db() as conn:
        conn.execute("UPDATE lessons SET archived=? WHERE filename=?",
                     (1 if archived else 0, filename))


def get_lessons(include_archived: bool = False) -> list:
    with _db() as conn:
        if include_archived:
            rows = conn.execute(
                "SELECT filename,title,source_type,source_url,sentence_count,duration,created_at,archived"
                " FROM lessons ORDER BY archived ASC, created_at DESC, rowid DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT filename,title,source_type,source_url,sentence_count,duration,created_at,archived"
                " FROM lessons WHERE archived=0 ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
    return [dict(r) for r in rows]


# ── 课后总结 ──────────────────────────────────────────────────

def save_reflection(filename: str, reflection: str, today: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO lesson_reflections (filename, reflection, created_at) VALUES (?,?,?)",
            (filename, reflection, today)
        )


def get_latest_reflection(filename: str) -> Optional[str]:
    with _db() as conn:
        row = conn.execute(
            "SELECT reflection FROM lesson_reflections WHERE filename=?"
            " ORDER BY id DESC LIMIT 1",
            (filename,)
        ).fetchone()
    return row["reflection"] if row else None


# ── 练习记录 ──────────────────────────────────────────────────

def save_practice_attempt(filename: str, sentence_idx: int,
                           user_input: str, ai_feedback: str, today: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO practice_attempts"
            " (filename, sentence_idx, user_input, ai_feedback, created_at)"
            " VALUES (?,?,?,?,?)",
            (filename, sentence_idx, user_input, ai_feedback, today)
        )


def get_practice_attempts(filename: str, sentence_idx: int) -> list:
    with _db() as conn:
        rows = conn.execute(
            "SELECT user_input, ai_feedback, created_at FROM practice_attempts"
            " WHERE filename=? AND sentence_idx=? ORDER BY id DESC LIMIT 10",
            (filename, sentence_idx)
        ).fetchall()
    return [dict(r) for r in rows]


def save_v2_practice_attempt(
    *,
    practice_type: str,
    target: str,
    target_type: str = "",
    user_input: str,
    verdict: str,
    result: dict,
    sentence_id: Optional[int] = None,
    lesson_id: Optional[int] = None,
    input_method: str = "keyboard",
    hint_used: bool = False,
    scenario_cn: str = "",
    hint_text: str = "",
    source_context: str = "",
) -> dict:
    clean_input = str(user_input or "").strip()
    if not clean_input:
        raise ValueError("user input required")
    clean_type = str(practice_type or "word").strip().lower()
    if clean_type not in {"word", "phrase", "pattern"}:
        raise ValueError("Invalid practice type")
    clean_target_type = str(target_type or clean_type).strip().lower()
    if clean_target_type not in {"word", "phrase", "pattern"}:
        raise ValueError("Invalid target type")
    clean_verdict = str(verdict or "").strip().lower()
    if clean_verdict not in {"accepted", "needs_revision"}:
        raise ValueError("Invalid practice verdict")
    method = str(input_method or "keyboard").strip().lower()
    if method not in {"keyboard", "voice"}:
        raise ValueError("Invalid input method")
    with _db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO v2_practice_attempts
                (practice_type, target, target_type, sentence_id, lesson_id, user_input,
                 input_method, hint_used, scenario_cn, hint_text, source_context, verdict,
                 key_issue, revised_sentence, idiomatic_suggestion, result_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                clean_type,
                str(target or "").strip(),
                clean_target_type,
                sentence_id,
                lesson_id,
                clean_input,
                method,
                1 if hint_used else 0,
                str(scenario_cn or "").strip(),
                str(hint_text or "").strip(),
                str(source_context or "").strip(),
                clean_verdict,
                str(result.get("key_issue") or "").strip(),
                str(result.get("revised_sentence") or "").strip(),
                str(result.get("idiomatic_suggestion") or "").strip(),
                json.dumps(result, ensure_ascii=False),
                _now_iso(),
            ),
        )
        row = conn.execute(
            "SELECT * FROM v2_practice_attempts WHERE id=?",
            (cursor.lastrowid,),
        ).fetchone()
    return _serialize_v2_practice_attempt(row)


def _serialize_v2_practice_attempt(row) -> dict:
    item = dict(row)
    item["hint_used"] = bool(item["hint_used"])
    feedback = json.loads(item.pop("result_json"))
    item["feedback"] = feedback
    item["result"] = feedback
    item["user_answer"] = item["user_input"]
    item["status"] = item["verdict"]
    item["status_label"] = feedback.get("status_label", "")
    item["target_used_correctly"] = bool(feedback.get("target_used_correctly"))
    item["error_summary"] = item.get("key_issue") or str(feedback.get("key_issue") or "")
    item["explanation"] = str(feedback.get("explanation") or "")
    item["naturalness_analysis"] = str(feedback.get("naturalness_analysis") or "")
    raw_improvement_points = feedback.get("improvement_points")
    item["improvement_points"] = (
        [str(point) for point in raw_improvement_points if str(point).strip()]
        if isinstance(raw_improvement_points, list)
        else []
    )
    item["revised_sentence"] = (
        item.get("revised_sentence") or str(feedback.get("revised_sentence") or "")
    )
    item["idiomatic_suggestion"] = (
        item.get("idiomatic_suggestion")
        or str(feedback.get("idiomatic_suggestion") or "")
    )
    item["counts_as_completion"] = True
    return item


def list_v2_practice_attempt_history(
    *,
    target: str = "",
    sentence_id: Optional[int] = None,
    practice_type: str = "",
    lesson_id: Optional[int] = None,
    source_context: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    clauses = []
    params: list = []
    clean_target = str(target or "").strip()
    clean_type = str(practice_type or "").strip().lower()
    clean_context = str(source_context or "").strip()
    if clean_target:
        clauses.append("LOWER(target)=LOWER(?)")
        params.append(clean_target)
    if sentence_id is not None:
        clauses.append("sentence_id=?")
        params.append(sentence_id)
    if clean_type:
        clauses.append("practice_type=?")
        params.append(clean_type)
    if lesson_id is not None:
        clauses.append("lesson_id=?")
        params.append(lesson_id)
    if clean_context:
        clauses.append("source_context=?")
        params.append(clean_context)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    offset = (page - 1) * page_size
    with _db() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM v2_practice_attempts{where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM v2_practice_attempts{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        ).fetchall()
    return {
        "items": [_serialize_v2_practice_attempt(row) for row in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size,
        "filters": {
            "target": clean_target,
            "sentence_id": sentence_id,
            "practice_type": clean_type,
            "lesson_id": lesson_id,
            "source_context": clean_context,
        },
    }


def list_v2_practice_attempts() -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM v2_practice_attempts ORDER BY id ASC"
        ).fetchall()
    result = []
    for row in rows:
        result.append(_serialize_v2_practice_attempt(row))
    return result


# ── Study Sessions ─────────────────────────────────────────

def upsert_study_session(lesson_filename: str, current_idx: int,
                          total: int, now: str) -> None:
    with _db() as conn:
        conn.execute("""
            INSERT INTO study_sessions
                (lesson_filename, started_at, last_active_at, current_sentence_idx, total_sentences)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(lesson_filename) DO UPDATE SET
                last_active_at       = excluded.last_active_at,
                current_sentence_idx = excluded.current_sentence_idx,
                total_sentences      = excluded.total_sentences
        """, (lesson_filename, now, now, current_idx, total))


def get_study_session(lesson_filename: str) -> Optional[dict]:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM study_sessions WHERE lesson_filename=?",
            (lesson_filename,)
        ).fetchone()
    return dict(row) if row else None


def get_recent_study_sessions(limit: int = 5) -> list[dict]:
    """Return recently active lesson sessions, newest first."""
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM study_sessions
            ORDER BY last_active_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Sentence Marks ─────────────────────────────────────────

def toggle_sentence_mark(lesson_filename: str, sentence_idx: int, now: str) -> bool:
    """Toggle mark; returns True if now marked, False if unmarked."""
    with _db() as conn:
        existing = conn.execute(
            "SELECT id FROM sentence_marks WHERE lesson_filename=? AND sentence_idx=?",
            (lesson_filename, sentence_idx)
        ).fetchone()
        if existing:
            conn.execute(
                "DELETE FROM sentence_marks WHERE lesson_filename=? AND sentence_idx=?",
                (lesson_filename, sentence_idx)
            )
            return False
        conn.execute(
            "INSERT INTO sentence_marks (lesson_filename, sentence_idx, marked_at) VALUES (?,?,?)",
            (lesson_filename, sentence_idx, now)
        )
        return True


def set_sentence_mark(lesson_filename: str, sentence_idx: int, marked: bool, now: str) -> bool:
    """Set mark state explicitly; returns the resulting state."""
    with _db() as conn:
        if marked:
            conn.execute(
                "INSERT OR IGNORE INTO sentence_marks (lesson_filename, sentence_idx, marked_at) VALUES (?,?,?)",
                (lesson_filename, sentence_idx, now)
            )
            return True
        conn.execute(
            "DELETE FROM sentence_marks WHERE lesson_filename=? AND sentence_idx=?",
            (lesson_filename, sentence_idx)
        )
        return False


def get_sentence_marks(lesson_filename: str) -> list[int]:
    """Return list of marked sentence indices for a lesson."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT sentence_idx FROM sentence_marks WHERE lesson_filename=? ORDER BY sentence_idx",
            (lesson_filename,)
        ).fetchall()
    return [r["sentence_idx"] for r in rows]


# ── v2 Video Workspace ────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_sentence_text(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _validate_tag_category(category: str) -> str:
    value = (category or "").strip().lower()
    if value not in V2_TAG_CATEGORIES:
        raise ValueError(f"Unsupported tag category: {category}")
    return value


def upsert_v2_sentence(text: str, *, translation: str = "", phonetics: str = "") -> dict:
    normalized = _normalize_sentence_text(text)
    if not normalized:
        raise ValueError("sentence text required")
    now = _now_iso()
    with _db() as conn:
        conn.execute(
            "INSERT INTO v2_sentences"
            " (normalized_text, text, translation, phonetics, first_seen_at, last_seen_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(normalized_text) DO UPDATE SET"
            " text = excluded.text,"
            " translation = CASE WHEN excluded.translation != '' THEN excluded.translation ELSE v2_sentences.translation END,"
            " phonetics = CASE WHEN excluded.phonetics != '' THEN excluded.phonetics ELSE v2_sentences.phonetics END,"
            " last_seen_at = excluded.last_seen_at",
            (normalized, text.strip(), translation, phonetics, now, now),
        )
        row = conn.execute(
            "SELECT * FROM v2_sentences WHERE normalized_text=?",
            (normalized,),
        ).fetchone()
    return dict(row)


def save_v2_manual_sentence(text: str, translation: str = "") -> dict:
    """收藏无课程来源的句子（如 AI 生成句），直接进句子库。"""
    sentence = upsert_v2_sentence(text, translation=translation)
    with _db() as conn:
        conn.execute(
            "UPDATE v2_sentences SET saved_manually=1, archived=0 WHERE id=?",
            (sentence["id"],),
        )
    saved = get_v2_sentence_by_id(sentence["id"])
    return saved or sentence


def get_v2_sentence(text: str) -> dict | None:
    normalized = _normalize_sentence_text(text)
    if not normalized:
        return None
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM v2_sentences WHERE normalized_text=?",
            (normalized,),
        ).fetchone()
    return dict(row) if row else None


def get_v2_sentence_by_id(sentence_id: int) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM v2_sentences WHERE id=?",
            (sentence_id,),
        ).fetchone()
    return dict(row) if row else None


def set_v2_sentence_phonetics(sentence_id: int, phonetics: str, source: str = "rule") -> bool:
    with _db() as conn:
        result = conn.execute(
            "UPDATE v2_sentences SET phonetics=?, phonetics_source=? WHERE id=?",
            (phonetics, source, sentence_id),
        )
    return result.rowcount > 0


def clear_v2_lesson_subtitle_error(lesson_id: int) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE v2_lessons SET subtitle_error='' WHERE id=?",
            (lesson_id,),
        )


def _recover_saved_sentence_timing(conn, lesson_id: int, text: str) -> tuple[float, float] | None:
    target = _normalize_sentence_text(text)
    if not target:
        return None
    segments = conn.execute(
        "SELECT start, end, text FROM v2_subtitle_segments WHERE lesson_id=? ORDER BY idx",
        (lesson_id,),
    ).fetchall()
    for start_index in range(len(segments)):
        parts: list[str] = []
        for end_index in range(start_index, min(len(segments), start_index + 40)):
            parts.append(str(segments[end_index]["text"] or ""))
            candidate = _normalize_sentence_text(" ".join(parts))
            if candidate == target:
                return (
                    float(segments[start_index]["start"] or 0),
                    float(segments[end_index]["end"] or 0),
                )
            if len(candidate) > len(target) + 10 or not target.startswith(candidate):
                break
    return None


def list_v2_saved_sentences(today: str = "", *, include_archived: bool = False) -> list[dict]:
    review_day = today or date.today().isoformat()
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT sentence.*
            FROM v2_sentences AS sentence
            WHERE (
                EXISTS (
                    SELECT 1 FROM v2_phase_b_sentences AS saved
                    WHERE saved.sentence_id=sentence.id
                )
                OR sentence.saved_manually=1
            )
              AND (? OR sentence.archived=0)
            ORDER BY
                sentence.archived ASC,
                CASE sentence.listening_result
                    WHEN 'untested' THEN 0
                    WHEN 'not_understood' THEN 1
                    ELSE 2
                END,
                CASE WHEN sentence.last_reviewed_at='' THEN 0 ELSE 1 END,
                sentence.last_reviewed_at ASC,
                sentence.id ASC
            """,
            (1 if include_archived else 0,),
        ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            lesson_rows = conn.execute(
                """
                SELECT lesson.id, lesson.title, lesson.source_type,
                       lesson.video_id, lesson.media_url,
                       saved.segment_index, saved.start_seconds, saved.end_seconds
                FROM v2_phase_b_sentences AS saved
                JOIN v2_lessons AS lesson ON lesson.id=saved.lesson_id
                WHERE saved.sentence_id=?
                ORDER BY lesson.id DESC
                """,
                (item["id"],),
            ).fetchall()
            lesson_links = []
            for lesson in lesson_rows:
                start_seconds = float(lesson["start_seconds"] or 0)
                end_seconds = float(lesson["end_seconds"] or 0)
                if end_seconds <= start_seconds:
                    recovered = _recover_saved_sentence_timing(
                        conn, int(lesson["id"]), str(item.get("text") or "")
                    )
                    if recovered:
                        start_seconds, end_seconds = recovered
                lesson_links.append({
                    "lesson_id": int(lesson["id"]),
                    "title": str(lesson["title"] or f"Course {lesson['id']}"),
                    "source_type": str(lesson["source_type"] or ""),
                    "video_id": str(lesson["video_id"] or ""),
                    "segment_index": int(lesson["segment_index"]),
                    "start_seconds": start_seconds,
                    "end_seconds": end_seconds,
                    "media_url": str(lesson["media_url"] or ""),
                })
            item["lesson_links"] = lesson_links
            item["lesson_ids"] = [link["lesson_id"] for link in lesson_links]
            item["lesson_titles"] = [link["title"] for link in lesson_links]
            audio_link = next(
                (
                    link for link in lesson_links
                    if link["media_url"] and link["end_seconds"] > link["start_seconds"]
                ),
                None,
            )
            item["audio"] = (
                {
                    "url": audio_link["media_url"],
                    "start": audio_link["start_seconds"],
                    "end": audio_link["end_seconds"],
                }
                if audio_link
                else None
            )
            if item["audio"] is None:
                youtube_link = next(
                    (
                        link for link in lesson_links
                        if link["source_type"] == "youtube"
                        and link["video_id"]
                        and link["end_seconds"] > link["start_seconds"]
                    ),
                    None,
                )
                if youtube_link:
                    item["audio"] = {
                        "kind": "youtube",
                        "video_id": youtube_link["video_id"],
                        "start": youtube_link["start_seconds"],
                        "end": youtube_link["end_seconds"],
                    }
            item["tags"] = get_v2_sentence_tags(int(item["id"]))
            item["pattern"] = get_v2_sentence_pattern(int(item["id"]))
            item["has_pattern"] = bool(
                item["pattern"] and item["pattern"].get("pattern_template")
            )
            item["is_due"] = not item.get("next_review") or item["next_review"] <= review_day
            item["archived"] = bool(item.get("archived"))
            item["listening_result"] = str(item.get("listening_result") or "untested")
            results.append(item)
    return results


def review_v2_sentence(sentence_id: int, rating: str, today: str = "") -> dict | None:
    rating_key = str(rating or "").strip().lower()
    result_map = {
        "again": "not_understood",
        "hard": "not_understood",
        "good": "understood",
        "understood": "understood",
        "not_understood": "not_understood",
    }
    if rating_key not in result_map:
        raise ValueError("Invalid sentence review rating")
    listening_result = result_map[rating_key]
    review_day = date.fromisoformat(today) if today else date.today()
    with _db() as conn:
        row = conn.execute(
            """
            SELECT sentence.*
            FROM v2_sentences AS sentence
            WHERE sentence.id=?
              AND EXISTS (
                  SELECT 1 FROM v2_phase_b_sentences AS saved
                  WHERE saved.sentence_id=sentence.id
              )
            """,
            (sentence_id,),
        ).fetchone()
        if not row:
            return None
        new_count = int(row["review_count"] or 0) + 1
        if listening_result == "not_understood":
            interval_days = 1
        else:
            intervals = (3, 7, 14, 30, 60, 120)
            interval_days = intervals[min(new_count - 1, len(intervals) - 1)]
        next_review = (review_day + timedelta(days=interval_days)).isoformat()
        reviewed_at = _now_iso()
        conn.execute(
            """
            UPDATE v2_sentences
            SET review_count=?, listening_result=?, last_reviewed_at=?, next_review=?
            WHERE id=?
            """,
            (new_count, listening_result, reviewed_at, next_review, sentence_id),
        )
        conn.execute(
            """
            INSERT INTO v2_sentence_listening_attempts
                (sentence_id, result, created_at)
            VALUES (?, ?, ?)
            """,
            (sentence_id, listening_result, reviewed_at),
        )
        updated = conn.execute(
            "SELECT * FROM v2_sentences WHERE id=?",
            (sentence_id,),
        ).fetchone()
    item = dict(updated)
    item["archived"] = bool(item.get("archived"))
    return item


def set_v2_sentence_archived(sentence_id: int, archived: bool) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            """
            SELECT sentence.id
            FROM v2_sentences AS sentence
            WHERE sentence.id=?
              AND EXISTS (
                  SELECT 1 FROM v2_phase_b_sentences AS saved
                  WHERE saved.sentence_id=sentence.id
              )
            """,
            (sentence_id,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE v2_sentences SET archived=? WHERE id=?",
            (1 if archived else 0, sentence_id),
        )
        updated = conn.execute(
            "SELECT * FROM v2_sentences WHERE id=?",
            (sentence_id,),
        ).fetchone()
    item = dict(updated)
    item["archived"] = bool(item.get("archived"))
    return item


def get_v2_sentence_pattern(sentence_id: int) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM v2_sentence_patterns WHERE sentence_id=?",
            (sentence_id,),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        analysis = json.loads(item.get("analysis_json") or "{}")
    except (TypeError, ValueError):
        analysis = {}
    item["analysis"] = analysis if isinstance(analysis, dict) else {}
    return item


def save_v2_sentence_pattern(sentence_id: int, pattern_template: str) -> dict:
    clean_pattern = " ".join(str(pattern_template or "").split())
    if not clean_pattern:
        raise ValueError("pattern_template required")
    if not get_v2_sentence_by_id(sentence_id):
        raise ValueError("Sentence not found")
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO v2_sentence_patterns
                (sentence_id, pattern_template, scenario_cn, updated_at)
            VALUES (?, ?, '', ?)
            ON CONFLICT(sentence_id) DO UPDATE SET
                pattern_template=excluded.pattern_template,
                scenario_cn='',
                updated_at=excluded.updated_at
            """,
            (sentence_id, clean_pattern, _now_iso()),
        )
    return get_v2_sentence_pattern(sentence_id)


def save_v2_sentence_pattern_scenario(sentence_id: int, scenario_cn: str) -> dict:
    clean_scenario = " ".join(str(scenario_cn or "").split())
    if not clean_scenario:
        raise ValueError("scenario_cn required")
    current = get_v2_sentence_pattern(sentence_id)
    if not current or not current.get("pattern_template"):
        raise ValueError("Sentence pattern not found")
    with _db() as conn:
        conn.execute(
            """
            UPDATE v2_sentence_patterns
            SET scenario_cn=?, updated_at=?
            WHERE sentence_id=?
            """,
            (clean_scenario, _now_iso(), sentence_id),
        )
    return get_v2_sentence_pattern(sentence_id)


def save_v2_sentence_analysis(sentence_id: int, analysis: dict) -> dict:
    if not get_v2_sentence_by_id(sentence_id):
        raise ValueError("Sentence not found")
    if not isinstance(analysis, dict) or not analysis:
        raise ValueError("analysis required")
    template = " ".join(str(analysis.get("template") or "").split())
    payload = json.dumps(analysis, ensure_ascii=False)
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO v2_sentence_patterns
                (sentence_id, pattern_template, scenario_cn, analysis_json, updated_at)
            VALUES (?, ?, '', ?, ?)
            ON CONFLICT(sentence_id) DO UPDATE SET
                scenario_cn=CASE
                    WHEN excluded.pattern_template<>''
                     AND excluded.pattern_template<>v2_sentence_patterns.pattern_template THEN ''
                    ELSE v2_sentence_patterns.scenario_cn
                END,
                pattern_template=CASE
                    WHEN excluded.pattern_template<>'' THEN excluded.pattern_template
                    ELSE v2_sentence_patterns.pattern_template
                END,
                analysis_json=excluded.analysis_json,
                updated_at=excluded.updated_at
            """,
            (sentence_id, template, payload, _now_iso()),
        )
    return get_v2_sentence_pattern(sentence_id)


def list_v2_tags() -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM v2_tags ORDER BY category, source DESC, name COLLATE NOCASE"
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_v2_tag(category: str, name: str, source: str = "user") -> dict:
    clean_category = _validate_tag_category(category)
    clean_name = " ".join((name or "").strip().split())
    if not clean_name:
        raise ValueError("tag name required")
    now = _now_iso()
    clean_source = "system" if source == "system" else "user"
    with _db() as conn:
        conn.execute(
            "INSERT INTO v2_tags (category, name, source, created_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(category, name) DO UPDATE SET name = excluded.name",
            (clean_category, clean_name, clean_source, now),
        )
        row = conn.execute(
            "SELECT * FROM v2_tags WHERE category=? AND name=?",
            (clean_category, clean_name),
        ).fetchone()
    return dict(row)


def get_v2_sentence_tags(sentence_id: int) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT t.*
            FROM v2_sentence_tags st
            JOIN v2_tags t ON t.id = st.tag_id
            WHERE st.sentence_id=?
            ORDER BY t.category, t.name COLLATE NOCASE
            """,
            (sentence_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def replace_v2_sentence_tags(sentence_id: int, tags: list[dict]) -> list[dict]:
    now = _now_iso()
    tag_ids = []
    for item in tags:
        tag = upsert_v2_tag(str(item.get("category") or ""), str(item.get("name") or ""))
        tag_ids.append(int(tag["id"]))
    with _db() as conn:
        conn.execute("DELETE FROM v2_sentence_tags WHERE sentence_id=?", (sentence_id,))
        conn.executemany(
            "INSERT OR IGNORE INTO v2_sentence_tags (sentence_id, tag_id, created_at) VALUES (?, ?, ?)",
            [(sentence_id, tag_id, now) for tag_id in tag_ids],
        )
    return get_v2_sentence_tags(sentence_id)


def create_v2_lesson(source_type: str, source_url: str, video_id: str = "",
                     title: str = "", duration: float = 0,
                     media_url: str = "", media_kind: str = "",
                     lesson_mode: str = "listening") -> dict:
    now = _now_iso()
    with _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO v2_lessons"
            " (source_type, source_url, video_id, lesson_mode, media_url, media_kind, title, duration, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source_type, source_url, video_id, lesson_mode, media_url, media_kind, title, duration, now, now),
        )
        row = conn.execute(
            "SELECT * FROM v2_lessons WHERE source_type=? AND source_url=?",
            (source_type, source_url),
        ).fetchone()
    return dict(row)


def get_v2_lesson(lesson_id: int) -> Optional[dict]:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM v2_lessons WHERE id=?", (lesson_id,)
        ).fetchone()
    return dict(row) if row else None


def get_v2_lesson_by_source(source_type: str, source_url: str) -> Optional[dict]:
    """按 (source_type, source_url) 查课：建课幂等/崩溃恢复用（组合唯一）。"""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM v2_lessons WHERE source_type=? AND source_url=?",
            (source_type, source_url),
        ).fetchone()
    return dict(row) if row else None


# ── v2_media_uploads：普通用户浏览器音视频上传暂存 ─────────────
# status 流转：ready →（原子 consume）→ consumed；未消费可 → deleted。
# 记录保存在当前用户自己的 vocab.db，跨用户天然不可见。

def create_v2_media_upload(upload_id: str, original_filename: str, stored_relpath: str,
                           media_kind: str, size_bytes: int, duration_seconds: float) -> dict:
    with _db() as conn:
        conn.execute(
            "INSERT INTO v2_media_uploads"
            " (id, original_filename, stored_relpath, media_kind, size_bytes, duration_seconds, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'ready', ?)",
            (upload_id, original_filename, stored_relpath, media_kind,
             int(size_bytes), float(duration_seconds), _now_iso()),
        )
    return get_v2_media_upload(upload_id)


def get_v2_media_upload(upload_id: str) -> Optional[dict]:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM v2_media_uploads WHERE id=?", (upload_id,)
        ).fetchone()
    return dict(row) if row else None


def consume_v2_media_upload(upload_id: str) -> bool:
    """原子地将 ready → consumed；已被消费/删除或不存在返回 False。"""
    with _db() as conn:
        cur = conn.execute(
            "UPDATE v2_media_uploads SET status='consumed', consumed_at=?"
            " WHERE id=? AND status='ready'",
            (_now_iso(), upload_id),
        )
    return cur.rowcount > 0


def restore_v2_media_upload_ready(upload_id: str) -> bool:
    """消费后失败回滚：consumed → ready；仅本轮获胜消费者可回滚（状态仍 consumed）。"""
    with _db() as conn:
        cur = conn.execute(
            "UPDATE v2_media_uploads SET status='ready', consumed_at=''"
            " WHERE id=? AND status='consumed'",
            (upload_id,),
        )
    return cur.rowcount > 0


def mark_v2_media_upload_deleted(upload_id: str) -> bool:
    """仅未 consumed 的上传可删除；返回是否真的标记成功。"""
    with _db() as conn:
        cur = conn.execute(
            "UPDATE v2_media_uploads SET status='deleted'"
            " WHERE id=? AND status != 'consumed'",
            (upload_id,),
        )
    return cur.rowcount > 0


def list_v2_lessons(include_archived: bool = False) -> list[dict]:
    where = "" if include_archived else "WHERE lesson.archived=0"
    with _db() as conn:
        rows = conn.execute(
            f"""
            SELECT lesson.*,
                   progress.last_position_seconds,
                   progress.last_segment_index,
                   progress.updated_at AS progress_updated_at,
                   (SELECT COUNT(*) FROM v2_subtitle_segments AS segment
                    WHERE segment.lesson_id=lesson.id) AS subtitle_count,
                   (SELECT COUNT(*) FROM v2_reading_blocks AS block
                    WHERE block.lesson_id=lesson.id) AS reading_block_count,
                   (SELECT COUNT(*) FROM v2_lesson_words AS lesson_word
                    WHERE lesson_word.lesson_id=lesson.id) AS word_count,
                   (SELECT COUNT(*) FROM v2_phase_b_sentences AS phase_b
                    WHERE phase_b.lesson_id=lesson.id) AS saved_sentence_count
            FROM v2_lessons AS lesson
            LEFT JOIN v2_lesson_progress AS progress ON progress.lesson_id=lesson.id
            {where}
            ORDER BY COALESCE(NULLIF(progress.updated_at, ''), NULLIF(lesson.updated_at, ''), lesson.created_at) DESC,
                     lesson.id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def set_v2_lesson_archived(lesson_id: int, archived: bool) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE v2_lessons SET archived=?, updated_at=? WHERE id=?",
            (1 if archived else 0, _now_iso(), lesson_id),
        )


def normalize_lesson_tags(tags) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for tag in tags or []:
        name = str(tag).strip()
        if not name or len(name) > 20:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(name)
        if len(normalized) >= 10:
            break
    return normalized


def set_v2_lesson_tags(lesson_id: int, tags) -> list[str]:
    normalized = normalize_lesson_tags(tags)
    with _db() as conn:
        conn.execute(
            "UPDATE v2_lessons SET tags=?, updated_at=? WHERE id=?",
            (json.dumps(normalized, ensure_ascii=False), _now_iso(), lesson_id),
        )
    return normalized


def delete_v2_lesson(lesson_id: int) -> bool:
    with _db() as conn:
        for table in (
            "v2_subtitle_segments",
            "v2_reading_blocks",
            "v2_lesson_progress",
            "v2_chat_messages",
            "v2_chat_sessions",
            "v2_lesson_summaries",
            "v2_phase_b_sentences",
            "v2_lesson_words",
            "v2_lesson_hidden_words",
        ):
            conn.execute(f"DELETE FROM {table} WHERE lesson_id=?", (lesson_id,))
        cursor = conn.execute("DELETE FROM v2_lessons WHERE id=?", (lesson_id,))
    return cursor.rowcount > 0


def get_cached_v2_reading_lesson(source_url: str) -> Optional[dict]:
    with _db() as conn:
        row = conn.execute(
            "SELECT lesson.* FROM v2_lessons AS lesson"
            " WHERE lesson.source_type='reading_upload'"
            " AND (lesson.source_url=? OR lesson.source_url LIKE ?)"
            " AND EXISTS (SELECT 1 FROM v2_reading_blocks AS block WHERE block.lesson_id=lesson.id)",
            (source_url, source_url + ':%'),
        ).fetchone()
    return dict(row) if row else None


def update_v2_lesson_metadata(lesson_id: int, *, title: str | None = None,
                              duration: float | None = None,
                              media_url: str | None = None,
                              media_kind: str | None = None,
                              source_type: str | None = None,
                              lesson_mode: str | None = None) -> None:
    now = _now_iso()
    sets = ["updated_at = ?"]
    params: list = [now]
    if title is not None:
        sets.append("title = ?")
        params.append(title)
    if duration is not None:
        sets.append("duration = ?")
        params.append(duration)
    if media_url is not None:
        sets.append("media_url = ?")
        params.append(media_url)
    if media_kind is not None:
        sets.append("media_kind = ?")
        params.append(media_kind)
    if source_type is not None:
        sets.append("source_type = ?")
        params.append(source_type)
    if lesson_mode is not None:
        sets.append("lesson_mode = ?")
        params.append(lesson_mode)
    params.append(lesson_id)
    with _db() as conn:
        conn.execute(
            f"UPDATE v2_lessons SET {', '.join(sets)} WHERE id = ?", params
        )


def configure_v2_lesson_translation(lesson_id: int, *, requested: bool) -> None:
    now = _now_iso()
    with _db() as conn:
        conn.execute(
            "UPDATE v2_lessons SET translation_requested=?, translation_status=?,"
            " translation_done=0, translation_total=0, translation_buffer_seconds=0,"
            " translation_rate=0, translation_ready=0, translation_error='', updated_at=?"
            " WHERE id=?",
            (int(requested), "pending" if requested else "disabled", now, lesson_id),
        )


def update_v2_translation_status(lesson_id: int, *, status: str | None = None,
                                 done: int | None = None, total: int | None = None,
                                 buffer_seconds: float | None = None,
                                 rate: float | None = None, ready: bool | None = None,
                                 error: str | None = None) -> None:
    sets = ["updated_at = ?"]
    params: list = [_now_iso()]
    for column, value in (
        ("translation_status", status),
        ("translation_done", done),
        ("translation_total", total),
        ("translation_buffer_seconds", buffer_seconds),
        ("translation_rate", rate),
        ("translation_ready", int(ready) if ready is not None else None),
        ("translation_error", error),
    ):
        if value is not None:
            sets.append(f"{column} = ?")
            params.append(value)
    params.append(lesson_id)
    with _db() as conn:
        conn.execute(f"UPDATE v2_lessons SET {', '.join(sets)} WHERE id = ?", params)


def set_v2_lesson_status(lesson_id: int, *, subtitle_status: str | None = None,
                         summary_status: str | None = None,
                         subtitle_error: str | None = None, summary_error: str | None = None) -> None:
    now = _now_iso()
    sets = ["updated_at = ?"]
    params: list = [now]
    if subtitle_status is not None:
        sets.append("subtitle_status = ?")
        params.append(subtitle_status)
    if summary_status is not None:
        sets.append("summary_status = ?")
        params.append(summary_status)
    # None=不动该列；显式空串=清除；状态转为非失败（pending/ready/取消）且未显式给错误时自动清除 stale error
    if subtitle_error is not None:
        sets.append("subtitle_error = ?")
        params.append(subtitle_error)
    elif subtitle_status in ("pending", "ready", ""):
        sets.append("subtitle_error = ?")
        params.append("")
    if summary_error is not None:
        sets.append("summary_error = ?")
        params.append(summary_error)
    elif summary_status in ("pending", "ready", ""):
        sets.append("summary_error = ?")
        params.append("")
    params.append(lesson_id)
    with _db() as conn:
        conn.execute(
            f"UPDATE v2_lessons SET {', '.join(sets)} WHERE id = ?", params
        )


def replace_v2_subtitle_segments(lesson_id: int, segments: list[dict]) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM v2_subtitle_segments WHERE lesson_id=?", (lesson_id,))
        conn.executemany(
            "INSERT INTO v2_subtitle_segments (lesson_id, idx, start, end, text, normalized, words_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    lesson_id, s["index"], s["start"], s["end"], s["text"], s["text"].lower(),
                    json.dumps(s["words"], ensure_ascii=False) if s.get("words") else "",
                )
                for s in segments
            ],
        )


def get_v2_subtitle_segments(lesson_id: int) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT idx, start, end, text, words_json FROM v2_subtitle_segments"
            " WHERE lesson_id=? ORDER BY idx",
            (lesson_id,),
        ).fetchall()
    out = []
    for r in rows:
        seg = {"index": r["idx"], "start": r["start"], "end": r["end"], "text": r["text"]}
        if r["words_json"]:
            try:
                seg["words"] = json.loads(r["words_json"])
            except Exception:
                pass
        out.append(seg)
    return out


def replace_v2_reading_blocks(lesson_id: int, blocks: list[dict]) -> None:
    now = _now_iso()
    with _db() as conn:
        conn.execute("DELETE FROM v2_reading_blocks WHERE lesson_id=?", (lesson_id,))
        conn.executemany(
            "INSERT INTO v2_reading_blocks"
            " (lesson_id, block_index, text, start_seconds, end_seconds,"
            " sentences_json, source_segment_ids, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    lesson_id,
                    int(block["index"]),
                    str(block["text"]),
                    block.get("start_seconds"),
                    block.get("end_seconds"),
                    json.dumps(block.get("sentences", []), ensure_ascii=False),
                    json.dumps(block.get("source_segment_ids", []), ensure_ascii=False),
                    now,
                )
                for block in blocks
            ],
        )


def get_v2_reading_blocks(lesson_id: int) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT block_index, text, start_seconds, end_seconds,"
            " sentences_json, source_segment_ids FROM v2_reading_blocks"
            " WHERE lesson_id=? ORDER BY block_index",
            (lesson_id,),
        ).fetchall()
    return [
        {
            "index": row["block_index"],
            "text": row["text"],
            "start_seconds": row["start_seconds"],
            "end_seconds": row["end_seconds"],
            "sentences": json.loads(row["sentences_json"] or "[]"),
            "source_segment_ids": json.loads(row["source_segment_ids"] or "[]"),
        }
        for row in rows
    ]


def get_v2_document_outline(lesson_id: int, content_hash: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT outline_json, updated_at FROM v2_document_outlines"
            " WHERE lesson_id=? AND content_hash=?",
            (lesson_id, content_hash),
        ).fetchone()
    if not row:
        return None
    try:
        outline = json.loads(row["outline_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    return {"outline": outline, "updated_at": row["updated_at"]}


def get_latest_v2_document_outline(lesson_id: int) -> dict | None:
    """RAG 章节路由用：取最新缓存 outline，不限定 content_hash。"""
    with _db() as conn:
        row = conn.execute(
            "SELECT outline_json, updated_at FROM v2_document_outlines"
            " WHERE lesson_id=? ORDER BY updated_at DESC LIMIT 1",
            (lesson_id,),
        ).fetchone()
    if not row:
        return None
    try:
        outline = json.loads(row["outline_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    return {"outline": outline, "updated_at": row["updated_at"]}


def save_v2_document_outline(lesson_id: int, content_hash: str, outline: dict) -> dict:
    now = _now_iso()
    payload = json.dumps(outline, ensure_ascii=False)
    with _db() as conn:
        conn.execute(
            "INSERT INTO v2_document_outlines (lesson_id, content_hash, outline_json, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(lesson_id) DO UPDATE SET"
            " content_hash=excluded.content_hash, outline_json=excluded.outline_json, updated_at=excluded.updated_at",
            (lesson_id, content_hash, payload, now),
        )
    return {"outline": outline, "updated_at": now}


def upsert_v2_lesson_progress(lesson_id: int, last_position_seconds: float,
                              last_segment_index: int) -> None:
    now = _now_iso()
    with _db() as conn:
        conn.execute(
            "INSERT INTO v2_lesson_progress"
            " (lesson_id, last_position_seconds, last_segment_index, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(lesson_id) DO UPDATE SET"
            " last_position_seconds = excluded.last_position_seconds,"
            " last_segment_index = excluded.last_segment_index,"
            " updated_at = excluded.updated_at",
            (lesson_id, last_position_seconds, last_segment_index, now),
        )


def get_v2_lesson_progress(lesson_id: int) -> Optional[dict]:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM v2_lesson_progress WHERE lesson_id=?", (lesson_id,)
        ).fetchone()
    return dict(row) if row else None


def create_v2_chat_session(lesson_id: int, title: str = "") -> dict:
    now = _now_iso()
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO v2_chat_sessions (lesson_id, title, created_at, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (lesson_id, title, now, now),
        )
        row = conn.execute(
            "SELECT * FROM v2_chat_sessions WHERE id=?", (cur.lastrowid,)
        ).fetchone()
    return dict(row)


def get_v2_chat_session(session_id: int) -> Optional[dict]:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM v2_chat_sessions WHERE id=?", (session_id,)
        ).fetchone()
    return dict(row) if row else None


def list_v2_chat_sessions(lesson_id: int) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM v2_chat_sessions WHERE lesson_id=?"
            " ORDER BY updated_at DESC, id DESC",
            (lesson_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_v2_chat_message(
    lesson_id: int,
    timestamp_seconds: float,
    selected_start_seconds: float | None,
    selected_end_seconds: float | None,
    selected_segment_ids: list[int],
    user_message: str,
    ai_response: str,
    context_mode: str = "auto",
    session_id: int | None = None,
    coverage_status: str = "",
    external_knowledge_used: bool = False,
    citations: list[dict] | None = None,
    unsupported: list[str] | None = None,
) -> dict:
    now = _now_iso()
    if session_id is None:
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM v2_chat_sessions"
                " WHERE lesson_id=? AND title='Legacy conversation'"
                " ORDER BY id ASC LIMIT 1",
                (lesson_id,),
            ).fetchone()
        session_id = (
            row["id"]
            if row
            else create_v2_chat_session(lesson_id, title="Legacy conversation")["id"]
        )
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO v2_chat_messages"
            " (lesson_id, session_id, timestamp_seconds, selected_start_seconds, selected_end_seconds,"
            "  selected_segment_ids, user_message, ai_response, context_mode,"
            "  coverage_status, external_knowledge_used, citations_json, unsupported_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                lesson_id, session_id, timestamp_seconds, selected_start_seconds, selected_end_seconds,
                json.dumps(selected_segment_ids, ensure_ascii=False),
                user_message, ai_response, context_mode,
                coverage_status, int(bool(external_knowledge_used)),
                json.dumps(citations or [], ensure_ascii=False),
                # 有界：最多 5 条缺口说明
                json.dumps([str(x) for x in (unsupported or [])][:5], ensure_ascii=False),
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM v2_chat_messages WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        conn.execute(
            "UPDATE v2_chat_sessions SET updated_at=? WHERE id=? AND lesson_id=?",
            (now, session_id, lesson_id),
        )
    return _normalize_chat_message(dict(row))


def _normalize_chat_message(d: dict) -> dict:
    d["selected_segment_ids"] = json.loads(d.get("selected_segment_ids") or "[]")
    try:
        d["citations"] = json.loads(d.pop("citations_json", None) or "[]")
    except (TypeError, json.JSONDecodeError):
        d["citations"] = []
    try:
        d["unsupported"] = json.loads(d.pop("unsupported_json", None) or "[]")
    except (TypeError, json.JSONDecodeError):
        d["unsupported"] = []
    if not isinstance(d["unsupported"], list):
        d["unsupported"] = []
    d["unsupported"] = [str(x) for x in d["unsupported"]][:5]
    d["external_knowledge_used"] = bool(d.get("external_knowledge_used"))
    return d


def get_v2_chat_history(
    lesson_id: int, session_id: int | None = None, limit: int = 50
) -> list[dict]:
    with _db() as conn:
        if session_id is None:
            rows = conn.execute(
                "SELECT * FROM v2_chat_messages WHERE lesson_id=?"
                " ORDER BY id ASC LIMIT ?",
                (lesson_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM v2_chat_messages WHERE lesson_id=? AND session_id=?"
                " ORDER BY id ASC LIMIT ?",
                (lesson_id, session_id, limit),
            ).fetchall()
    result = []
    for r in rows:
        result.append(_normalize_chat_message(dict(r)))
    return result


def save_v2_phase_b_sentence(
    lesson_id: int,
    segment_index: int,
    start_seconds: float,
    end_seconds: float,
    text: str,
) -> dict:
    now = _now_iso()
    sentence = upsert_v2_sentence(text)
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO v2_phase_b_sentences"
            " (lesson_id, sentence_id, segment_index, start_seconds, end_seconds, text, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (lesson_id, sentence["id"], segment_index, start_seconds, end_seconds, text, now),
        )
        row = conn.execute(
            "SELECT * FROM v2_phase_b_sentences"
            " WHERE lesson_id=? AND segment_index=?",
            (lesson_id, segment_index),
        ).fetchone()
    result = dict(row)
    result["tags"] = get_v2_sentence_tags(result["sentence_id"]) if result.get("sentence_id") else []
    return result


def get_v2_phase_b_sentences(lesson_id: int) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM v2_phase_b_sentences WHERE lesson_id=?"
            " ORDER BY segment_index",
            (lesson_id,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        if not item.get("sentence_id") and item.get("text"):
            sentence = upsert_v2_sentence(item["text"])
            item["sentence_id"] = sentence["id"]
            with _db() as conn:
                conn.execute(
                    "UPDATE v2_phase_b_sentences SET sentence_id=? WHERE id=?",
                    (sentence["id"], item["id"]),
                )
        item["tags"] = get_v2_sentence_tags(item["sentence_id"]) if item.get("sentence_id") else []
        item["pattern"] = get_v2_sentence_pattern(item["sentence_id"]) if item.get("sentence_id") else None
        result.append(item)
    return result


def delete_v2_phase_b_sentence(lesson_id: int, segment_index: int) -> bool:
    with _db() as conn:
        result = conn.execute(
            "DELETE FROM v2_phase_b_sentences WHERE lesson_id=? AND segment_index=?",
            (lesson_id, segment_index),
        )
    return result.rowcount > 0
