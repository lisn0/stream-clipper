import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
from speech_excerpt import speech_bounds, timed_speech_edges


class TestSpeechBounds(unittest.TestCase):
    def test_word_timing_uses_internal_pauses_and_keeps_reaction_context(self):
        words = [SimpleNamespace(start=start, end=end, word=text) for start, end, text in (
            (1, 5, "earlier"), (6, 9, "setup"), (9.1, 19.9, "exchange"),
            (20, 22.9, "payoff!"), (23.2, 28, "later"),
        )]
        edges = timed_speech_edges(words, start=6, end=23, latest_start=9, earliest_end=21)
        self.assertAlmostEqual(edges[0], 5.85)
        self.assertAlmostEqual(edges[1], 23.05)
        self.assertIsNone(timed_speech_edges(words[:-1], start=6, end=23,
                                             latest_start=9, earliest_end=21))

    def test_viewer_clip_ends_at_first_eligible_pause(self):
        speech = [(0, 5), (10, 15), (16, 30), (31, 55)]
        self.assertEqual(speech_bounds(speech, 35, 60, before=36,
                                      viewer_duration=30), (0, 15.25))

    def test_viewer_clip_with_continuous_speech_limits_tail(self):
        self.assertEqual(speech_bounds([(0, 60)], 32, 60, before=36,
                                      viewer_duration=30), (0, 35))

    def test_viewer_clip_uses_sentence_start_without_moving_payoff_end(self):
        speech = [(0, 49), (50, 60)]
        self.assertEqual(speech_bounds(speech, 66, 70, before=66,
                                      viewer_duration=60, sentence_starts=[7, 33, 39, 43]),
                         (33, 49.25))

    def test_moves_cuts_into_pauses_around_the_exchange(self):
        speech = [(0, 30), (35, 42), (42.2, 49), (50, 68), (71, 80)]
        self.assertEqual(speech_bounds(speech, 52, 90), (34.75, 68.25))

    def test_falls_back_to_context_window_in_continuous_speech(self):
        self.assertEqual(speech_bounds([(0, 90)], 52, 90), (35, 70))

    def test_limits_window_to_available_footage(self):
        self.assertEqual(speech_bounds([], 3, 10), (0, 10))
        with self.assertRaises(ValueError):
            speech_bounds([], 12, 10)
