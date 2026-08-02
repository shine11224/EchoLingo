import datetime

import db
from db import get_lessons, get_recent_study_sessions
from webapp.storage.lessons import scan_lessons


def word_review_status(entry: dict, today: datetime.date) -> str:
    try:
        last = datetime.date.fromisoformat(entry.get("last_studied") or "")
    except ValueError:
        return "due"
    intervals = [1, 3, 7, 14, 30, 60]
    count = max(1, int(entry.get("count") or 1))
    interval = intervals[min(count - 1, len(intervals) - 1)]
    return "due" if (today - last).days >= interval else "ok"


def build_today_lesson_task() -> dict | None:
    lessons = get_lessons(include_archived=False) or scan_lessons()
    if not lessons:
        return None
    lesson_map = {row["filename"]: row for row in lessons}
    session = None
    lesson = None
    for candidate in get_recent_study_sessions(10):
        lesson = lesson_map.get(candidate.get("lesson_filename"))
        if lesson:
            session = candidate
            break
    if lesson is None:
        lesson = lessons[0]

    total = int((session or {}).get("total_sentences") or lesson.get("sentence_count") or 0)
    idx = int((session or {}).get("current_sentence_idx") or 0)
    idx = max(0, min(idx, max(total - 1, 0))) if total else max(0, idx)
    if total:
        note = f"上次到第 {idx + 1}/{total} 句"
        progress = round(((idx + 1) / total) * 100)
    elif session:
        note = f"上次到第 {idx + 1} 句"
        progress = 0
    else:
        note = "还没有学习进度，先打开最近课程"
        progress = 0

    return {
        "filename": lesson.get("filename", ""),
        "title": lesson.get("title") or lesson.get("filename", ""),
        "sentence_count": lesson.get("sentence_count", 0),
        "current_sentence_idx": idx if session else None,
        "total_sentences": total,
        "progress_percent": progress,
        "note": note,
        "url": f"/output/{lesson.get('filename', '')}",
    }


def get_today_tasks(today: datetime.date | None = None) -> dict:
    today = today or datetime.date.today()
    today_s = today.isoformat()
    words = db.get_all_words()
    entries = [(word, entry) for word, entry in words.items() if not word.startswith("__")]
    today_words = [
        word for word, entry in entries
        if (entry.get("first_studied") or "") == today_s
    ]
    lesson_task = build_today_lesson_task()

    completion_items = []
    if lesson_task:
        completion_items.append(f"继续《{lesson_task['title']}》到上次句子位置")
    else:
        completion_items.append("生成或打开一节课程，建立今天的学习进度")

    return {
        "date": today_s,
        "review": {
            "today_learned": len(today_words),
        },
        "lesson": lesson_task,
        "completion_items": completion_items,
    }
