from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import analyzer
from analyzer import SentenceAnalyzer
from schemas import Segment, SentenceAnalysis


class FakeChatCompletions:
    def __init__(self, payloads: list[dict]):
        self._payloads = list(payloads)

    def create(self, **_kwargs):
        if not self._payloads:
            raise AssertionError("fake client exhausted")
        content = json.dumps(self._payloads.pop(0))
        message = SimpleNamespace(content=content)
        choice = SimpleNamespace(message=message)
        return SimpleNamespace(choices=[choice])


class FakeClient:
    def __init__(self, payloads: list[dict]):
        self.chat = SimpleNamespace(completions=FakeChatCompletions(payloads))


def _seg(index: int, text: str, start: float, end: float) -> Segment:
    return Segment(index=index, text=text, start=start, end=end, translation="")


class AnalyzerCanonicalIpaTest(unittest.TestCase):
    def test_canonical_ipa_applies_high_confidence_word_overrides(self):
        reading = analyzer._to_canonical_ipa("Reading in a certain way.")
        self.assertIn("\u02c8ri\u02d0d\u026a\u014b", reading)
        self.assertNotIn("\u02c8r\u025bd\u026a\u014b", reading)

        slightly = analyzer._to_canonical_ipa("It slightly changes.")
        self.assertIn("\u02c8sla\u026atli", slightly)
        self.assertNotIn("s\u02c8la\u026atli", slightly)

        us = analyzer._to_canonical_ipa("It allows us.")
        self.assertIn("\u0259s", us)
        self.assertNotIn("\u02c8ju\u02c8\u025bs", us)

    def test_canonical_ipa_does_not_treat_uppercase_us_as_pronoun(self):
        result = analyzer._to_canonical_ipa("The US market.")

        self.assertIn("\u02c8ju\u02c8\u025bs", result)
        self.assertNotIn(" \u0259s ", result)

    def test_canonical_ipa_disambiguates_read_from_context(self):
        present = analyzer._to_canonical_ipa("You can read a page.")
        self.assertIn("ri\u02d0d", present)
        self.assertNotIn("r\u025bd \u0259", present)

        past = analyzer._to_canonical_ipa("You've read a page.")
        self.assertIn("r\u025bd", past)
        self.assertNotIn("ri\u02d0d \u0259", past)


class AnalyzerPipelineContractsTest(unittest.TestCase):
    def setUp(self):
        self.ipa_patchers = [
            patch.dict("os.environ", {"AI_API_KEY": "test-key"}),
            patch.object(analyzer, "_to_canonical_ipa", lambda text: f"canonical:{text}"),
            patch.object(analyzer, "_to_rule_natural_ipa", lambda text: f"rule:{text}"),
        ]
        for patcher in self.ipa_patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_step2_preserves_input_sentence_set_when_ai_omits_and_reorders(self):
        subject = SentenceAnalyzer(mode="auto")
        subject.client = FakeClient([
            {
                "sentences": [
                    {
                        "indices": [3],
                        "text": "Third sentence.",
                        "translation": "third zh",
                        "natural_ipa": "ai third",
                    },
                    {
                        "indices": [1],
                        "text": "First sentence.",
                        "translation": "first zh",
                        "natural_ipa": "ai first",
                    },
                ]
            }
        ])

        batch = [
            _seg(1, "First sentence.", 0.0, 1.0),
            _seg(2, "Second sentence.", 1.0, 2.0),
            _seg(3, "Third sentence.", 2.0, 3.0),
        ]

        segments, analyses = subject._analyze_combined_batch(batch, "lesson")

        self.assertEqual([s.text for s in segments], [s.text for s in batch])
        self.assertEqual([a.text for a in analyses], [s.text for s in batch])
        self.assertEqual(len(segments), len(batch))
        self.assertEqual(len(analyses), len(batch))
        self.assertEqual(analyses[1].phonetics, "rule:Second sentence.")

    def test_step3_analysis_uses_precomputed_ipa_without_overwriting_it(self):
        subject = SentenceAnalyzer(mode="auto")
        subject.client = FakeClient([
            {
                "sentences": [
                    {
                        "indices": [1],
                        "text": "First sentence.",
                        "translation": "first zh",
                        "natural_ipa": "ai text ipa",
                    }
                ]
            }
        ])
        batch = [_seg(1, "First sentence.", 0.0, 1.0)]

        _segments, analyses = subject._analyze_combined_batch(
            batch,
            "lesson",
            precomputed_ipa_by_index={1: "precomputed ipa"},
        )

        self.assertEqual(analyses[0].phonetics, "precomputed ipa")
        self.assertEqual(analyses[0].phonetics_natural, "precomputed ipa")
        self.assertEqual(analyses[0].phonetics_source, "ai_ipa")
        self.assertEqual(analyses[0].translation, "first zh")

    def test_step1_splits_obvious_multi_sentence_ai_merge(self):
        subject = SentenceAnalyzer(mode="auto")
        subject.client = FakeClient([
            {
                "break_after_token_ids": [2, 4],
            }
        ])
        raw = [
            _seg(1, "First sentence.", 10.0, 11.0),
            _seg(2, "Second sentence?", 11.0, 12.0),
        ]

        merged = subject._merge_raw_segments(raw)

        self.assertEqual([s.text for s in merged], ["First sentence.", "Second sentence?"])
        self.assertEqual([(s.start, s.end) for s in merged], [(10.0, 11.0), (11.0, 12.0)])

    def test_step1_splits_overlong_unpunctuated_ai_merge(self):
        subject = SentenceAnalyzer(mode="auto")
        long_text = " ".join(f"word{i}" for i in range(80))
        subject.client = FakeClient([
            {
                "break_after_token_ids": [],
            }
        ])

        merged = subject._merge_raw_segments([_seg(1, long_text, 0.0, 8.0)])

        self.assertGreaterEqual(len(merged), 2)
        self.assertTrue(all(len(s.text.split()) <= 45 for s in merged))
        self.assertEqual(merged[0].start, 0.0)
        self.assertEqual(merged[-1].end, 8.0)

    def test_step1_merges_comma_ended_candidate_with_next_fragment_locally(self):
        subject = SentenceAnalyzer(mode="auto")
        subject.client = FakeClient([])
        raw = [
            _seg(1, "If this is truly the golden age of consumer products,", 0.0, 1.0),
            _seg(2, "there is a lot we can learn.", 2.0, 3.0),
        ]

        merged = subject._merge_raw_segments(raw)

        self.assertEqual(
            [s.text for s in merged],
            ["If this is truly the golden age of consumer products, there is a lot we can learn."],
        )
        self.assertEqual((merged[0].start, merged[0].end), (0.0, 3.0))

    def test_step1_merges_comma_ended_overlong_split_when_still_reasonable(self):
        text = (
            "If this is truly the golden age of consumer products, like many people say AI is going to enable, "
            "there is a lot that we can learn about how Evan and his team think and operate."
        )

        pieces = SentenceAnalyzer._split_text_sentences(text)

        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0], text)
        self.assertLessEqual(len(pieces[0].split()), 45)

    def test_step1_keeps_ambiguous_punctuationless_windows_local_when_ai_review_disabled(self):
        subject = SentenceAnalyzer(mode="auto")
        subject._ENABLE_AI_BOUNDARY_REVIEW = False
        raw = [
            _seg(1, "Finished sentence.", 0.0, 1.0),
            _seg(2, "I want to go", 2.0, 3.0),
            _seg(3, "home soon", 4.0, 5.0),
            _seg(4, "Another finished sentence.", 6.0, 7.0),
        ]

        merged = subject._merge_raw_segments(raw)

        self.assertEqual(
            [s.text for s in merged],
            ["Finished sentence.", "I want to go home soon Another finished sentence."],
        )
        self.assertEqual((merged[1].start, merged[1].end), (2.0, 7.0))

    def test_step1_rebuilds_ai_cue_boundaries_from_original_text(self):
        subject = SentenceAnalyzer(mode="auto")
        subject.client = FakeClient([{"break_after_token_ids": [10]}])
        raw = [
            _seg(1, "I want to go", 2.0, 3.0),
            _seg(2, "home soon", 4.0, 5.0),
            _seg(3, "before the rain starts", 5.0, 6.0),
            _seg(4, "Another finished sentence.", 6.0, 7.0),
        ]

        merged = subject._merge_raw_segments(raw)

        self.assertEqual(
            [s.text for s in merged],
            ["I want to go home soon before the rain starts", "Another finished sentence."],
        )
        self.assertEqual([(s.start, s.end) for s in merged], [(2.0, 6.0), (6.0, 7.0)])

    def test_step1_interpolates_timestamp_for_ai_boundary_inside_cue(self):
        subject = SentenceAnalyzer(mode="auto")
        subject.client = FakeClient([{"break_after_token_ids": [8]}])

        merged = subject._merge_raw_segments([
            _seg(1, "one two three four five six seven eight Start again", 10.0, 20.0),
        ])

        self.assertEqual(
            [s.text for s in merged],
            ["one two three four five six seven eight", "Start again"],
        )
        self.assertEqual([(s.start, s.end) for s in merged], [(10.0, 18.0), (18.0, 20.0)])

    def test_step1_rejects_dangling_ai_boundaries(self):
        subject = SentenceAnalyzer(mode="auto")
        subject.client = FakeClient([{"break_after_token_ids": [7, 15]}])
        raw = [
            _seg(1, "the work of a solution engineer revolves around integration", 0.0, 8.0),
            _seg(2, "because that is going to give them information", 8.0, 16.0),
        ]

        merged = subject._merge_raw_segments(raw)

        self.assertEqual(
            [s.text for s in merged],
            ["the work of a solution engineer revolves around integration because that is going to give them information"],
        )

    def test_step1_moves_so_boundary_before_connector(self):
        subject = SentenceAnalyzer(mode="auto")
        subject.client = FakeClient([{"break_after_token_ids": [9]}])
        raw = [
            _seg(1, "we help product teams make that workflow happen so that's why", 0.0, 10.0),
        ]

        merged = subject._merge_raw_segments(raw)

        self.assertEqual(
            [s.text for s in merged],
            ["we help product teams make that workflow happen", "so that's why"],
        )

    def test_step1_overlong_fallback_avoids_dangling_soft_split(self):
        subject = SentenceAnalyzer(mode="auto")
        tokens = [
            _seg(index, word, float(index), float(index) + (0.1 if word == "give" else 0.9))
            for index, word in enumerate(
                "one two three four five six seven eight nine ten eleven twelve give them more details now".split(),
                1,
            )
        ]

        split_at = subject._best_soft_split(tokens)

        self.assertNotEqual(tokens[split_at - 1].text, "give")

    def test_step1_denoise_merges_consecutive_duplicate_sentences_without_losing_time(self):
        subject = SentenceAnalyzer(mode="auto")
        raw = [
            _seg(1, "I don't think that's particularly helpful.", 10.0, 11.0),
            _seg(2, "I don't think that's particularly helpful.", 11.0, 12.5),
        ]

        cleaned = subject._denoise_repeated_sentence_text(raw)

        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0].text, "I don't think that's particularly helpful.")
        self.assertEqual((cleaned[0].start, cleaned[0].end), (10.0, 12.5))

    def test_step1_denoise_compresses_repeated_phrase_inside_sentence(self):
        subject = SentenceAnalyzer(mode="auto")
        raw = [
            _seg(
                1,
                "You should share your share your idea. You should share your share your idea.",
                20.0,
                23.0,
            )
        ]

        cleaned = subject._denoise_repeated_sentence_text(raw)

        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0].text, "You should share your idea.")
        self.assertEqual((cleaned[0].start, cleaned[0].end), (20.0, 23.0))

    def test_step1_denoise_compresses_long_repeated_phrase_inside_sentence(self):
        subject = SentenceAnalyzer(mode="auto")
        raw = [
            _seg(
                1,
                "But I think going deep and talking with But I think going deep and talking with someone for an hour.",
                24.0,
                27.0,
            )
        ]

        cleaned = subject._denoise_repeated_sentence_text(raw)

        self.assertEqual(cleaned[0].text, "But I think going deep and talking with someone for an hour.")
        self.assertEqual((cleaned[0].start, cleaned[0].end), (24.0, 27.0))

    def test_step1_denoise_trims_overlap_prefix_but_keeps_sentence_timing(self):
        subject = SentenceAnalyzer(mode="auto")
        raw = [
            _seg(1, "You can learn so much from listening to customers.", 30.0, 32.0),
            _seg(2, "listening to customers made a huge difference.", 32.0, 34.0),
        ]

        cleaned = subject._denoise_repeated_sentence_text(raw)

        self.assertEqual([s.text for s in cleaned], [
            "You can learn so much from listening to customers.",
            "made a huge difference.",
        ])
        self.assertEqual((cleaned[1].start, cleaned[1].end), (32.0, 34.0))

    def test_step3_invalid_ipa_cannot_overwrite_rule_fallback(self):
        subject = SentenceAnalyzer(mode="auto")
        subject._has_key = True
        subject._ENABLE_AI_IPA_REFINEMENT = True

        source = [_seg(1, "Keep the fallback.", 0.0, 1.0)]
        fallback = SentenceAnalysis(
            text="Keep the fallback.",
            phonetics="rule:Keep the fallback.",
            phonetics_natural="rule:Keep the fallback.",
            phonetics_source="rule",
        )

        subject._merge_raw_segments = lambda segments: segments
        subject._analyze_combined_batch = lambda batch, title, ipa=None: (batch, [fallback])
        subject._refine_ipa_pass = lambda texts: {0: '{"natural_ipa": "bad json fragment"}'}

        _segments, analyses = subject.analyze_from_raw_segments(source, "lesson")

        self.assertEqual(analyses[0].phonetics, "rule:Keep the fallback.")
        self.assertEqual(analyses[0].phonetics_natural, "rule:Keep the fallback.")
        self.assertEqual(analyses[0].phonetics_source, "rule")

    def test_ai_ipa_only_accepts_known_gap_corrections(self):
        subject = SentenceAnalyzer(mode="auto")

        self.assertEqual(
            subject._clean_natural_ipa("ˈlɪmɪtər. ↘", "limiter*. ↘", "rate limiter"),
            "ˈlɪmɪtər. ↘",
        )
        self.assertEqual(
            subject._clean_natural_ipa("ˌθərˈtin jɪrz. ↘", "13 jɪrz. ↘", "13 years."),
            "ˌθərˈtin jɪrz. ↘",
        )
        self.assertEqual(
            subject._clean_natural_ipa("ˌɪmˈpɔrdənt. ↘", "ˌɪmˈpɔrtənt. ↘", "important."),
            "ˌɪmˈpɔrtənt. ↘",
        )
        self.assertEqual(
            subject._clean_natural_ipa("limiter*. ↘", "limiter*. ↘", "rate limiter"),
            "limiter*. ↘",
        )
        self.assertEqual(
            subject._clean_natural_ipa("ˈlɪmɪtər / ↘", "limiter*. ↘", "rate limiter"),
            "limiter*. ↘",
        )


if __name__ == "__main__":
    unittest.main()
