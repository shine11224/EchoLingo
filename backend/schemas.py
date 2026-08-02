from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Segment:
    index: int
    text: str
    start: float | None = None
    end: float | None = None
    translation: str = ""


@dataclass
class VocabularyItem:
    word: str
    ipa: str
    meaning: str
    example: str = ""


@dataclass
class PatternItem:
    template: str
    usage: str


@dataclass
class SentenceAnalysis:
    text: str
    phonetics: str
    phonetics_canonical: str = ""
    phonetics_natural: str = ""
    phonetics_source: str = "rule"
    phonetics_confidence: float | None = None
    phonetics_why_changed: str = ""
    translation: str = ""
    vocabulary: list[VocabularyItem] = field(default_factory=list)
    pattern: PatternItem = field(default_factory=lambda: PatternItem(template="", usage=""))
    connected_speech: list = field(default_factory=list)  # list[str | dict]
    pronunciation_hints: list = field(default_factory=list)
    oral_analysis: str = ""
    difficulty: str = "B1"
    # 以下字段由 NATURAL_COMBINED_ANALYSIS_PROMPT 路径填充
    speech_features: list[str] = field(default_factory=list)  # 语流特征标签：连读/吞音/弱读/弱读链/省略/重读
    learning_value: str = ""  # 学习价值：高/中/低
    expression_function: str = ""  # 提出观点/举例说明/转折让步/强调递进/因果解释/提出建议/总结收尾
    topic_tag: str = ""  # 爱好兴趣/职业工作/家庭关系/健康生活/学习成长/科技数字/社会文化/旅行体验


@dataclass
class SourceBundle:
    source_type: str
    title: str
    source_value: str
    segments: list[Segment]
    youtube_id: str | None = None
    local_video: Path | None = None
    article_url: str | None = None
