import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from twitch_clock import TimestampedRecorder, parse_playlist


class TestPlaylistParsing(unittest.TestCase):
    def test_pdt_carries_across_segments_and_media_sequence_increments(self):
        playlist = """#EXTM3U
#EXT-X-MEDIA-SEQUENCE:41
#EXT-X-PROGRAM-DATE-TIME:2026-11-15T10:00:00.000Z
#EXTINF:2.0,
seg41.ts
#EXTINF:2.5,
seg42.ts
"""
        segments = parse_playlist(playlist)
        self.assertEqual([s.sequence for s in segments], [41, 42])
        self.assertEqual([s.duration for s in segments], [2.0, 2.5])
        self.assertEqual(segments[1].pdt_utc - segments[0].pdt_utc, 2.0)

    def test_discontinuity_breaks_pdt_inheritance(self):
        playlist = """#EXTM3U
#EXT-X-PROGRAM-DATE-TIME:2026-11-15T10:00:00Z
#EXTINF:2,
a.ts
#EXT-X-DISCONTINUITY
#EXTINF:2,
b.ts
#EXT-X-PROGRAM-DATE-TIME:2026-11-15T10:00:10Z
#EXTINF:2,
c.ts
"""
        a, b, c = parse_playlist(playlist)
        self.assertFalse(a.discontinuity)
        self.assertTrue(b.discontinuity)
        self.assertIsNone(b.pdt_utc)
        self.assertEqual(c.pdt_utc, a.pdt_utc + 10)

    def test_rejects_timezone_less_or_invalid_duration(self):
        with self.assertRaises(ValueError):
            parse_playlist("#EXT-X-PROGRAM-DATE-TIME:2026-11-15T10:00:00\n")
        with self.assertRaises(ValueError):
            parse_playlist("#EXTINF:nan,\na.ts\n")


class TestTimestampedRecorderWindows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.recorder = TimestampedRecorder("tester", self.directory, "unused")
        base = 1_800_000_000.0
        for i in range(5):
            path = self.directory / f"seg{i}.ts"
            path.write_bytes(b"segment")
            self.recorder._owned[(i,)] = (path, base + i * 2, 2.0, False)
        self.base = base

    def tearDown(self):
        self.tmp.cleanup()

    def test_window_yields_auto_probe_manifest_and_origin(self):
        with self.recorder.window(self.base + 1, self.base + 7) as result:
            self.assertIsNotNone(result)
            manifest, origin = result
            self.assertEqual(origin, self.base)
            content = manifest.read_text()
            self.assertTrue(content.startswith("ffconcat version 1.0\n"))
            self.assertIn("file 'seg0.ts'\nduration 2.000000\n", content)
            self.assertIn("file 'seg3.ts'", content)
            self.assertEqual(self.recorder._protected, {
                self.directory / "seg0.ts", self.directory / "seg1.ts",
                self.directory / "seg2.ts", self.directory / "seg3.ts",
            })
        self.assertFalse(manifest.exists())
        self.assertFalse(self.recorder._protected)

    def test_gaps_and_discontinuities_return_no_window(self):
        path, pdt, duration, _ = self.recorder._owned[(2,)]
        self.recorder._owned[(2,)] = (path, pdt + 1, duration, False)
        with self.recorder.window(self.base + 1, self.base + 7) as result:
            self.assertIsNone(result)
        self.recorder._owned[(2,)] = (path, pdt, duration, True)
        with self.recorder.window(self.base + 1, self.base + 7) as result:
            self.assertIsNone(result)

    def test_retention_removes_only_old_unprotected_owned_segments(self):
        owned = list(self.recorder._owned.values())
        old_path = owned[0][0]
        protected_path = owned[1][0]
        self.recorder._protected.add(protected_path)
        self.recorder._expire(now_utc=self.base + self.recorder.RETENTION_SECONDS + 3)
        self.assertFalse(old_path.exists())
        self.assertTrue(protected_path.exists())
        self.assertTrue(owned[-1][0].exists())


if __name__ == "__main__":
    unittest.main()
