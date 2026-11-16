#!/usr/bin/env python3
"""Live monitor: watch Twitch channels, record when live, cut clips on chat hype.

Design (stdlib only — no pip dependencies):
  - Live status via Helix polling (`GET /helix/streams`), 1 point/call/minute
    for a handful of channels — EventSub WebSocket can replace this later.
  - Live chat via anonymous IRC over TLS (irc.chat.twitch.tv:6697, NICK
    justinfan<n>) — no account, no OAuth. Per-second message counts are kept
    in memory. Raw chat samples are written only when explicitly enabled.
  - Recording via yt-dlp `--hls-use-mpegts` into local staging.
  - Chat hype queues a clip around the spike. FFmpeg cuts it once the
    recording has had time to capture the post-spike footage.
  - Completed clips receive an insights sidecar for PostFling intake.
  - Helix 401 (expired app token) triggers a transparent re-auth.

Credentials come from an env file (TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET)
— never from argv. Smoke mode tests chat collection on one selected live channel.

Usage:
  python3 -B pipeline/live_monitor.py --channels your_channel --env twitch.env
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import signal
import socket
import ssl
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, SimpleQueue

from clip_quality import inspect_source_window, suggest_pause_bounds
from fortnite_hud import HudEvents, scan
from live_drafts import LiveDrafts
from moment_ranking import CHAT_WINDOW_SECONDS, ChatShare, MomentRanking, VIEWER_LOOKBACK
from speech_excerpt import (load_speech_model, render_excerpt, speech_bounds,
                            suggest_transcript_bounds, transcribe)
from twitch_clock import TimestampedRecorder
from twitch_slate import contains_slate

IRC_HOST, IRC_PORT = "irc.chat.twitch.tv", 6697
HELIX = "https://api.twitch.tv/helix"


def load_env(path: str) -> dict:
    out = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def post_form(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode("ascii")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def utcnow() -> float:
    return datetime.now(timezone.utc).timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hype_zscore(rate: float, baseline: list[float]) -> float:
    """Pure function so the hype rule is unit-testable without sockets."""
    if len(baseline) < 30:
        return 0.0
    mean = statistics.mean(baseline)
    std = statistics.pstdev(baseline)
    return (rate - mean) / std if std > 1e-9 else 0.0


class HelixClient:
    """App-token Helix client with transparent re-auth on 401."""

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = self._auth()

    def _auth(self) -> str:
        return post_form("https://id.twitch.tv/oauth2/token", {
            "client_id": self.client_id, "client_secret": self.client_secret,
            "grant_type": "client_credentials"})["access_token"]

    def get(self, path: str, params, *, retry_auth: bool = True) -> dict:
        """params: dict or list of pairs (list preserves repeated keys)."""
        url = f"{HELIX}/{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.token}",
                          "Client-Id": self.client_id})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and retry_auth:
                exc.close()
                self.token = self._auth()
                return self.get(path, params, retry_auth=False)
            raise


REACTION = re.compile(
    r"\b(?:[a-z0-9_]*(?:kekw|omegalul|lulw|pogchamp|poggers)|clip (?:it|that))\b"
    r"|^\s*(?:lmao+|lol+|(?:ha){2,}|(?:ah){2,}|pog|no way)[!?.\s]*$",
    re.IGNORECASE,
)
REACTION_SECONDS = CHAT_WINDOW_SECONDS
REACTION_GAP_SECONDS = 1.25
LAUGHTER = re.compile(
    r"\b(?:[a-z0-9_]*(?:kekw|omegalul|lulw)|lul|forsenlaughingatyou)\b"
    r"|^\s*(?:lmao+|lol+|(?:ha){2,})[!?.\s]*$",
    re.IGNORECASE,
)


def chat_message(line: str) -> tuple[str, str] | None:
    # IRC trailing text can contain protocol words and colons.
    if line.startswith("@"):
        line = line.partition(" ")[2]
    header, separator, body = line.partition(" :")
    fields = header.split()
    if not separator or len(fields) < 3 or fields[-2] != "PRIVMSG":
        return None
    sender = fields[-3].removeprefix(":").partition("!")[0].casefold()
    return sender, body


def reaction_counts(reactions, now: float) -> tuple[int, float]:
    # Count people, so one user repeating an emote cannot create a burst.
    recent = {sender for ts, sender in reactions if 0 <= now - ts <= REACTION_SECONDS}
    previous = {sender for ts, sender in reactions
                if REACTION_SECONDS < now - ts <= REACTION_SECONDS + 30}
    return len(recent), max(3, len(previous) / 2)


def reaction_burst(reactions, now: float) -> bool:
    count, threshold = reaction_counts(reactions, now)
    return count >= threshold


def reaction_onset(reactions, active_reactors, now: float) -> float | None:
    """Find the start of the latest sustained run of distinct reactions."""
    first_by_sender = {}
    for received, sender in reversed(reactions):
        if received < now - REACTION_SECONDS:
            break
        if received <= now and sender in active_reactors:
            first_by_sender[sender] = received
    latest_run = []
    onset = None
    for received in sorted(first_by_sender.values()):
        if latest_run and received - latest_run[-1] > REACTION_GAP_SECONDS:
            latest_run.clear()
        latest_run.append(received)
        if len(latest_run) == 3:
            onset = latest_run[0]
    return onset


class IrcCounter(threading.Thread):
    """Read one channel's chat over TLS, count messages/sec, detect hype."""

    def __init__(self, channel: str, hype_z: float, on_hype, window_s: int = 240,
                 *, reaction_chat: bool = False, ranked: bool = False):
        super().__init__(daemon=True)
        self.channel = channel.lower()
        self.hype_z = hype_z
        self.on_hype = on_hype
        self.window_s = window_s
        self.reaction_chat = reaction_chat
        self.ranked = ranked
        self.per_sec: deque = deque()               # (ts, count)
        self.reactions = deque(maxlen=4000)
        self.laughter = deque(maxlen=4000)
        self.active_chatters = {}
        self.chat_share = ChatShare() if ranked else None
        self.raw_window: deque = deque(maxlen=45)   # last seconds of raw msgs
        self.hype_cooldown = 0.0
        self.count_total = 0
        self.peak_rate = 0.0
        self.error: str | None = None
        self._sec_count = 0
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def _connect(self):
        raw = socket.create_connection((IRC_HOST, IRC_PORT), timeout=30)
        try:
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=IRC_HOST)
        except OSError:
            raw.close()
            raise
        nick = f"justinfan{random.randint(100_000, 9_999_999)}"
        self.sock.sendall(f"NICK {nick}\r\nJOIN #{self.channel}\r\n".encode())

    def _tick(self):
        now = utcnow()
        self.per_sec.append((now, self._sec_count))
        self._sec_count = 0
        while self.per_sec and now - self.per_sec[0][0] > self.window_s:
            self.per_sec.popleft()
        while self.reactions and now - self.reactions[0][0] > REACTION_SECONDS + 30:
            self.reactions.popleft()
        while self.laughter and now - self.laughter[0][0] > REACTION_SECONDS:
            self.laughter.popleft()
        rate = self.per_sec[-1][1]
        if self.chat_share is not None:
            self.peak_rate = max(self.peak_rate, rate)
            self.active_chatters = {
                sender: ts for sender, ts in self.active_chatters.items()
                if 0 <= now - ts <= REACTION_SECONDS
            }
            reactors = {sender for ts, sender in self.reactions
                        if 0 <= now - ts <= REACTION_SECONDS and sender in self.active_chatters}
            evidence = self.chat_share.observe(now, len(self.active_chatters), len(reactors))
            if evidence is not None and len(reactors) >= 3:
                evidence["reaction_laughers"] = len({sender for _, sender in self.laughter} & reactors)
                onset = reaction_onset(self.reactions, reactors, now)
                if onset is not None:
                    evidence["reaction_onset"] = onset
                self.on_hype(self.channel, now, 0, rate, self.raw_window, evidence)
            return
        if len(self.per_sec) < 60:
            return
        counts = [c for _, c in self.per_sec]
        self.peak_rate = max(self.peak_rate, rate)
        z = hype_zscore(rate, counts[:-1][-self.window_s:])
        reaction = (self.reaction_chat and now >= self.hype_cooldown
                    and reaction_burst(self.reactions, now))
        if (z >= self.hype_z or reaction) and now >= self.hype_cooldown:
            self.hype_cooldown = now + 120
            snap = list(self.raw_window)
            self.on_hype(self.channel, now, z, rate, snap, reaction)

    def run(self):
        backoff = 2
        while not self._stop_event.is_set():
            self.sock = None
            try:
                self._connect()
                self.error = None
                buf = b""
                last_tick = time.monotonic()
                self._sec_count = 0
                self.per_sec.clear()
                self.raw_window.clear()
                self.reactions.clear()
                self.laughter.clear()
                self.active_chatters.clear()
                if self.chat_share is not None:
                    self.chat_share.history.clear()
                while not self._stop_event.is_set():
                    self.sock.settimeout(1.0)
                    try:
                        chunk = self.sock.recv(8192)
                        if not chunk:
                            raise OSError("connection closed")
                        backoff = 2
                        buf += chunk
                    except socket.timeout:
                        pass
                    if buf:
                        *lines, buf = buf.split(b"\r\n")
                        for part in lines:
                            text = part.decode("utf-8", "ignore")
                            if text.startswith("PING"):
                                self.sock.sendall(
                                    text.replace("PING", "PONG").encode() + b"\r\n")
                            else:
                                message = chat_message(text)
                                if message is not None:
                                    sender, body = message
                                    received = utcnow()
                                    self.count_total += 1
                                    self._sec_count += 1
                                    self.raw_window.append((received, body[:200]))
                                    if self.ranked and sender:
                                        self.active_chatters[sender] = received
                                    if self.reaction_chat and sender:
                                        laughed = self.ranked and LAUGHTER.search(body)
                                        if laughed:
                                            self.laughter.append((received, sender))
                                        if laughed or REACTION.search(body):
                                            self.reactions.append((received, sender))
                    if time.monotonic() - last_tick >= 1.0:
                        last_tick = time.monotonic()
                        self._tick()
            except OSError as exc:
                if self._stop_event.is_set():
                    return
                self.error = f"connection failed: {exc}"
                self.log_reconnect()
            finally:
                if self.sock is not None:
                    self.sock.close()
            if self._stop_event.wait(backoff):
                break
            backoff = min(backoff * 2, 60)

    def log_reconnect(self):
        print(f"[{iso(utcnow())}] irc {self.channel}: {self.error} — reconnecting",
              flush=True)


class Monitor:
    def __init__(self, args):
        self.args = args
        self.staging = Path(args.staging)
        self.staging.mkdir(parents=True, exist_ok=True)
        env = load_env(args.env)
        self.helix = HelixClient(env["TWITCH_CLIENT_ID"], env["TWITCH_CLIENT_SECRET"])
        self.channels = [c.strip().lower() for c in args.channels.split(",") if c.strip()]
        self.ids = {}
        for c in self.channels:
            data = self.helix.get("users", {"login": c})
            if data.get("data"):
                self.ids[c] = data["data"][0]["id"]
        self.live: dict = {}
        if not self.ids:
            raise ValueError("no Twitch channels resolved")
        self.hype_log = self.staging / "hype.jsonl"
        self.hype_events = SimpleQueue()
        self.pending_cuts = []
        self.quality_first = getattr(args, "quality_first", False)
        self.quality_pool = ThreadPoolExecutor(max_workers=1) if self.quality_first else None
        self.quality_cut = None
        self.quality_pending = []
        self.quality_exports = {}
        if self.quality_first:
            for path in (self.staging / "clips").glob("*_insights.json"):
                try:
                    saved = json.loads(path.read_text(encoding="utf-8"))
                    quality = saved.get("quality")
                    if quality and quality["source_end"] >= utcnow() - VIEWER_LOOKBACK:
                        key = (saved["channel"], quality["stream_id"])
                        self.quality_exports.setdefault(key, []).append(
                            (quality["source_start"], quality["source_end"]),
                        )
                except (OSError, KeyError, ValueError, TypeError, AttributeError) as exc:
                    self.log(f"could not read previous clip {path.name}: {exc}")
        self.speech_model = None
        self.quality_speech_model = load_speech_model() if getattr(args, "quality_speech", False) else None
        self.speech_pool = None
        self.speech_cut = None
        if getattr(args, "speech_chat", False):
            self.speech_model = load_speech_model()
            self.speech_pool = ThreadPoolExecutor(max_workers=1)
        self.hud_pool = None
        self.hud_scan = None
        if getattr(args, "fortnite_hud", False):
            if not shutil.which(args.tesseract):
                raise ValueError("Fortnite HUD detection requires Tesseract")
            self.hud_pool = ThreadPoolExecutor(max_workers=1)
        self.stopping = False
        config = getattr(args, "postfling_config", None)
        self.drafts = None
        if config:
            self.drafts = LiveDrafts(config, self.staging / "clips", self.ids, os.environ, self.log)
        signal.signal(signal.SIGTERM, self._sigterm)

    def _sigterm(self, *_):
        print(f"[{iso(utcnow())}] SIGTERM — stopping recorders", flush=True)
        self.stopping = True

    def log(self, msg):
        print(f"[{iso(utcnow())}] {msg}", flush=True)

    def disk_ok(self) -> bool:
        return shutil.disk_usage(self.staging).free > self.args.min_free_gb * 1024**3

    def _cut_clip(self, channel, recording, event, start, duration):
        clips = self.staging / "clips"
        clips.mkdir(exist_ok=True)
        stem = f"{channel}-{event['timestamp']:.6f}"
        clip = clips / f"{stem}.mp4"
        partial = clips / f"{stem}.partial.mp4"
        speech_partial = clips / f"{stem}.speech.partial.mp4"
        quality = event.get("quality")
        if quality and clip.exists():
            self.log(f"{channel}: clip already exists {clip.name}")
            return False
        seek = ["-ss", f"{start:.3f}"]
        source = ["-i", str(recording)]
        input_args = source + seek if quality else seek + source
        encoding = (
            ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
             "-threads", "2", "-c:a", "aac"] if quality else ["-c", "copy"]
        )
        try:
            subprocess.run(
                [self.args.ffmpeg, "-nostdin", "-loglevel", "error",
                 *input_args, "-t", str(duration), "-map", "0:v:0", "-map", "0:a:0?",
                 *encoding, "-avoid_negative_ts", "make_zero", "-movflags", "+faststart",
                 "-y", str(partial)],
                check=True, timeout=30, start_new_session=True,
            )
            actual_duration = self.validate_clip(partial, duration)
            if (getattr(self.args, "skip_twitch_ads", False)
                    and event.get("trigger", "chat") in ("chat", "chat_reaction", "viewer_clip")
                    and contains_slate(self.args.ffmpeg, partial)):
                self.log(f"{channel}: clip skipped: Twitch commercial break slate")
                partial.unlink()
                return
            speech_trimmed = False
            if "speech_reaction" in event:
                try:
                    viewer = event.get("viewer_clip")
                    sentence_starts = []
                    segments = transcribe(self.speech_model, partial, word_boundaries=bool(viewer),
                                          sentence_starts=sentence_starts)
                    if segments:
                        left, right = speech_bounds(
                            segments, min(event["speech_reaction"], actual_duration),
                            actual_duration,
                            before=viewer["duration"] + 6 if viewer else 14,
                            after=38, viewer_duration=viewer["duration"] if viewer else None,
                            sentence_starts=sentence_starts,
                        )
                        render_excerpt(self.args.ffmpeg, partial, speech_partial, left, right)
                        actual_duration = self.validate_clip(speech_partial, right - left)
                        start += left
                        speech_trimmed = True
                    else:
                        self.log(f"{channel}: no speech detected, keeping reaction window")
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    self.log(f"{channel}: speech trim failed, keeping reaction window: {exc}")
            sidecar = clips / f"{stem}_insights.json"
            pending = sidecar.with_suffix(".json.partial")
            pending.write_text(json.dumps({
                "source_video": f"https://www.twitch.tv/{channel}",
                "channel": channel,
                "timestamp": iso(event["timestamp"]),
                "category": event["category"],
                "score": event["ranking"]["score"] if quality else event["z"],
                "chat_peak": str(event["rate_per_s"]),
                "trigger": event.get("trigger", "chat"),
                **({"hud_offset": event["hud_offset"]} if "hud_offset" in event else {}),
                **({"speech_trimmed": speech_trimmed} if "speech_reaction" in event else {}),
                **({"viewer_clip": event["viewer_clip"]} if "viewer_clip" in event else {}),
                **({"ranking": event["ranking"]} if "ranking" in event else {}),
                **({"quality": quality} if quality else {}),
                "start": round(start, 3),
                "duration": actual_duration,
            }, indent=2) + "\n", encoding="utf-8")
            (speech_partial if speech_trimmed else partial).replace(clip)
            pending.replace(sidecar)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self.log(f"{channel}: clip failed: {exc}")
            return
        finally:
            if quality or "speech_reaction" in event:
                partial.unlink(missing_ok=True)
                speech_partial.unlink(missing_ok=True)
        trigger = event.get("trigger", "chat")
        self.log(f"{channel}: clip ready {clip.name} ({actual_duration:.1f}s, {trigger})")
        return True

    def validate_clip(self, path, duration):
        probe = subprocess.run(
            [self.args.ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            check=True, timeout=10, capture_output=True, text=True, start_new_session=True,
        )
        actual_duration = float(probe.stdout.strip())
        if not max(1, duration - 3) <= actual_duration <= duration + 5:
            raise ValueError(f"expected {duration:.1f}s, got {actual_duration:.1f}s")
        subprocess.run(
            [self.args.ffmpeg, "-nostdin", "-loglevel", "error", "-xerror",
             "-abort_on", "empty_output_stream", "-threads", "2", "-i", str(path),
             "-map", "0:v:0", "-map", "0:a:0?", "-f", "null", "-"],
            check=True, timeout=60, start_new_session=True,
        )
        return actual_duration

    def clip_bounds(self, event, recording_start):
        if "viewer_clip" in event:
            end = event["timestamp"] - recording_start
            start = max(0, end - event["viewer_clip"]["duration"])
            return start, end - start
        peak = max(0, event.get("anchor", event["timestamp"]) - recording_start)
        if self.args.chat_context and event["category"].casefold() == "just chatting":
            pre, post = self.args.chat_context
            start = max(0, peak - pre)
            return start, peak + post - start

        return max(0, peak - self.args.clip_pre), self.args.clip_window

    def record_quality_decision(self, channel, event, decision, **details):
        row = {
            "channel": channel,
            "timestamp": event["timestamp"],
            "ranking": event["ranking"],
            "decision": decision,
            **details,
        }
        with (self.staging / "quality.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps(row) + "\n")
        self.log(f"{channel}: quality {decision} at {event['utc']}")

    def render_quality_candidate(self, channel, state, event):
        recorder = state["clock"]
        ranking = event["ranking"]
        reaction_cue = ranking["chat_anchor"]
        onset_cue = ranking.get("chat_onset_anchor")
        cue = reaction_cue if onset_cue is None else onset_cue
        left, right = cue - 18, reaction_cue + 9
        viewer_left = ranking.get("viewer_consensus_start")
        has_viewer_bounds = viewer_left is not None
        if has_viewer_bounds:
            viewer_right = ranking["viewer_consensus_end"]
            if 10 <= cue - viewer_left <= 35 and 3 <= viewer_right - reaction_cue <= 18:
                left, right = viewer_left, viewer_right
            else:
                has_viewer_bounds = False

        def record(decision, **details):
            self.record_quality_decision(
                channel, event, decision,
                **{"stream_id": state["stream_id"], "cut_cue": cue, **details},
            )

        try:
            # The containing source interval is only used to look for clean
            # edges. The export uses the chosen boundaries, not its length.
            with recorder.window(left - 8, right + 8) as window:
                if window is None:
                    record("source coverage unavailable")
                    return
                source, origin = window
                bounds = None
                method = "Whisper word timing"
                speech_seconds = None
                model = getattr(self, "quality_speech_model", None)
                if model is not None:
                    started = time.monotonic()
                    try:
                        bounds = suggest_transcript_bounds(
                            model, self.args.ffmpeg, source, start=left - origin, end=right - origin,
                            latest_start=cue - origin - 8, earliest_end=reaction_cue - origin + 2,
                        )
                    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                        self.log(f"{channel}: Whisper timing failed: {type(exc).__name__}")
                    speech_seconds = round(time.monotonic() - started, 3)
                    record("speech timing measured", speech_seconds=speech_seconds,
                           transcript_edges=bounds is not None)
                if bounds is None:
                    method = "audio pauses"
                    bounds = suggest_pause_bounds(
                        self.args.ffmpeg, source, start=left - origin, end=right - origin,
                    )
                if bounds:
                    start, end = bounds
                elif has_viewer_bounds:
                    method = "viewer boundary agreement"
                    start, end = left - origin, right - origin
                else:
                    record("boundaries need review", source_start=left, source_end=right)
                    return
                if start > cue - origin - 8 or end < reaction_cue - origin + 2:
                    record("boundaries lost reaction context", source_start=origin + start,
                           source_end=origin + end)
                    return
                absolute_start, absolute_end = origin + start, origin + end
                recent = utcnow() - VIEWER_LOOKBACK
                self.quality_exports = {
                    key: [bounds for bounds in windows if bounds[1] >= recent]
                    for key, windows in self.quality_exports.items()
                    if any(bounds[1] >= recent for bounds in windows)
                }
                key = (channel, state["stream_id"])
                prior = self.quality_exports.setdefault(key, [])
                if any(
                    min(end_at, absolute_end) - max(start_at, absolute_start)
                    >= 0.5 * min(end_at - start_at, absolute_end - absolute_start)
                    for start_at, end_at in prior
                ):
                    record("overlapping export", source_start=absolute_start, source_end=absolute_end)
                    return
                result = inspect_source_window(
                    self.args.ffmpeg, source, start=start, duration=end - start,
                    brb_template=self.args.brb_templates.get(channel),
                )
                if result["reject"]:
                    record("footage rejected", reasons=result["reasons"],
                           source_start=absolute_start, source_end=absolute_end)
                    return
                quality = {"boundary_method": method, "source_start": absolute_start,
                           "source_end": absolute_end, "clock": "twitch_hls_pdt",
                           "stream_id": state["stream_id"], "cut_cue": cue}
                if speech_seconds is not None:
                    quality["speech_seconds"] = speech_seconds
                if self._cut_clip(channel, source, {**event, "quality": quality}, start, end - start):
                    prior.append((absolute_start, absolute_end))
                    record("exported", **quality)
                else:
                    record("render rejected", **quality)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            record("footage check failed", error=str(exc))

    def process_quality(self):
        if self.quality_cut is not None:
            if not self.quality_cut.done():
                return
            self.quality_cut.result()
            self.quality_cut = None
        if self.quality_pending:
            self.quality_pending.sort(key=lambda job: job[2]["ranking"]["score"], reverse=True)
            self.quality_cut = self.quality_pool.submit(
                self.render_quality_candidate, *self.quality_pending.pop(0),
            )

    def process_hype(self, ready_by=None):
        if self.speech_cut is not None and self.speech_cut.done():
            self.finish_speech_cut()
        cutoff = utcnow() if ready_by is None else ready_by
        events = []
        while True:
            try:
                channel, recording, start_time, event = self.hype_events.get_nowait()
            except Empty:
                break
            if "reaction_strength" in event or "viewer_creator" in event:
                state = self.live.get(channel)
                if state and state["rec_path"] == recording and start_time is not None:
                    state["ranking"].add(event, cutoff)
                continue
            events.append((channel, recording, start_time, event))
        if getattr(self.args, "rank_moments", False):
            for channel, state in self.live.items():
                for event, selected in state["ranking"].rank(cutoff):
                    evidence = event["ranking"]
                    if getattr(self, "quality_first", False):
                        if selected:
                            self.quality_pending.append((channel, state, event))
                        else:
                            self.record_quality_decision(
                                channel, event, evidence.get("reason", "insufficient evidence"),
                                stream_id=state["stream_id"],
                            )
                        continue
                    verdict = ("selected" if selected else
                               "outranked" if evidence["eligible"] else "insufficient evidence")
                    self.log(f"{channel}: rank {verdict} at {event['utc']} "
                             f"score={evidence['score']} creators={evidence['viewer_creators']} "
                             f"reactors={evidence['chat_reactors']} "
                             f"active={evidence['chat_active']} share={evidence['chat_share']} "
                             f"baseline={evidence['chat_baseline']} strength={evidence['chat_strength']} "
                             f"laughers={evidence['chat_laughers']}")
                    if selected:
                        events.append((channel, state["rec_path"], state["rec_start"], event))
        for channel, recording, start_time, event in events:
            try:
                with self.hype_log.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event) + "\n")
            except OSError as exc:
                self.log(f"{channel}: hype log write failed: {exc}")
            if recording is not None:
                # A recorder without its first segment cannot anchor chat to media.
                if start_time is None:
                    self.log(f"{channel}: spike arrived before recording was ready")
                    continue
                start, duration = self.clip_bounds(event, start_time)
                if self.speech_pool is not None and event.get("trigger") in (
                    "chat_reaction", "viewer_clip",
                ):
                    reaction = max(0, event["timestamp"] - start_time)
                    pre = event["viewer_clip"]["duration"] + 16 if "viewer_clip" in event else 28
                    start = max(0, reaction - pre)
                    post = 12 if "viewer_clip" in event else 46
                    duration = reaction + post - start
                    event = {**event, "speech_reaction": reaction - start}
                ready = start_time + start + duration + self.args.capture_grace
                self.pending_cuts.append((ready, channel, recording, event, start, duration))

        pending = []
        for ready, channel, recording, event, start, duration in self.pending_cuts:
            if cutoff < ready or ("speech_reaction" in event and self.speech_cut is not None):
                pending.append((ready, channel, recording, event, start, duration))
            elif self.disk_ok():
                if "speech_reaction" in event:
                    self.speech_cut = self.speech_pool.submit(
                        self._cut_clip, channel, recording, event, start, duration,
                    )
                else:
                    self._cut_clip(channel, recording, event, start, duration)
            else:
                self.log(f"{channel}: clip skipped at the free-space floor")
        self.pending_cuts = pending

    def finish_speech_cut(self):
        if self.speech_cut is not None:
            try:
                self.speech_cut.result()
            except Exception as exc:
                self.log(f"speech clip worker failed: {exc}")
            finally:
                self.speech_cut = None

    def process_hud(self):
        if self.hud_pool is None:
            return
        if self.hud_scan is not None:
            future, channel, state, generation = self.hud_scan
            if not future.done():
                return
            self.hud_scan = None
            if self.live.get(channel) is not state or state["hud_generation"] != generation:
                return
            try:
                samples = future.result()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                self.log(f"{channel}: Fortnite HUD scan failed: {exc}")
                state["hud"].reset()
                if not state["hud_retry"]:
                    state["hud_retry"] = True
                    state["hud_cursor"] -= 8
                    state["hud_retry_at"] = time.monotonic() + 7
                else:
                    state["hud_retry"] = False
            else:
                state["hud_retry"] = False
                for offset, kind in state["hud"].accept(samples):
                    timestamp = state["rec_start"] + offset
                    event = {
                        "timestamp": timestamp, "category": "Fortnite",
                        "z": 0.0, "rate_per_s": 0, "trigger": kind,
                        "hud_offset": offset,
                    }
                    pre = 20 if kind == "win" else 12
                    start = max(0, offset - pre)
                    end = offset + 9
                    self.pending_cuts.append((
                        state["rec_start"] + end + self.args.capture_grace,
                        channel, state["rec_path"], event, start, end - start,
                    ))
                    self.log(f"{channel}: Fortnite HUD {kind} at media {offset:.1f}s")

        # Take the oldest available window first so multiple streams can share the worker.
        eligible = []
        now = utcnow()
        for channel, state in self.live.items():
            started = state["rec_start"]
            if (started is None or state["category"].casefold() != "fortnite"
                    or time.monotonic() < state["hud_retry_at"]):
                continue
            elapsed = now - started
            cursor = state["hud_cursor"]
            if elapsed - cursor >= 30:
                eligible.append((started + cursor, channel, state, cursor))
        if eligible:
            _, channel, state, cursor = min(eligible)
            state["hud_cursor"] = cursor + 8
            future = self.hud_pool.submit(
                scan, self.args.ffmpeg, self.args.tesseract, state["rec_path"], cursor, 8,
            )
            self.hud_scan = (future, channel, state, state["hud_generation"])

    def observe_recordings(self):
        for state in self.live.values():
            clock = state.get("clock")
            if clock is not None:
                start = clock.coverage_start
                if start is not None and state["rec_start"] is None:
                    self.log(f"{clock.channel}: timestamped source available from {iso(start)}")
                state["rec_start"] = start
                continue
            path = state["rec_path"]
            if path is not None and state["rec_start"] is None:
                try:
                    if path.stat().st_size > 0:
                        state["rec_start"] = utcnow()
                except FileNotFoundError:
                    pass

    def poll_viewer_clips(self, channel, state):
        recorded_at = state["rec_start"]
        if recorded_at is None:
            return
        broadcaster = self.ids[channel]
        if state["archive"] is None:
            archives = self.helix.get("videos", {
                "user_id": broadcaster, "type": "archive", "first": 5,
            })["data"]
            state["archive"] = next((video for video in archives
                                     if video["stream_id"] == state["stream_id"]), None)
        archive = state["archive"]
        if archive is None:
            return
        now = utcnow()
        params = {"broadcaster_id": broadcaster, "first": 100,
                  "started_at": iso(max(recorded_at, now - VIEWER_LOOKBACK))}
        clips = []
        while True:
            page = self.helix.get("clips", params)
            clips.extend(page["data"])
            cursor = page.get("pagination", {}).get("cursor")
            if not cursor or not page["data"]:
                break
            params["after"] = cursor
        archive_start = datetime.fromisoformat(archive["created_at"]).timestamp()
        recent = state["viewer_events"]
        ranked = state.get("ranking") is not None
        if ranked:
            recent = state["viewer_events"] = {
                clip_id: anchor for clip_id, anchor in recent.items()
                if now - anchor <= VIEWER_LOOKBACK
            }
        for clip in sorted(clips, key=lambda item: item["created_at"]):
            if (clip["video_id"] != archive["id"] or clip["vod_offset"] is None
                    or clip["creator_id"] == broadcaster):
                continue
            start = archive_start + clip["vod_offset"]
            anchor = start + clip["duration"]
            if not recorded_at <= start < anchor <= now or now - anchor > VIEWER_LOOKBACK:
                continue
            if ranked:
                if not clip["creator_id"] or clip["id"] in recent:
                    continue
                recent[clip["id"]] = anchor
            else:
                if any(clip["id"] == clip_id or abs(anchor - timestamp) <= 25
                       for clip_id, timestamp in recent):
                    continue
                recent.append((clip["id"], anchor))
            event = {
                "channel": channel, "utc": iso(anchor), "timestamp": anchor,
                "category": state["category"], "z": 0.0, "rate_per_s": 0,
                "trigger": "viewer_clip",
                "viewer_clip": {"id": clip["id"], "url": clip["url"], "title": clip["title"],
                                "vod_offset": clip["vod_offset"], "duration": clip["duration"]},
            }
            if ranked:
                event["viewer_creator"] = clip["creator_id"]
            self.hype_events.put((channel, state["rec_path"], recorded_at, event))

    def start_live(self, channel: str, stream: dict):
        if not self.args.dry_run and not self.disk_ok():
            self.log(f"{channel}: live, recording paused at the free-space floor")
            return
        out = None
        proc = None
        clock = None
        if self.quality_first:
            out = self.staging / f"{channel}-{time.time_ns()}"
            clock = TimestampedRecorder(
                channel, out, self.args.ytdlp, self.args.rec_format, self.args.ffmpeg, self.log,
            )
        elif not self.args.dry_run:
            out = self.staging / f"{channel}-{time.time_ns()}.ts"
            proc = subprocess.Popen(
                [self.args.ytdlp, "--quiet", "--no-progress", "--no-part", "--hls-use-mpegts",
                 "-f", self.args.rec_format,
                 "-o", str(out), f"https://www.twitch.tv/{channel}"],
                stdout=subprocess.DEVNULL)
        state = {
            "rec": proc, "clock": clock, "rec_path": out, "rec_start": None,
            "started_at": stream["started_at"],
            "stream_id": stream["id"], "archive": None,
            "viewer_events": {} if self.args.rank_moments else deque(maxlen=128),
            "ranking": MomentRanking(quality_first=self.quality_first) if self.args.rank_moments else None,
            "category": stream.get("game_name", ""), "hud_cursor": 0, "hud": HudEvents(),
            "hud_generation": 0, "hud_retry": False, "hud_retry_at": 0,
        }

        def on_hype(channel, timestamp, z, rate, snapshot, reaction):
            event = {
                "channel": channel, "utc": iso(timestamp), "timestamp": timestamp,
                "z": round(z, 2), "rate_per_s": rate, "category": state["category"],
                "trigger": "chat_reaction" if reaction else "chat",
            }
            if self.args.save_chat_samples:
                event["chat_sample"] = [message for _, message in list(snapshot)[-20:]]
            if reaction:
                event["anchor"] = timestamp - self.args.chat_delay
                if self.args.rank_moments:
                    event.update(reaction)
                    if "reaction_onset" in reaction:
                        event["onset_anchor"] = reaction["reaction_onset"] - self.args.chat_delay
            self.hype_events.put((channel, out, state["rec_start"], event))

        state["irc"] = IrcCounter(channel, self.args.hype_z, on_hype,
                                 reaction_chat=self.args.reaction_chat, ranked=self.args.rank_moments)
        self.live[channel] = state
        if clock is not None:
            clock.start()
        state["irc"].start()
        self.log(f"{channel}: live — " + (f"recording {out.name}" if out else "dry run"))

    def stop_live(self, channel: str, reason: str):
        state = self.live.pop(channel)
        state["irc"].stop()
        if state.get("clock") is not None:
            state["clock"].stop()
            if not state["clock"].join(timeout=17):
                self.log(f"{channel}: timestamped recorder still stopping after timeout")
        proc = state["rec"]
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        state["irc"].join(timeout=2)
        self.log(f"{channel}: {reason}")

    def poll(self) -> list:
        if not self.ids:
            return []
        params = [("first", "100")]
        params += [("user_id", i) for i in self.ids.values()]  # list: keeps repeats
        data = self.helix.get("streams", params)
        live_now = {s["user_login"].lower(): s for s in data.get("data", [])}
        for ch, state in list(self.live.items()):
            proc = state["rec"]
            if ch not in live_now or live_now[ch]["started_at"] != state["started_at"]:
                self.stop_live(ch, "broadcast ended")
            elif proc is not None and proc.poll() is not None:
                self.stop_live(ch, f"recorder exited ({proc.returncode}); will retry on next poll")
                live_now.pop(ch)
            elif (proc is not None or state.get("clock") is not None) and not self.disk_ok():
                self.stop_live(ch, "recording stopped at the free-space floor")
                live_now.pop(ch)
            else:
                category = live_now[ch].get("game_name", "")
                if category != state["category"]:
                    state["category"] = category
                    started = state["rec_start"]
                    state["hud_cursor"] = max(0, int(utcnow() - started)) if started else 0
                    state["hud"].reset()
                    state["hud_generation"] += 1
                    state["hud_retry"] = False
                    state["hud_retry_at"] = 0
        for ch, stream in live_now.items():
            if ch in self.ids and ch not in self.live:
                self.start_live(ch, stream)
        if self.args.viewer_clips:
            for ch, state in self.live.items():
                try:
                    self.poll_viewer_clips(ch, state)
                except (OSError, ValueError, KeyError) as exc:
                    self.log(f"{ch}: viewer clip poll failed: {exc}")
        return sorted(live_now)

    def run(self):
        self.log(f"monitoring {','.join(self.ids)} (poll {self.args.poll}s)")
        next_poll = 0
        if self.drafts is not None:
            self.drafts.start()
        try:
            while not self.stopping:
                if time.monotonic() >= next_poll:
                    next_poll = time.monotonic() + self.args.poll
                    try:
                        names = ",".join(self.poll()) or "none"
                        stats = " ".join(f"{ch}:{st['irc'].count_total}msgs"
                                         for ch, st in self.live.items())
                        self.log(f"live: {names} {stats}")
                    except (OSError, ValueError, KeyError) as exc:
                        self.log(f"poll error: {exc}")
                self.observe_recordings()
                self.process_hud()
                self.process_hype()
                if self.quality_first:
                    self.process_quality()
                time.sleep(1)
        finally:
            stopped_at = utcnow()
            try:
                if self.args.rank_moments:
                    self.process_hype(ready_by=stopped_at)
                for ch in list(self.live):
                    self.stop_live(ch, "monitor stopped")
                self.process_hype(ready_by=stopped_at)
                while self.speech_cut is not None:
                    self.finish_speech_cut()
                    self.process_hype(ready_by=stopped_at)
                if self.pending_cuts:
                    self.log(f"{len(self.pending_cuts)} pending clips lack their capture window at shutdown")
                if self.quality_first:
                    while self.quality_pending or self.quality_cut is not None:
                        if self.quality_cut is not None:
                            self.quality_cut.result()
                        self.process_quality()
            finally:
                if self.quality_pool is not None:
                    self.quality_pool.shutdown()
                if self.speech_pool is not None:
                    self.speech_pool.shutdown()
                    self.finish_speech_cut()
                if self.hud_pool is not None:
                    self.hud_pool.shutdown(cancel_futures=True)
                if self.drafts is not None:
                    self.drafts.stopped.set()
                    self.drafts.join(timeout=2)


def smoke(args) -> int:
    """Verify Twitch authentication and chat collection for the selected channel."""
    env = load_env(args.env)
    client = HelixClient(env["TWITCH_CLIENT_ID"], env["TWITCH_CLIENT_SECRET"])
    target = args.channels.strip().lower()
    if not client.get("streams", {"user_login": target}).get("data"):
        raise ValueError("The selected Twitch channel is offline or unavailable")
    print(f"token OK; selected channel is live: {target}", flush=True)
    irc = IrcCounter(target, hype_z=999, on_hype=lambda *a: None)
    irc.start()
    time.sleep(args.smoke)
    irc.stop()
    irc.join(timeout=2)
    print(f"IRC OK: {irc.count_total} messages read from #{target} in {args.smoke}s",
          flush=True)
    return 0 if irc.count_total > 0 and irc.error is None else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--channels", required=True,
                    help="comma-separated Twitch logins for authorized sources")
    ap.add_argument("--env", required=True)
    ap.add_argument("--staging", default="output/live")
    ap.add_argument("--ytdlp", default="yt-dlp")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--ffprobe", default="ffprobe")
    ap.add_argument("--fortnite-hud", action="store_true",
                    help="also detect Fortnite events from the recording")
    ap.add_argument("--tesseract", default="tesseract")
    ap.add_argument("--capture-grace", type=int, default=15,
                    help="extra seconds for buffered live HLS segments before cutting")
    ap.add_argument("--poll", type=int, default=60)
    ap.add_argument("--hype-z", type=float, default=4.0)
    ap.add_argument("--reaction-chat", action="store_true",
                    help="also detect bursts of chat reactions across stream categories")
    ap.add_argument("--chat-delay", type=float, default=REACTION_SECONDS,
                    help="seconds to shift a chat reaction cue back when cutting footage")
    ap.add_argument("--speech-chat", action="store_true",
                    help="trim live chat-reaction clips near speech boundaries (uses cached Whisper)")
    ap.add_argument("--viewer-clips", action="store_true",
                    help="also detect moments clipped on Twitch by viewers")
    ap.add_argument("--rank-moments", action="store_true",
                    help="rank chat reactions and distinct Twitch clip creators before cutting")
    ap.add_argument("--quality-first", action="store_true",
                    help="capture timestamped footage and export only corroborated or exceptional cuts")
    ap.add_argument("--quality-speech", action="store_true",
                    help="try cached Whisper word timing on qualified quality-first moments")
    ap.add_argument("--brb-template", action="append", default=[], metavar="CHANNEL=IMAGE",
                    help="local broadcaster break-screen reference used in quality-first mode")
    ap.add_argument("--skip-twitch-ads", action="store_true",
                    help="skip candidates containing the Twitch commercial break slate")
    ap.add_argument("--clip-window", type=int, default=45)
    ap.add_argument("--clip-pre", type=int, default=10)
    ap.add_argument("--chat-context", type=int, nargs=2, metavar=("PRE", "POST"),
                    help="seconds before and after a Just Chatting spike")
    ap.add_argument("--min-free-gb", type=int, default=10)
    ap.add_argument("--rec-format", default="720p30/720p60/best",
                    help="yt-dlp format for the live proxy recording")
    ap.add_argument("--dry-run", action="store_true",
                    help="poll + IRC + hype log only — no recording, no cuts")
    ap.add_argument("--save-chat-samples", action="store_true",
                    help="retain short raw chat samples in the local hype log")
    ap.add_argument("--postfling-config", type=Path,
                    help="JSON mapping monitored Twitch sources to PostFling draft destinations")
    ap.add_argument("--smoke", type=int, help="smoke mode: N seconds then exit")
    args = ap.parse_args()
    channels = args.channels.split(",")
    if any(not re.fullmatch(r"[a-zA-Z0-9_]{1,25}", channel.strip()) for channel in channels):
        ap.error("--channels requires one or more Twitch logins")
    if args.smoke is not None and (args.smoke < 1 or len(channels) != 1):
        ap.error("--smoke requires a positive duration and exactly one Twitch channel")
    args.brb_templates = {}
    for spec in args.brb_template:
        channel, separator, path = spec.partition("=")
        if not separator or not channel or not path or not Path(path).is_file():
            ap.error("--brb-template requires CHANNEL=IMAGE with a readable image file")
        args.brb_templates[channel.casefold()] = Path(path)
    if args.quality_first:
        if args.dry_run or args.smoke or args.fortnite_hud or args.speech_chat or args.postfling_config:
            ap.error("--quality-first requires local capture with compatible options")
        args.reaction_chat = args.viewer_clips = args.rank_moments = args.skip_twitch_ads = True
    if args.quality_speech and not args.quality_first:
        ap.error("--quality-speech requires --quality-first")
    if args.postfling_config and (args.dry_run or args.smoke):
        ap.error("--postfling-config requires live recording mode")
    if args.smoke:
        try:
            raise SystemExit(smoke(args))
        except (OSError, ValueError, KeyError) as exc:
            ap.error(str(exc))
    if args.speech_chat and (args.dry_run or not args.reaction_chat):
        ap.error("--speech-chat requires --reaction-chat and live recording")
    if args.viewer_clips and args.dry_run:
        ap.error("--viewer-clips requires live recording")
    if args.rank_moments and not (args.reaction_chat and args.viewer_clips):
        ap.error("--rank-moments requires --reaction-chat and --viewer-clips")
    if not 0 <= args.chat_delay <= 30:
        ap.error("--chat-delay must be between 0 and 30 seconds")
    if args.poll < 1 or args.clip_window < 5 or not 0 <= args.clip_pre < args.clip_window:
        ap.error("require poll >= 1, clip window >= 5, and 0 <= clip pre < clip window")
    if args.chat_context and (args.chat_context[0] < 0 or args.chat_context[1] < 5
                              or sum(args.chat_context) > 180):
        ap.error("require chat context pre >= 0, post >= 5 and total <= 180 seconds")
    if args.capture_grace < 0 or args.min_free_gb < 0:
        ap.error("capture grace and free-space floor must be nonnegative")
    try:
        monitor = Monitor(args)
    except (OSError, ValueError, KeyError, TypeError, ImportError, RuntimeError) as exc:
        ap.error(str(exc))
    monitor.run()


if __name__ == "__main__":
    main()
