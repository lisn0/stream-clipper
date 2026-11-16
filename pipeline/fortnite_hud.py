"""Lightweight OCR for Fortnite elimination, knock, and victory HUD banners."""
from __future__ import annotations

import re
import struct
from pathlib import Path
import subprocess
import os
from collections import defaultdict, deque


FPS = 2
MAX_SCAN_SECONDS = 90
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_frames(payload: bytes):
    """Yield complete PNG images from ffmpeg's image2pipe output."""
    offset = 0
    while offset < len(payload):
        if payload[offset:offset + 8] != _PNG_SIGNATURE:
            raise ValueError("ffmpeg returned malformed PNG frame data")
        end = offset + 8
        while True:
            if end + 12 > len(payload):
                raise ValueError("ffmpeg returned a truncated PNG frame")
            size = struct.unpack(">I", payload[end:end + 4])[0]
            chunk_end = end + 12 + size
            if chunk_end > len(payload):
                raise ValueError("ffmpeg returned a truncated PNG chunk")
            is_end = payload[end + 4:end + 8] == b"IEND"
            end = chunk_end
            if is_end:
                break
        yield payload[offset:end]
        offset = end


def _event_type(text: str) -> str | None:
    normalized = re.sub(r"[^A-Z0-9# ]", " ", text.upper())
    normalized = " ".join(normalized.split())
    if re.search(r"\bVICTORY\s+ROYALE\b", normalized) or re.search(
            r"\b#?1\s+VICTORY\b", normalized):
        return "win"
    if re.search(r"\b(ENEMY\s+ELIMINATED|ELIMINATION)\b", normalized):
        return "elimination"
    if re.search(r"\b(KNOCKED\s+DOWN|KNOCKED|KNOCKDOWN)\b", normalized):
        return "knock"
    return None


def scan(ffmpeg: str, tesseract: str, recording: str, start: float,
         duration: float) -> list[tuple[float, str]]:
    """OCR a short recording range, sampling two combined HUD crops per second.

    The right-center crop follows the confirmed 1280x720 elimination banner
    coordinates (870,190,335,215), expressed proportionally for other sizes.
    Center notification and top-center victory crops cover alternate layouts.
    """
    if start < 0 or duration <= 0 or duration > MAX_SCAN_SECONDS:
        raise ValueError(f"scan range must be 0..{MAX_SCAN_SECONDS} seconds")
    is_transport_stream = Path(recording).suffix == ".ts"
    trim = f"trim=start={start:.3f},setpts=PTS-STARTPTS," if is_transport_stream else ""
    crop_filter = (
        f"[0:v]{trim}fps=2,split=3[k][w][m];"
        "[k]crop=w=iw*0.262:h=ih*0.299:x=iw*0.680:y=ih*0.264,"
        "scale=900:460:flags=lanczos,format=gray[kill];"
        "[w]crop=w=iw*0.50:h=ih*0.30:x=iw*0.25:y=ih*0.10,"
        "scale=900:370:flags=lanczos,format=gray[win];"
        "[m]crop=w=iw*0.29:h=ih*0.15:x=iw*0.34:y=ih*0.68,"
        "scale=900:260:flags=lanczos,format=gray[mid];"
        "[kill][win][mid]vstack=inputs=3[out]"
    )
    timeout = min(120, max(20, duration * 1.5 + start * 0.15 + 10))
    # Twitch's MPEG-TS can change H.264 parameters within the recording. A
    # byte seek can skip those updates, so decode the prefix before sampling.
    seek = ["-i", recording] if is_transport_stream else [
        "-ss", f"{start:.3f}", "-i", recording,
    ]
    decoded = subprocess.run(
        [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
         *seek, "-t", f"{duration:.3f}",
         "-an", "-filter_complex", crop_filter, "-map", "[out]",
         "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
        check=True, capture_output=True, timeout=timeout,
    )
    frames = list(_png_frames(decoded.stdout))
    expected = int(duration * FPS)
    minimum = max(1, expected - 1)  # tolerate the final half-second boundary
    if len(frames) < minimum:
        raise ValueError(f"incomplete HUD scan: decoded {len(frames)} of at least {minimum} frames")

    events = []
    for index, frame in enumerate(frames):
        result = subprocess.run(
            [tesseract, "stdin", "stdout", "--psm", "11"],
            input=frame, check=True, capture_output=True, timeout=8,
            env={**os.environ, "OMP_THREAD_LIMIT": "1"},
        )
        event = _event_type(result.stdout.decode("utf-8", errors="replace"))
        if event:
            events.append((start + index / FPS, event))
    return events


class HudEvents:
    """Debounce banner OCR and emit once per visible event, with rearming."""

    def __init__(self, confirm_reads: int = 2, confirm_window: float = 3.0,
                 rearm_after: float = 2.0):
        self.confirm_reads = confirm_reads
        self.confirm_window = confirm_window
        self.rearm_after = rearm_after
        self._reads: dict[str, deque[float]] = defaultdict(deque)
        self._last_seen: dict[str, float] = {}
        self._armed: dict[str, bool] = defaultdict(lambda: True)

    def reset(self) -> None:
        """Forget debounce state when a recorder or stream session restarts."""
        self._reads.clear()
        self._last_seen.clear()
        self._armed.clear()

    def accept(self, samples: list[tuple[float, str]]) -> list[tuple[float, str]]:
        emitted = []
        for timestamp, event in sorted(samples):
            if event not in {"elimination", "knock", "win"}:
                continue
            reads = self._reads[event]
            previous = self._last_seen.get(event)
            if previous is not None and timestamp <= previous:
                continue  # overlapping scanner windows must not count twice
            while reads and timestamp - reads[0] > self.confirm_window:
                reads.popleft()
            reads.append(timestamp)
            if previous is not None and timestamp - previous > self.rearm_after:
                self._armed[event] = True
                reads.clear()
                reads.append(timestamp)
            self._last_seen[event] = timestamp
            if len(reads) >= self.confirm_reads and self._armed[event]:
                emitted.append((reads[0], event))
                self._armed[event] = False
                reads.clear()
        return emitted
