import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


def test_vocab_story_deduplicates_and_caps_selection_at_twenty(monkeypatch):
    from webapp.fastapi_routes import vocab

    captured = {}
    monkeypatch.setattr(vocab.db, "get_story", lambda _key: None)
    monkeypatch.setattr(vocab.db, "get_all_words", lambda: {})
    monkeypatch.setattr(
        vocab.db,
        "save_story",
        lambda _key, words, _story, _date: captured.update(words=words),
    )

    def create(**kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        message = SimpleNamespace(content='{"story_content":"A story.","used_words":[]}')
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(
        vocab.ai_config,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    monkeypatch.setattr(vocab.ai_config, "AI_MODEL", "test-model")

    words = [f"word{i}" for i in range(22)] + ["word1"]
    result = vocab.vocab_story(vocab.VocabStoryBody(words=words))

    assert result["story"] == "A story."
    assert captured["words"] == [f"word{i}" for i in range(20)]
    assert "- word19" in captured["prompt"]
    assert "- word20" not in captured["prompt"]
