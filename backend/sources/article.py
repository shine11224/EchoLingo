from __future__ import annotations

import re

import requests
from bs4 import BeautifulSoup

from schemas import Segment, SourceBundle


def build_article_lesson(url: str) -> SourceBundle:
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    title = (soup.title.string or url).strip() if soup.title else url
    text_nodes = [node.get_text(" ", strip=True) for node in soup.select("article p, main p, p")]
    text = "\n".join(node for node in text_nodes if node)
    if not text:
        text = soup.get_text(" ", strip=True)
    sentences = _split_sentences(text)[:60]
    segments = [Segment(index=i + 1, text=sentence) for i, sentence in enumerate(sentences)]
    return SourceBundle(
        source_type="article",
        title=title,
        source_value=url,
        segments=segments,
        article_url=url,
    )


def _split_sentences(text: str) -> list[str]:
    raw = re.split(r"(?<=[.!?])\s+", text)
    return [sentence.strip() for sentence in raw if sentence.strip()]
