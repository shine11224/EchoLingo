"""V2 chat context builder — four-layer context for AI chat."""
import db


def build_chat_context(
    lesson_id: int,
    message: str,
    session_id: int | None = None,
    timestamp_seconds: float = 0,
    context_mode: str = "auto",
    selected_start_seconds: float | None = None,
    selected_end_seconds: float | None = None,
    selected_segment_ids: list[int] | None = None,
    selected_text: str | None = None,
) -> dict:
    lesson = db.get_v2_lesson(lesson_id)
    if not lesson:
        raise ValueError(f"Lesson {lesson_id} not found")

    segments = db.get_v2_subtitle_segments(lesson_id)
    history = db.get_v2_chat_history(lesson_id, session_id=session_id, limit=5)

    title_line = f"Lesson: {lesson.get('title') or 'Untitled'}"
    source_line = f"Source: {lesson.get('source_url', '')}"

    selected_context = ""
    if context_mode == "reading_selection":
        selected_context = " ".join((selected_text or "").split())
    elif context_mode == "selected_range" and selected_segment_ids:
        selected_context = "\n".join(
            s["text"] for s in segments
            if s["index"] in selected_segment_ids
        )
    else:
        nearby = [s for s in segments if abs(s["start"] - timestamp_seconds) < 30]
        if nearby:
            selected_context = "\n".join(s["text"] for s in nearby)
        else:
            selected_context = "\n".join(s["text"] for s in segments)

    summary_text = ""
    if lesson.get("summary_status") == "ready":
        summary_text = "Video summary is ready. Use the summary for context."
    else:
        summary_text = "Video summary is still pending; use local subtitle context."

    history_text = ""
    if history:
        lines = []
        for h in history[-5:]:
            lines.append(f"User: {h['user_message']}\nAI: {h['ai_response']}")
        history_text = "\n\n".join(lines)

    prompt_parts = [
        f"[{title_line}]",
        f"[{source_line}]",
        f"[Summary status: {summary_text}]",
    ]

    if history_text:
        prompt_parts.append(f"[Recent chat history]\n{history_text}")

    if selected_context:
        context_label = "Reading selection" if context_mode == "reading_selection" else "Subtitle context"
        prompt_parts.append(f"[{context_label}]\n{selected_context}")

    prompt_parts.append(f"[User message]\n{message}")

    prompt = "\n\n".join(prompt_parts)

    return {
        "prompt": prompt,
        "context_mode": context_mode,
        "selected_context": selected_context,
    }
