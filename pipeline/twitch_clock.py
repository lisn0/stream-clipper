"""Capture bounded Twitch HLS source segments with their playlist UTC timing.

Unlike yt-dlp's growing MPEG-TS output, this optional source-segment recorder
retains original media-segment PDT. It exposes only continuous ranges and
fails closed across missing or discontinuous coverage.
"""
from __future__ import annotations

import math
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


_PDT = "#EXT-X-PROGRAM-DATE-TIME:"
_DURATION = "#EXTINF:"
_DISCONTINUITY = "#EXT-X-DISCONTINUITY"


def _utc_epoch(value: str) -> float:
    """Parse an ISO-8601 timestamp, requiring an explicit timezone."""
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("PDT must include a timezone")
    return parsed.astimezone(timezone.utc).timestamp()


@dataclass(frozen=True)
class HlsSegment:
    """One playlist media segment and the PDT associated with its first sample."""

    uri: str
    duration: float
    pdt_utc: float | None
    discontinuity: bool = False
    sequence: int | None = None


def parse_playlist(text: str) -> list[HlsSegment]:
    """Parse media-playlist segment URIs, EXTINF durations, and PDT markers.

    PDT is carried forward only until the next discontinuity. The playlist is
    expected to be a media playlist; master-playlist entries are ignored.
    Invalid or timezone-less PDT values raise ValueError rather than silently
    creating an unsafe clock mapping.
    """
    pending_duration: float | None = None
    pdt: float | None = None
    next_discontinuity = False
    media_sequence = 0
    sequence_set = False
    segments: list[HlsSegment] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_sequence = int(line.partition(":")[2])
            except ValueError as exc:
                raise ValueError("invalid media sequence") from exc
            sequence_set = True
        elif line.startswith(_PDT):
            pdt = _utc_epoch(line[len(_PDT):])
        elif line == _DISCONTINUITY:
            pdt = None
            pending_duration = None
            next_discontinuity = True
        elif line.startswith(_DURATION):
            value = line[len(_DURATION):].split(",", 1)[0]
            try:
                pending_duration = float(value)
            except ValueError as exc:
                raise ValueError(f"invalid EXTINF duration: {value!r}") from exc
            if not math.isfinite(pending_duration) or pending_duration <= 0:
                raise ValueError("EXTINF duration must be finite and positive")
        elif not line.startswith("#") and pending_duration is not None:
            segments.append(HlsSegment(
                line, pending_duration, pdt, next_discontinuity,
                media_sequence if sequence_set else None,
            ))
            media_sequence += 1
            if pdt is not None:
                pdt += pending_duration
            pending_duration = None
            next_discontinuity = False
    return segments


class TimestampedRecorder:
    """Capture Twitch's selected HLS segments directly into a bounded rolling set.

    This optional backend avoids a second full recording. It refreshes the
    signed media-playlist URL periodically using yt-dlp, downloads only unseen
    playlist segments, and exposes continuous PDT-covered ranges as concat
    demuxer manifests. No segment is served from an uncalibrated/discontinuous
    range. The caller must pass the yielded manifest to ffmpeg's concat demuxer
    and subtract `source_start_utc` from an event time for its clip offset.

    One worker performs all network and disk work. Retention and an owned-byte
    limit, URL and subprocess timeouts, and exact continuity checks bound capture.
    Existing segment files are never managed; retention touches only files this
    instance created.
    """

    RETENTION_SECONDS = 15 * 60
    PLAYLIST_REFRESH_SECONDS = 180
    POLL_SECONDS = 1.0
    MAX_PLAYLIST_BYTES = 1_000_000
    MAX_SEGMENT_BYTES = 32 * 1024 * 1024
    MAX_OWNED_BYTES = 512 * 1024 * 1024
    MAX_CONCAT_SEGMENTS = 80

    def __init__(self, channel: str, directory: str | Path, ytdlp: str,
                 rec_format: str = "720p30/720p60/best", ffmpeg: str = "ffmpeg",
                 log=None, *, request_timeout: float = 12.0):
        if not re.fullmatch(r"[A-Za-z0-9_]{1,25}", channel):
            raise ValueError("invalid Twitch channel name")
        self.channel = channel.lower()
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.ytdlp, self.rec_format, self.ffmpeg = ytdlp, rec_format, ffmpeg
        self.log = log or (lambda _: None)
        self.request_timeout = request_timeout
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._owned: dict[tuple, tuple[Path, float, float, bool]] = {}
        self._session_id = time.time_ns()
        self._protected: set[Path] = set()
        self._playlist_url: str | None = None
        self._playlist_refreshed = 0.0
        self._error: str | None = None

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def coverage_start(self) -> float | None:
        with self._lock:
            values = [row[1] for row in self._owned.values() if not row[3]]
            return min(values) if values else None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"twitch-hls-{self.channel}", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def join(self, timeout: float | None = None) -> bool:
        """Wait for capture to finish and report whether the worker stopped."""
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    def _refresh_playlist_url(self) -> str:
        result = subprocess.run(
            [self.ytdlp, "--quiet", "--no-warnings", "-g", "-f", self.rec_format,
             f"https://www.twitch.tv/{self.channel}"],
            check=True, timeout=15, capture_output=True, text=True, start_new_session=True,
        )
        urls = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        urls = [url for url in urls if urllib.parse.urlparse(url).scheme == "https"]
        if len(urls) != 1:
            raise ValueError(f"expected one muxed HLS URL, got {len(urls)}; refusing ambiguous audio/video capture")
        playlist = urls[0]
        if not playlist:
            raise ValueError("yt-dlp returned no HTTPS media URL")
        self._playlist_url = playlist
        self._playlist_refreshed = time.monotonic()
        return playlist

    def _playlist(self) -> tuple[str, str]:
        if (self._playlist_url is None or time.monotonic() - self._playlist_refreshed
                >= self.PLAYLIST_REFRESH_SECONDS):
            self._refresh_playlist_url()
        assert self._playlist_url is not None
        request = urllib.request.Request(self._playlist_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
            raw = response.read(self.MAX_PLAYLIST_BYTES + 1)
            final_url = response.geturl()
        if len(raw) > self.MAX_PLAYLIST_BYTES:
            raise ValueError("HLS playlist exceeds size limit")
        return raw.decode("utf-8-sig"), final_url

    @staticmethod
    def _key(segment: HlsSegment) -> tuple:
        return (segment.sequence, segment.pdt_utc, urllib.parse.urlparse(segment.uri).path)

    def _download(self, base_url: str, segment: HlsSegment) -> None:
        if segment.pdt_utc is None:
            return
        uri = urllib.parse.urljoin(base_url, segment.uri)
        parsed = urllib.parse.urlparse(uri)
        if parsed.scheme != "https":
            return
        key = self._key(segment)
        with self._lock:
            if key in self._owned:
                return
        request = urllib.request.Request(uri, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
            payload = response.read(self.MAX_SEGMENT_BYTES + 1)
        if not payload or len(payload) > self.MAX_SEGMENT_BYTES:
            raise ValueError("HLS segment is empty or exceeds size limit")
        self._expire()
        self._enforce_byte_budget(len(payload))
        stamp = int(segment.pdt_utc * 1_000_000)
        seq = segment.sequence if segment.sequence is not None else stamp
        path = self.directory / f"{self.channel}-{self._session_id}-{seq}-{stamp}.ts"
        tmp = path.with_suffix(".ts.partial")
        with tmp.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(path)
        with self._lock:
            self._owned[key] = (path, segment.pdt_utc, segment.duration, segment.discontinuity)

    def _enforce_byte_budget(self, incoming_bytes: int):
        """Evict oldest unpinned files owned by this recorder before a write."""
        with self._lock:
            rows = sorted(self._owned.items(), key=lambda item: item[1][1])
            sizes = {}
            for _key, (path, _pdt, _duration, _disc) in rows:
                try:
                    sizes[path] = path.stat().st_size
                except FileNotFoundError:
                    sizes[path] = 0
            total = sum(sizes.values())
            for key, (path, _pdt, _duration, _disc) in rows:
                if total + incoming_bytes <= self.MAX_OWNED_BYTES:
                    break
                if path in self._protected:
                    continue
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                total -= sizes[path]
                self._owned.pop(key, None)
            if total + incoming_bytes > self.MAX_OWNED_BYTES:
                raise ValueError("owned segment byte limit reached while windows are pinned")

    def _run(self):
        while not self._stop.is_set():
            try:
                playlist, base_url = self._playlist()
                segments = parse_playlist(playlist)
                for segment in segments:
                    if self._stop.is_set():
                        break
                    self._download(base_url, segment)
                self._expire()
                self._error = None
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                status = getattr(exc, "code", None)
                message = f"HTTP status {status}" if status is not None else type(exc).__name__
                if message != self._error:
                    self.log(f"{self.channel}: timestamped HLS capture error: {message}")
                self._error = message
                self._playlist_refreshed = 0.0
            self._stop.wait(self.POLL_SECONDS)

    def _expire(self, now_utc: float | None = None):
        cutoff = (datetime.now(timezone.utc).timestamp() if now_utc is None else now_utc) - self.RETENTION_SECONDS
        with self._lock:
            for key, (path, pdt, _duration, _disc) in list(self._owned.items()):
                if pdt < cutoff and path not in self._protected:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                    self._owned.pop(key, None)

    @contextmanager
    def window(self, start_utc: float, end_utc: float):
        """Yield `(concat_manifest, first_segment_pdt)` or `None` if uncovered.

        The manifest is removed on context exit. All segment files used by it
        are protected for the duration of the context. Requested bounds must be
        within a continuous run; gaps and explicit HLS discontinuities fail
        closed. Media seeking within the concat input is relative to the first
        segment PDT, including the subsegment offset before `start_utc`.
        """
        start, end = float(start_utc), float(end_utc)
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            yield None
            return
        manifest: Path | None = None
        selected: list[tuple[Path, float, float, bool]] = []
        unavailable = False
        with self._lock:
            ordered = sorted(self._owned.values(), key=lambda row: row[1])
            for i, row in enumerate(ordered):
                path, pdt, duration, discontinuity = row
                segment_end = pdt + duration
                if segment_end < start or pdt > start:
                    continue
                if discontinuity:
                    continue
                run = [row]
                cursor = segment_end
                for nxt in ordered[i + 1:]:
                    npath, npdt, nduration, ndisc = nxt
                    if ndisc or abs(npdt - cursor) > 0.25:
                        break
                    run.append(nxt)
                    cursor = npdt + nduration
                    if cursor >= end:
                        break
                if cursor >= end and run and run[-1][1] <= end:
                    selected = run
                    break
            if not selected or len(selected) > self.MAX_CONCAT_SEGMENTS:
                unavailable = True
            else:
                self._protected.update(row[0] for row in selected)
                manifest = self.directory / f"{self.channel}-window-{time.time_ns()}.ffconcat"
                try:
                    with manifest.open("w", encoding="utf-8") as out:
                        out.write("ffconcat version 1.0\n")
                        for path, _pdt, duration, _disc in selected:
                            # Relative paths keep the generated manifest portable and local.
                            out.write(f"file '{path.name}'\n")
                            out.write(f"duration {duration:.6f}\n")
                except OSError:
                    self._protected.difference_update(row[0] for row in selected)
                    raise
        if unavailable:
            yield None
            return
        try:
            yield (manifest, selected[0][1])
        finally:
            with self._lock:
                self._protected.difference_update(row[0] for row in selected)
                if manifest is not None:
                    try:
                        manifest.unlink()
                    except FileNotFoundError:
                        pass
