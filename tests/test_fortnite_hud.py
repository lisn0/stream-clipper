"""Stdlib-only tests for the Fortnite HUD scanner."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import fortnite_hud  # noqa: E402


def png_frame() -> bytes:
    # Minimal chunk sequence; the parser checks framing, not PNG pixel decoding.
    return (fortnite_hud._PNG_SIGNATURE
            + b"\x00\x00\x00\x00IHDR\x00\x00\x00\x00"
            + b"\x00\x00\x00\x00IDAT\x00\x00\x00\x00"
            + b"\x00\x00\x00\x00IEND\x00\x00\x00\x00")


class TestHudScan(unittest.TestCase):
    def test_ocr_samples_map_to_recording_offsets_and_event_types(self):
        frames = [png_frame() for _ in range(4)]
        ocr = iter([b"+300 ENEMY ELIMINATED", b"KNOCKED DOWN",
                    b"VICTORY ROYALE", b"some unrelated HUD"])

        def run(command, **kwargs):
            if command[0] == "ffmpeg":
                self.assertIn("fps=2", command[command.index("-filter_complex") + 1])
                self.assertIn("vstack=inputs=3", command[command.index("-filter_complex") + 1])
                return SimpleNamespace(stdout=b"".join(frames))
            return SimpleNamespace(stdout=next(ocr))

        with patch.object(fortnite_hud.subprocess, "run", side_effect=run) as mocked:
            found = fortnite_hud.scan("ffmpeg", "tesseract", "recording.ts", 12, 2)
        self.assertEqual(found, [(12.0, "elimination"), (12.5, "knock"), (13.0, "win")])
        self.assertEqual(mocked.call_count, 5)

    def test_failed_or_short_decode_raises(self):
        with patch.object(fortnite_hud.subprocess, "run",
                          side_effect=subprocess.CalledProcessError(1, "ffmpeg")):
            with self.assertRaises(subprocess.CalledProcessError):
                fortnite_hud.scan("ffmpeg", "tesseract", "recording.ts", 0, 2)
        with patch.object(fortnite_hud.subprocess, "run",
                          return_value=SimpleNamespace(stdout=png_frame())):
            with self.assertRaisesRegex(ValueError, "incomplete HUD scan"):
                fortnite_hud.scan("ffmpeg", "tesseract", "recording.ts", 0, 2)

    def test_scan_range_is_bounded(self):
        with self.assertRaises(ValueError):
            fortnite_hud.scan("ffmpeg", "tesseract", "recording.ts", 0, 91)


class TestHudEvents(unittest.TestCase):
    def test_requires_two_reads_dedupes_and_rearms_after_disappearance(self):
        events = fortnite_hud.HudEvents()
        self.assertEqual(events.accept([(1.0, "elimination")]), [])
        self.assertEqual(events.accept([(1.5, "elimination"), (2.0, "elimination")]),
                         [(1.0, "elimination")])
        self.assertEqual(events.accept([(2.5, "elimination")]), [])
        self.assertEqual(events.accept([(2.5, "elimination")]), [])
        # A gap longer than the disappearance threshold rearms the same banner type.
        self.assertEqual(events.accept([(5.0, "elimination"), (5.5, "elimination")]),
                         [(5.0, "elimination")])

    def test_event_types_debounce_independently_and_reset(self):
        events = fortnite_hud.HudEvents()
        self.assertEqual(events.accept([(0, "knock"), (0, "win")]), [])
        self.assertEqual(events.accept([(0.5, "knock"), (0.5, "win")]),
                         [(0, "knock"), (0, "win")])
        self.assertEqual(events.accept([(1.0, "unknown")]), [])
        events.reset()
        self.assertEqual(events.accept([(2, "win")]), [])
        self.assertEqual(events.accept([(2.5, "win")]), [(2, "win")])

    def test_reads_outside_confirmation_window_do_not_confirm(self):
        events = fortnite_hud.HudEvents(confirm_window=3)
        self.assertEqual(events.accept([(1, "knock")]), [])
        self.assertEqual(events.accept([(5, "knock")]), [])


if __name__ == "__main__":
    unittest.main()
