"""Stdlib unit tests for the stream-clipping pipeline.

Run with Python and FFmpeg: python3 -B tests/test_pipeline.py
"""
from __future__ import annotations

import json
import sys
import unittest
import subprocess
from concurrent.futures import Future
from contextlib import nullcontext
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import chat_spikes  # noqa: E402
import live_monitor  # noqa: E402
from live_monitor import hype_zscore  # noqa: E402
from clips_ground_truth import parse_duration  # noqa: E402


def synth_comments(duration_s=900, spike_at=600, spike_rate=40, base_rate=2):
    """Normalized comments (as load_comments emits) with one obvious spike."""
    comments = []
    t = 0.0
    while t < duration_s:
        rate = spike_rate if abs(t - spike_at) < 5 else base_rate
        for _ in range(rate):
            comments.append({"t": t, "body": "KEKW", "emotes": 1})
        t += 1.0
    return comments


class TestChatSpikes(unittest.TestCase):
    def test_finds_known_spike(self):
        comments = synth_comments(spike_at=600)
        msgs, emotes = chat_spikes.per_second_series(comments)
        smooth = chat_spikes.moving_average([float(m) for m in msgs], 10)
        z = chat_spikes.z_scores(smooth, 300)
        windows = chat_spikes.pick_windows(
            z, z, smooth, smooth, threshold=3.0, top=3, window=75, pad=15,
            min_gap=60)
        self.assertTrue(windows, "no windows found on an obvious spike")
        peak = min(windows, key=lambda w: abs(w["peak"] - 600))
        self.assertLess(abs(peak["peak"] - 600), 30)

    def test_flat_chat_finds_nothing(self):
        comments = synth_comments(spike_rate=2)  # no spike at all
        msgs, emotes = chat_spikes.per_second_series(comments)
        smooth = chat_spikes.moving_average([float(m) for m in msgs], 10)
        z = chat_spikes.z_scores(smooth, 300)
        windows = chat_spikes.pick_windows(
            z, z, smooth, smooth, threshold=3.0, top=3, window=75, pad=15,
            min_gap=60)
        self.assertEqual(windows, [])

    def test_gzip_and_plain_load(self):
        import gzip
        # raw TwitchDownloader schema — load_comments normalizes it
        raw = [{"content_offset_seconds": float(t),
                "message": {"body": "KEKW", "emoticons": [{}]}}
               for t in range(100)]
        payload = json.dumps({"comments": raw}).encode()
        plain = Path("/tmp/test_chat_plain.json")
        gzipped = Path("/tmp/test_chat.json.gz")
        plain.write_bytes(payload)
        with gzip.open(gzipped, "wb") as fh:
            fh.write(payload)
        self.assertEqual(len(chat_spikes.load_comments(str(plain))), len(raw))
        self.assertEqual(len(chat_spikes.load_comments(str(gzipped))), len(raw))
        plain.unlink(); gzipped.unlink()


class TestHypeScore(unittest.TestCase):
    def test_spike_over_noisy_baseline(self):
        baseline = [5.0 + (i % 3) for i in range(240)]  # small natural noise
        self.assertGreater(hype_zscore(60.0, baseline), 10)

    def test_perfectly_flat_baseline_guarded(self):
        # zero variance is undefined z — the guard returns 0.0 by design
        self.assertEqual(hype_zscore(60.0, [5.0] * 240), 0.0)

    def test_quiet_over_quiet(self):
        baseline = [5.0 + (i % 3) for i in range(240)]
        self.assertLess(hype_zscore(5.0, baseline), 1)

    def test_short_baseline_is_zero(self):
        self.assertEqual(hype_zscore(100.0, [1.0] * 10), 0.0)


class TestChatReactions(unittest.TestCase):
    def test_parses_tagged_chat_and_matches_standalone_reaction(self):
        line = "@id=abc :Viewer!viewer@host PRIVMSG #channel :No way!"
        self.assertEqual(live_monitor.chat_message(line), ("viewer", "No way!"))
        self.assertIsNone(live_monitor.chat_message(":server NOTICE #channel :PRIVMSG here"))
        self.assertTrue(live_monitor.REACTION.search("No way!"))
        self.assertTrue(live_monitor.REACTION.search("KEKW KEKW"))
        self.assertFalse(live_monitor.REACTION.search("there is no way there is more"))

    def test_requires_multiple_reactors_and_compares_with_recent_activity(self):
        burst = live_monitor.reaction_burst
        self.assertFalse(burst([(t, "same") for t in range(95, 101)], 100))
        recent = [(95, "one"), (98, "two"), (100, "three")]
        self.assertTrue(burst(recent, 100))
        self.assertFalse(burst(recent, 101))
        baseline = [(70 + i, f"viewer{i}") for i in range(7)]
        self.assertFalse(burst(baseline + recent, 100))

    def test_reaction_trigger_is_optional_and_observes_clip_cooldown(self):
        for enabled in (False, True):
            events = []
            counter = live_monitor.IrcCounter("test", 4.0, lambda *event: events.append(event),
                                             reaction_chat=enabled)
            counter.per_sec.extend((i, 1) for i in range(41, 100))
            counter.reactions.extend([(98, "a"), (99, "b"), (100, "c")])
            with patch.object(live_monitor, "utcnow", side_effect=[100, 101]):
                counter._tick()
                counter._tick()
            self.assertEqual(len(events), int(enabled))
            if enabled:
                self.assertTrue(events[0][-1])

    def test_reaction_onset_uses_distinct_chatters_and_ignores_isolated_noise(self):
        reactions = [(95.1, "early"), (95.2, "early"), (96.5, "a"), (96.9, "b"),
                     (97.1, "a"), (97.3, "c"), (99.0, "late")]
        active = {sender for _, sender in reactions}
        self.assertEqual(live_monitor.reaction_onset(reactions, active, 100), 96.5)
        self.assertIsNone(live_monitor.reaction_onset(reactions, active - {"c"}, 100))


class TestIrcReconnect(unittest.TestCase):
    """A flapping IRC connection must reconnect, not die silently."""

    def test_reconnects_after_losses(self):
        class FakeSock:
            def settimeout(self, t):
                pass

            def recv(self, n):
                raise OSError("connection reset")

            def close(self):
                pass

        class Flappy(live_monitor.IrcCounter):
            connects = 0

            def _connect(self):
                Flappy.connects += 1
                if Flappy.connects >= 3:
                    self._stop_event.set()  # enough proof, end the loop
                self.sock = FakeSock()

        irc = Flappy("testchannel", hype_z=999, on_hype=lambda *a: None)
        with patch.object(irc._stop_event, "wait", return_value=False) as wait:
            irc.start()
            irc.join(timeout=5)
        self.assertFalse(irc.is_alive())
        self.assertEqual([call.args[0] for call in wait.call_args_list[:2]], [2, 4])
        self.assertGreaterEqual(Flappy.connects, 3)


class TestLiveClips(unittest.TestCase):
    def test_dry_run_writes_chat_samples_only_when_enabled(self):
        for save_chat_samples in (False, True):
            with self.subTest(save_chat_samples=save_chat_samples), TemporaryDirectory() as directory:
                args = SimpleNamespace(
                    staging=directory, channels="fixture", env="unused", dry_run=True,
                    hype_z=4.0, save_chat_samples=save_chat_samples,
                    reaction_chat=False, rank_moments=False,
                )
                credentials = {"TWITCH_CLIENT_ID": "fixture", "TWITCH_CLIENT_SECRET": "fixture"}
                with (patch.object(live_monitor, "load_env", return_value=credentials),
                      patch.object(live_monitor, "HelixClient") as helix,
                      patch.object(live_monitor, "IrcCounter") as irc,
                      patch.object(live_monitor.signal, "signal"),
                      patch.object(live_monitor.Monitor, "log")):
                    helix.return_value.get.return_value = {"data": [{"id": "42"}]}
                    monitor = live_monitor.Monitor(args)
                    monitor.start_live("fixture", {"started_at": "2026-01-01T00:00:00Z", "id": "1"})
                    on_hype = irc.call_args.args[2]
                    on_hype("fixture", 105, 5.0, 12, [(104, "sample chat")], None)
                    monitor.process_hype()
                event = json.loads((Path(directory) / "hype.jsonl").read_text())
                self.assertEqual("chat_sample" in event, save_chat_samples)
                if save_chat_samples:
                    self.assertEqual(event["chat_sample"], ["sample chat"])

    def test_smoke_checks_only_the_requested_channel(self):
        args = SimpleNamespace(channels="Fixture", env="unused", smoke=1)
        credentials = {"TWITCH_CLIENT_ID": "fixture", "TWITCH_CLIENT_SECRET": "fixture"}
        with (patch.object(live_monitor, "load_env", return_value=credentials),
              patch.object(live_monitor, "HelixClient") as helix,
              patch.object(live_monitor, "IrcCounter") as irc,
              patch.object(live_monitor.time, "sleep")):
            helix.return_value.get.return_value = {"data": [{"user_login": "fixture"}]}
            irc.return_value.count_total = 1
            irc.return_value.error = None
            self.assertEqual(live_monitor.smoke(args), 0)
        helix.return_value.get.assert_called_once_with("streams", {"user_login": "fixture"})
        self.assertEqual(irc.call_args.args[0], "fixture")

    def test_quality_cut_looks_earlier_for_setup_and_preserves_payoff_window(self):
        monitor = live_monitor.Monitor.__new__(live_monitor.Monitor)
        monitor.args = SimpleNamespace(ffmpeg="ffmpeg")
        monitor.record_quality_decision = Mock()
        clock = Mock()
        clock.window.return_value = nullcontext((Path("window.ffconcat"), 110))
        event = {"ranking": {"chat_anchor": 145, "chat_onset_anchor": 142}}
        with patch.object(live_monitor, "suggest_pause_bounds", return_value=None) as pauses:
            monitor.render_quality_candidate("fixture", {"clock": clock, "stream_id": "id"}, event)
        clock.window.assert_called_once_with(116, 162)
        pauses.assert_called_once_with("ffmpeg", Path("window.ffconcat"), start=14, end=44)
        self.assertEqual(monitor.record_quality_decision.call_args.kwargs["cut_cue"], 142)

    def test_quality_cut_requires_source_and_avoids_duplicate_exports(self):
        with TemporaryDirectory() as directory:
            monitor = live_monitor.Monitor.__new__(live_monitor.Monitor)
            monitor.args = SimpleNamespace(ffmpeg="ffmpeg", brb_templates={})
            monitor.staging = Path(directory)
            monitor.log = Mock()
            monitor.quality_exports = {}
            monitor._cut_clip = Mock(return_value=True)
            clock = Mock()
            state = {"clock": clock, "stream_id": "fixture-broadcast"}
            event = {
                "timestamp": 150, "utc": "1970-01-01T00:02:30Z",
                "ranking": {"chat_anchor": 145, "score": 15},
            }
            clock.window.return_value = nullcontext()
            monitor.render_quality_candidate("fixture", state, event)
            monitor._cut_clip.assert_not_called()

            source = Path("window.ffconcat")
            clock.window.return_value = nullcontext((source, 115))
            with patch.object(live_monitor, "suggest_pause_bounds", return_value=(10, 42)), \
                    patch.object(live_monitor, "inspect_source_window",
                                 return_value={"reject": False, "reasons": []}), \
                    patch.object(live_monitor, "utcnow", return_value=160):
                monitor.render_quality_candidate("fixture", state, event)
                clock.window.return_value = nullcontext((source, 115))
                monitor.render_quality_candidate("fixture", state, event)

            clock.window.assert_called_with(119, 162)
            monitor._cut_clip.assert_called_once()
            channel, path, selected, start, duration = monitor._cut_clip.call_args.args
            self.assertEqual((channel, path, start, duration), ("fixture", source, 10, 32))
            self.assertEqual(selected["quality"]["source_start"], 125)
            self.assertEqual(selected["quality"]["source_end"], 157)
            decisions = [
                json.loads(line)["decision"]
                for line in (monitor.staging / "quality.jsonl").read_text().splitlines()
            ]
            self.assertEqual(decisions, ["source coverage unavailable", "exported", "overlapping export"])

    def test_viewer_clip_requires_matching_archive_and_observed_footage(self):
        monitor = live_monitor.Monitor.__new__(live_monitor.Monitor)
        monitor.ids = {"fixture": "broadcaster"}
        monitor.hype_events = live_monitor.SimpleQueue()
        monitor.helix = Mock()
        state = {
            "rec_start": 100, "stream_id": "stream", "rec_path": Path("source.ts"),
            "archive": {"id": "archive", "created_at": "1970-01-01T00:00:00Z"},
            "viewer_events": [], "category": "Music",
        }
        clip = {
            "id": "viewer", "video_id": "archive", "vod_offset": 120, "duration": 30,
            "creator_id": "another-account", "created_at": "1970-01-01T00:02:35Z",
            "url": "https://clips.twitch.tv/viewer", "title": "test",
        }
        monitor.helix.get.return_value = {"data": [
            {**clip, "id": "old", "video_id": "old_archive"},
            {**clip, "id": "early", "vod_offset": 90},
            {**clip, "id": "unresolved", "vod_offset": None},
            {**clip, "id": "self", "creator_id": "broadcaster"},
            clip, {**clip, "id": "same-beat", "vod_offset": 123},
        ]}
        with patch.object(live_monitor, "utcnow", return_value=180):
            monitor.poll_viewer_clips("fixture", state)
            monitor.poll_viewer_clips("fixture", state)
        _, _, _, event = monitor.hype_events.get_nowait()
        self.assertEqual((event["trigger"], event["timestamp"]), ("viewer_clip", 150))
        monitor.args = SimpleNamespace(chat_context=(40, 58))
        self.assertEqual(monitor.clip_bounds(event, 100), (20, 30))
        self.assertTrue(monitor.hype_events.empty())

    def test_speech_reaction_waits_for_payoff_footage_across_categories(self):
        monitor = live_monitor.Monitor.__new__(live_monitor.Monitor)
        monitor.args = SimpleNamespace(clip_pre=10, clip_window=45, chat_context=None,
                                       capture_grace=15)
        monitor.hype_log = Mock()
        monitor.hype_log.open.side_effect = OSError
        monitor.log = Mock()
        monitor.disk_ok = Mock(return_value=True)
        monitor._cut_clip = Mock()
        monitor.speech_pool = Mock()
        monitor.speech_cut = None
        monitor.hype_events = live_monitor.SimpleQueue()
        monitor.pending_cuts = []
        monitor.hype_events.put(("fixture", Path("source.ts"), 100, {
            "timestamp": 150, "category": "Music", "trigger": "chat_reaction",
        }))
        with patch.object(live_monitor, "utcnow", return_value=210):
            monitor.process_hype()
        monitor.speech_pool.submit.assert_not_called()
        with patch.object(live_monitor, "utcnow", return_value=211):
            monitor.process_hype()
        _, channel, _, event, start, duration = monitor.speech_pool.submit.call_args.args
        self.assertEqual((channel, start, duration, event["speech_reaction"]),
                         ("fixture", 22, 74, 28))

    def test_just_chatting_gets_context_without_shifting_the_end_at_startup(self):
        monitor = live_monitor.Monitor.__new__(live_monitor.Monitor)
        monitor.args = SimpleNamespace(clip_pre=10, clip_window=45, chat_context=(40, 58))

        event = {"category": "Just Chatting", "timestamp": 200}
        self.assertEqual(monitor.clip_bounds(event, 100), (60, 98))
        self.assertEqual(monitor.clip_bounds(event, 195), (0, 63))
        event["category"] = "Counter-Strike 2"
        self.assertEqual(monitor.clip_bounds(event, 100), (90, 45))
        self.assertEqual(monitor.clip_bounds(event, 195), (0, 45))
        event["anchor"] = 195
        self.assertEqual(monitor.clip_bounds(event, 100), (85, 45))

    def test_hud_clip_uses_recording_time_and_keeps_pending_chat(self):
        monitor = live_monitor.Monitor.__new__(live_monitor.Monitor)
        monitor.args = SimpleNamespace(capture_grace=15)
        monitor.hud_pool = object()
        monitor.log = lambda message: None
        recording = Path("sample.ts")
        state = {
            "rec_path": recording, "rec_start": 1000, "category": "Fortnite",
            "hud_cursor": 48, "hud": live_monitor.HudEvents(), "hud_generation": 0,
            "hud_retry": False, "hud_retry_at": 0,
        }
        monitor.live = {"fixture": state}
        future = Future()
        future.set_result([(40.5, "elimination"), (41.0, "elimination")])
        monitor.hud_scan = (future, "fixture", state, 0)
        previous = (1080, "fixture", recording, {"trigger": "chat"}, 18, 45)
        monitor.pending_cuts = [previous]
        with patch.object(live_monitor, "utcnow", return_value=1070):
            monitor.process_hud()

        self.assertEqual(monitor.pending_cuts[0], previous)
        ready, channel, source, event, start, duration = monitor.pending_cuts[1]
        self.assertEqual((channel, source, start, duration), ("fixture", recording, 28.5, 21))
        self.assertEqual((ready, event["timestamp"], event["trigger"]), (1064.5, 1040.5, "elimination"))

    def test_fresh_staging_and_completed_clip(self):
        with TemporaryDirectory() as directory:
            staging = Path(directory) / "fresh"
            args = SimpleNamespace(
                staging=staging, channels="fixture", env="unused", min_free_gb=0,
                ffmpeg="ffmpeg", ffprobe="ffprobe", clip_pre=2, clip_window=6,
                capture_grace=3, chat_context=None,
            )
            with patch.object(live_monitor, "load_env", return_value={
                "TWITCH_CLIENT_ID": "fixture", "TWITCH_CLIENT_SECRET": "fixture",
            }), patch.object(live_monitor, "HelixClient") as helix, patch.object(
                live_monitor.signal, "signal",
            ):
                helix.return_value.get.return_value = {"data": [{"id": "42"}]}
                monitor = live_monitor.Monitor(args)
            self.assertTrue(monitor.disk_ok())
            source = staging / "source.ts"
            subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi",
                 "-i", "color=s=160x90:r=25", "-f", "lavfi", "-i", "sine=frequency=440",
                 "-t", "14", "-c:v", "mpeg2video", "-c:a", "aac",
                 "-f", "mpegts", str(source)],
                check=True, timeout=20,
            )
            monitor.hype_events.put(("fixture", source, 100, {
                "timestamp": 105, "category": "Fixture", "z": 6.1, "rate_per_s": 12,
            }))
            with patch.object(live_monitor, "utcnow", return_value=111):
                monitor.process_hype()
            self.assertFalse((staging / "clips").exists())

            with patch.object(live_monitor, "utcnow", return_value=112):
                monitor.process_hype()
            clip_dir = staging / "clips"
            clip = clip_dir / "fixture-105.000000.mp4"
            sidecar = clip_dir / "fixture-105.000000_insights.json"
            self.assertGreater(clip.stat().st_size, 0)
            metadata = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(metadata["start"], 3)
            self.assertAlmostEqual(metadata["duration"], 6, delta=1)
            self.assertEqual(metadata["source_video"], "https://www.twitch.tv/fixture")
            streams = json.loads(subprocess.check_output(
                ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                 "-of", "json", str(clip)], timeout=10,
            ))
            self.assertEqual({stream["codec_type"] for stream in streams["streams"]}, {"audio", "video"})
            self.assertFalse(monitor.pending_cuts)

            monitor._cut_clip("fixture", staging / "missing.ts", {
                "timestamp": 115, "category": "Fixture", "z": 6.1, "rate_per_s": 12,
            }, 0, 6)
            self.assertEqual(list(clip_dir.glob("*_insights.json")), [sidecar])

            real_run = subprocess.run

            def fail_decode(command, **kwargs):
                if "-xerror" in command:
                    raise subprocess.CalledProcessError(1, command)
                return real_run(command, **kwargs)

            with patch.object(live_monitor.subprocess, "run", side_effect=fail_decode):
                monitor._cut_clip("fixture", source, {
                    "timestamp": 120, "category": "Fixture", "z": 6.1, "rate_per_s": 12,
                }, 0, 6)
            self.assertFalse((clip_dir / "fixture-120.000000.mp4").exists())
            self.assertEqual(list(clip_dir.glob("*_insights.json")), [sidecar])


class TestParseDuration(unittest.TestCase):
    def test_clock_format(self):
        self.assertEqual(parse_duration("8:30:12"), 8 * 3600 + 30 * 60 + 12)

    def test_relative_format(self):
        self.assertEqual(parse_duration("2h15m30s"), 2 * 3600 + 15 * 60 + 30)

    def test_empty(self):
        self.assertEqual(parse_duration(""), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
