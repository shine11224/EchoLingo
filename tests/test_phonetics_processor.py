import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from phonetics_processor import _add_elision, _add_linking, annotate


class PhoneticsProcessorRulesTest(unittest.TestCase):
    def test_linking_marks_common_spoken_chunks(self):
        self.assertEqual(_add_linking(["kind", "of"], ["kaɪnd", "əv"], set()), ["kaɪnd", "‿əv"])
        self.assertEqual(_add_linking(["lot", "of"], ["lɑt", "əv"], set()), ["lɑt", "‿əv"])
        self.assertEqual(_add_linking(["going", "to"], ["goʊɪŋ", "tə"], set()), ["goʊɪŋ", "‿tə"])
        self.assertEqual(_add_linking(["take", "a"], ["teɪk", "ə"], set()), ["teɪk", "‿ə"])
        self.assertEqual(_add_linking(["read", "a"], ["riːd", "ə"], set()), ["riːd", "‿ə"])
        self.assertEqual(_add_linking(["make", "it"], ["meɪk", "ɪt"], set()), ["meɪk", "‿ɪt"])
        self.assertEqual(_add_linking(["apply", "it"], ["əˈplaɪ", "ɪt"], set()), ["əˈplaɪ", "‿ɪt"])

    def test_linking_does_not_mark_ordinary_content_word_links(self):
        self.assertEqual(_add_linking(["trapped", "in"], ["træpt", "ɪn"], set()), ["træpt", "ɪn"])
        self.assertEqual(_add_linking(["success", "as"], ["səkˈsɛs", "æz"], set()), ["səkˈsɛs", "æz"])
        self.assertEqual(_add_linking(["come", "into"], ["kʌm", "ɪntu"], set()), ["kʌm", "ɪntu"])

    def test_linking_does_not_cross_pause_boundaries(self):
        self.assertEqual(_add_linking(["kind,", "of"], ["kaɪnd,", "əv"], set()), ["kaɪnd,", "əv"])
        self.assertEqual(_add_linking(["lot", "of"], ["lɑt", "əv"], {1}), ["lɑt", "əv"])

    def test_td_elision_is_limited_to_fast_speech_candidates(self):
        self.assertEqual(_add_elision(["last", "night"], ["læst", "naɪt"], set()), ["læs(t)", "naɪt"])
        self.assertEqual(_add_elision(["and", "then"], ["ænd", "ðen"], set()), ["æn(d)", "ðen"])
        self.assertEqual(_add_elision(["cat", "nap"], ["kæt", "næp"], set()), ["kæt", "næp"])

    def test_td_elision_excludes_boundaries_and_h_y_w_r_next_words(self):
        self.assertEqual(_add_elision(["last,", "night"], ["læst,", "naɪt"], set()), ["læst,", "naɪt"])
        self.assertEqual(_add_elision(["last", "week"], ["læst", "wik"], set()), ["læst", "wik"])
        self.assertEqual(_add_elision(["old", "road"], ["oʊld", "roʊd"], set()), ["oʊld", "roʊd"])

    def test_h_elision_is_only_unstressed_function_candidate(self):
        self.assertEqual(_add_elision(["I", "saw", "him"], ["aɪ", "sɔ", "hɪm"], set()), ["aɪ", "sɔ", "(h)ɪm"])
        self.assertEqual(_add_elision(["Him", "again"], ["hɪm", "əgɛn"], set()), ["hɪm", "əgɛn"])
        self.assertEqual(_add_elision(["I", "saw", "HIM"], ["aɪ", "sɔ", "hɪm"], set()), ["aɪ", "sɔ", "hɪm"])
        self.assertEqual(_add_elision(["I", "saw", "him"], ["aɪ", "sɔ", "hɪm"], {2}), ["aɪ", "sɔ", "hɪm"])
        self.assertEqual(_add_elision(["I", "have", "one"], ["aɪ", "hæv", "wən"], set()), ["aɪ", "hæv", "wən"])

    def test_multisyllable_reduction(self):
        result = annotate("That's probably fine.", "ðæts prɑbəbli faɪn.")
        self.assertIn('prɑbəbli', result)
        self.assertNotIn('prɑbli', result)

        result = annotate("Actually that works.", "ækʧuəli ðæt wɜrks.")
        self.assertIn('ækʧuəli', result)
        self.assertNotIn('ækʃli', result)

        result = annotate("That's interesting.", "ðæts ɪntrəstɪŋ.")
        self.assertIn('ɪntrəstɪŋ', result)

    def test_flapping(self):
        from phonetics_processor import _add_flapping
        self.assertEqual(_add_flapping(['bɛtər']), ['bɛɾər'])
        self.assertEqual(_add_flapping(['wɔtər']), ['wɔɾər'])
        self.assertEqual(_add_flapping(['ɡɛtɪŋ']), ['ɡɛɾɪŋ'])
        self.assertEqual(_add_flapping(['sɪti']), ['sɪɾi'])
        # stop: no intervocalic t, should not change
        self.assertEqual(_add_flapping(['stɑp']), ['stɑp'])
        # start: no intervocalic t, should not change
        self.assertEqual(_add_flapping(['stɑrt']), ['stɑrt'])


    def test_flapping_r_plus_t(self):
        from phonetics_processor import _add_flapping
        self.assertEqual(_add_flapping(['pɑrti']), ['pɑrɾi'])
        self.assertEqual(_add_flapping(['dɜrti']), ['dɜrɾi'])
        self.assertEqual(_add_flapping(['fɔrti']), ['fɔrɾi'])
        self.assertEqual(_add_flapping(['bʌtɚ']), ['bʌɾɚ'])
        self.assertEqual(_add_flapping(['wɔtɚ']), ['wɔɾɚ'])

    def test_annotate_flapping_is_conservative_for_lesson_words(self):
        self.assertIn('ˌɪmˈpɔrtənt', annotate('Important.', 'ˌɪmˈpɔrtənt.'))
        self.assertIn('ˈkɑmpləˌkeɪtəd', annotate('Complicated.', 'ˈkɑmpləˌkeɪtəd.'))
        self.assertIn('ˈdɛdəkeɪtəd', annotate('Dedicated.', 'ˈdɛdəkeɪtəd.'))
        self.assertIn('hæv', annotate('You have one.', 'ju hæv wən.'))
        self.assertNotIn('(h)æv', annotate('You have one.', 'ju hæv wən.'))

    def test_annotate_keeps_common_flapping_examples(self):
        self.assertIn('ˈbɛɾər', annotate('Better.', 'ˈbɛtər.'))
        self.assertIn('ˈsɪɾɪŋ', annotate('Sitting.', 'ˈsɪtɪŋ.'))

    def test_read_is_disambiguated_from_context(self):
        self.assertIn('riːd', annotate('You can read a page.', 'ju kæn rɛd ə peɪdʒ.'))
        self.assertNotIn('rɛd ə', annotate('You can read a page.', 'ju kæn rɛd ə peɪdʒ.'))
        self.assertIn('rɛd', annotate("You've read a page.", 'juv riːd ə peɪdʒ.'))
        self.assertNotIn('riːd ə', annotate("You've read a page.", 'juv riːd ə peɪdʒ.'))

    def test_reviewer_second_round_pronunciation_overrides(self):
        reading = annotate('Reading in a certain way.', 'ˈrɛdɪŋ ɪn ə ˈsɜrtən weɪ.')
        self.assertIn('ˈriːdɪŋ', reading)
        self.assertNotIn('ˈrɛdɪŋ', reading)

        us = annotate('It allows us.', 'ɪt əˈlaʊz ˈjuˈɛs.')
        self.assertIn('əs', us)
        self.assertNotIn('ˈjuˈɛs', us)

        slightly = annotate('It slightly changes.', 'ɪt sˈlaɪtli ˈtʃeɪndʒəz.')
        self.assertIn('ˈslaɪtli', slightly)
        self.assertNotIn('sˈlaɪtli', slightly)

    def test_reviewer_second_round_linking_samples(self):
        self.assertIn('meɪk ‿ɪt', annotate('Make it more effective.', 'meɪk ɪt mɔr ɪˈfɛktɪv.'))
        self.assertIn('teɪk ‿ə', annotate('Take a technique.', 'teɪk ə tɛkˈnik.'))
        self.assertIn('əˈplaɪ ‿ɪt', annotate('Apply it in.', 'əˈplaɪ ɪt ɪn.'))
        self.assertIn('riːd ‿ə', annotate('You can read a page.', 'ju kæn rɛd ə peɪdʒ.'))

    def test_used_to_uses_voiceless_teaching_pronunciation(self):
        adjectival = annotate('You are used to using it.', 'ju ɑr juːzd tu juːzɪŋ ɪt.')
        self.assertIn('ˈjuːst ‿tə', adjectival)
        self.assertNotIn('juːzd ‿tə', adjectival)
        self.assertNotIn('juːz(d) ‿tə', adjectival)

        habitual = annotate('You used to read a lot.', 'ju juːzd tu rɛd ə lɑt.')
        self.assertIn('ˈjuːst ‿tə', habitual)
        self.assertNotIn('juːzd ‿tə', habitual)
        self.assertIn('riːd ‿ə', habitual)

    def test_standalone_used_keeps_ordinary_pronunciation(self):
        standalone = annotate('I used the tool.', 'aɪ juːzd ðə tul.')
        self.assertIn('juːz(d) ðə', standalone)
        self.assertNotIn('ˈjuːst', standalone)

    def test_reviewer_second_round_it_elision_is_conservative(self):
        self.assertNotIn('ɪ(t)', annotate('It goes.', 'ɪt goʊz.'))
        self.assertNotIn('ɪ(t)', annotate('It just works.', 'ɪt dʒʌst wɜrks.'))
        self.assertNotIn('ɪ(t)', annotate('It might work.', 'ɪt maɪt wɜrk.'))
        self.assertNotIn('ɪ(t)', annotate('It slightly changes.', 'ɪt ˈslaɪtli ˈtʃeɪndʒəz.'))

    def test_incomplete_fragments_keep_continuation_tone(self):
        self.assertTrue(annotate('I was talking with', 'aɪ wəz tɔkɪŋ wɪθ').endswith('↗'))
        self.assertTrue(annotate('I stopped because', 'aɪ stɑpt bɪkɔz').endswith('↗'))
        self.assertTrue(annotate('I read a page.', 'aɪ rɛd ə peɪdʒ.').endswith('↘'))


if __name__ == "__main__":
    unittest.main()
