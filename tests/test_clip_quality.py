import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import clip_quality


class TestSourceWindowInspection(unittest.TestCase):
    def test_rejects_only_sustained_twitch_slate(self):
        frames = [b"slate"] * 8 + [b"talk"] * 2
        with patch.object(clip_quality, "_sampled_frames", return_value=frames):
            with patch.object(clip_quality, "is_slate", side_effect=lambda frame: frame == b"slate"):
                self.assertEqual(
                    clip_quality.inspect_source_window("ffmpeg", "source.mp4", start=22, duration=10),
                    {"reject": True, "reasons": ["sustained_twitch_slate"]},
                )

    def test_one_ad_slate_frame_does_not_reject_a_clip(self):
        frames = [b"slate"] + [b"talk"] * 9
        with patch.object(clip_quality, "_sampled_frames", return_value=frames):
            with patch.object(clip_quality, "is_slate", side_effect=lambda frame: frame == b"slate"):
                self.assertFalse(clip_quality.inspect_source_window("ffmpeg", "source.mp4")["reject"])

    def test_optional_brb_template_requires_sustained_match(self):
        frames = [b"card"] * 9 + [b"talk"]
        with patch.object(clip_quality, "_sampled_frames", return_value=frames):
            with patch.object(clip_quality, "_load_template", return_value=b"ref"):
                with patch.object(clip_quality, "_matches_template", side_effect=lambda frame, _: frame == b"card"):
                    result = clip_quality.inspect_source_window("ffmpeg", "source.mp4", brb_template="brb.png")
        self.assertEqual(result["reasons"], ["sustained_broadcaster_brb_template"])

    def test_optional_brb_template_catches_majority_in_a_mixed_event_cut(self):
        frames = [b"card"] * 6 + [b"event"] * 4
        with patch.object(clip_quality, "_sampled_frames", return_value=frames):
            with patch.object(clip_quality, "_load_template", return_value=b"ref"):
                with patch.object(clip_quality, "_matches_template", side_effect=lambda frame, _: frame == b"card"):
                    result = clip_quality.inspect_source_window("ffmpeg", "source.mp4", brb_template="brb.png")
        self.assertEqual(result["reasons"], ["sustained_broadcaster_brb_template"])

    def test_brb_template_rejects_sustained_break_inside_longer_footage(self):
        frames = [b"event"] * 8 + [b"card"] * 13 + [b"event"] * 9
        with patch.object(clip_quality, "_sampled_frames", return_value=frames), \
                patch.object(clip_quality, "_load_template", return_value=b"ref"), \
                patch.object(clip_quality, "_matches_template", side_effect=lambda frame, _: frame == b"card"):
            result = clip_quality.inspect_source_window("ffmpeg", "source.mp4", brb_template="brb.png")
        self.assertEqual(result["reasons"], ["sustained_broadcaster_brb_template"])

    def test_intermittent_brb_template_matches_below_threshold_do_not_reject(self):
        frames = [b"card", b"event"] * 5
        with patch.object(clip_quality, "_sampled_frames", return_value=frames):
            with patch.object(clip_quality, "_load_template", return_value=b"ref"):
                with patch.object(clip_quality, "_matches_template", side_effect=lambda frame, _: frame == b"card"):
                    result = clip_quality.inspect_source_window("ffmpeg", "source.mp4", brb_template="brb.png")
        self.assertFalse(result["reject"])

    def test_window_shorter_than_requested_is_rejected(self):
        with patch.object(clip_quality, "_sampled_frames", return_value=[b"frame"] * 5):
            result = clip_quality.inspect_source_window("ffmpeg", "source.mp4", duration=10)
        self.assertEqual(result, {"reject": True, "reasons": ["insufficient_video_footage"]})


class TestPauseSuggestions(unittest.TestCase):
    def test_parses_silence_intervals(self):
        log = "silence_start: 1.250\nsilence_end: 1.800 | silence_duration: 0.550\n"
        self.assertEqual(clip_quality._silence_intervals(log), [(1.25, 1.8)])

    def test_suggests_trimmed_boundaries_when_nearby_pauses_allow_it(self):
        fake = type("FFmpegResult", (), {"stderr": (
            "silence_start: 9.0\nsilence_end: 9.5\n"
            "silence_start: 35.0\nsilence_end: 35.5\n"
        )})
        with patch.object(clip_quality.subprocess, "run", return_value=fake):
            bounds = clip_quality.suggest_pause_bounds("ffmpeg", "source.mp4", start=100, end=130)
        self.assertEqual(bounds, (101.0, 127.5))

    def test_can_extend_both_edges_to_recover_context_at_pauses(self):
        fake = type("FFmpegResult", (), {"stderr": (
            "silence_start: 5.0\nsilence_end: 5.5\n"
            "silence_start: 40.0\nsilence_end: 40.5\n"
        )})
        with patch.object(clip_quality.subprocess, "run", return_value=fake):
            bounds = clip_quality.suggest_pause_bounds("ffmpeg", "source.mp4", start=100, end=130)
        self.assertEqual(bounds, (97.5, 132.0))

    def test_does_not_reuse_one_silence_gap_for_both_edges(self):
        fake = type("FFmpegResult", (), {"stderr": "silence_start: 9.0\nsilence_end: 9.5\n"})
        with patch.object(clip_quality.subprocess, "run", return_value=fake):
            self.assertIsNone(clip_quality.suggest_pause_bounds("ffmpeg", "source.mp4", start=100, end=110))

    def test_missing_pause_at_either_edge_means_no_guess(self):
        fake = type("FFmpegResult", (), {"stderr": "silence_start: 9.0\nsilence_end: 9.5\n"})
        with patch.object(clip_quality.subprocess, "run", return_value=fake):
            self.assertIsNone(clip_quality.suggest_pause_bounds("ffmpeg", "source.mp4", start=100, end=130))

    def test_missing_audio_returns_no_boundary_hint(self):
        failure = clip_quality.subprocess.CalledProcessError(
            1, "ffmpeg", stderr="Output file #0 does not contain any stream"
        )
        with patch.object(clip_quality.subprocess, "run", side_effect=failure):
            self.assertIsNone(clip_quality.suggest_pause_bounds("ffmpeg", "silent.mp4", start=100, end=130))

    def test_rejects_invalid_or_too_short_windows_without_running_ffmpeg(self):
        with patch.object(clip_quality.subprocess, "run") as run:
            self.assertIsNone(clip_quality.suggest_pause_bounds("ffmpeg", "source.mp4", start=0, end=7.9))
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
