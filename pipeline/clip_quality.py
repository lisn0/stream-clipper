"""Model-free source-window checks and conservative pause boundary hints.

These checks can identify a few mechanical capture failures. They do not judge
whether a moment is funny, coherent, or worth clipping.
"""

import math
import re
import subprocess

if __package__:
    from .twitch_slate import SLATE, is_slate
else:
    from twitch_slate import SLATE, is_slate


FRAME_SIZE = len(SLATE)
_SILENCE_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


def _sampled_frames(ffmpeg, path, start=0.0, duration=None):
    seek = ["-ss", str(max(0.0, start))] if start > 0 else []
    length = ["-t", str(duration)] if duration is not None else []
    result = subprocess.run(
        [ffmpeg, "-nostdin", "-loglevel", "error", "-threads", "2",
         "-i", str(path), *seek, *length,
         "-an", "-vf", "fps=1,scale=16:9,format=rgb24", "-pix_fmt", "rgb24",
         "-f", "rawvideo", "-"],
        check=True, timeout=60, stdout=subprocess.PIPE, start_new_session=True,
    )
    raw = result.stdout
    return [raw[i:i + FRAME_SIZE] for i in range(0, len(raw) - FRAME_SIZE + 1, FRAME_SIZE)]


def _matches_template(frame, template):
    return len(frame) == len(template) and sum(
        abs(pixel - sample) for pixel, sample in zip(frame, template)
    ) <= 10 * len(template)


def _load_template(ffmpeg, path):
    result = subprocess.run(
        [ffmpeg, "-nostdin", "-loglevel", "error", "-threads", "2", "-i", str(path),
         "-frames:v", "1", "-vf", "scale=16:9,format=rgb24", "-pix_fmt", "rgb24",
         "-f", "rawvideo", "-"],
        check=True, timeout=15, stdout=subprocess.PIPE, start_new_session=True,
    )
    return result.stdout[:FRAME_SIZE] if len(result.stdout) >= FRAME_SIZE else b""


def inspect_source_window(ffmpeg, path, *, start=0.0, duration=None, brb_template=None):
    """Inspect a bounded source interval and return rejection evidence.

    Twitch ads require 80% of five or more one-second samples. An opt-in BRB
    reference image requires a close match in five consecutive one-second
    samples or 60% of at least five samples. Static
    camera shots are not treated as BRB. Returns ``{"reject": bool,
    "reasons": [...]}``; when duration is supplied, footage below 80% of its
    requested sample count is also rejected.
    """
    frames = _sampled_frames(ffmpeg, path, start, duration)
    reasons = []
    if duration is not None and duration > 0:
        required_samples = max(5, math.ceil(duration * 0.8))
        if len(frames) < required_samples:
            reasons.append("insufficient_video_footage")
    if len(frames) >= 5 and sum(is_slate(frame) for frame in frames) / len(frames) >= 0.8:
        reasons.append("sustained_twitch_slate")
    if brb_template is not None and len(frames) >= 5:
        reference = _load_template(ffmpeg, brb_template)
        if reference:
            matches = [_matches_template(frame, reference) for frame in frames]
            if sum(matches) / len(matches) >= 0.60 or any(
                all(matches[index:index + 5]) for index in range(len(matches) - 4)
            ):
                reasons.append("sustained_broadcaster_brb_template")
    return {"reject": bool(reasons), "reasons": reasons}


def _silence_intervals(log):
    """Parse FFmpeg silencedetect output into (start, end) second pairs."""
    intervals = []
    pending = None
    for line in log.splitlines():
        start = _SILENCE_START.search(line)
        end = _SILENCE_END.search(line)
        if start:
            pending = float(start.group(1))
        if end and pending is not None:
            finish = float(end.group(1))
            if finish > pending:
                intervals.append((pending, finish))
            pending = None
    return intervals


def suggest_pause_bounds(ffmpeg, path, *, start, end, search_seconds=8.0):
    """Suggest nearby boundaries at clear silence, allowing context recovery.

    ``start`` and ``end`` are desired edges relative to the caller-provided
    bounded footage. The search may extend or shorten either edge by at most
    ``search_seconds``. A hint requires separate >=0.4s silence intervals at
    both edges and leaves at least 8s of footage. Silence is a mechanical cue,
    not proof of a sentence or joke boundary. Missing audio returns ``None``.
    """
    if start < 0 or end - start < 8 or search_seconds <= 0:
        return None
    search_seconds = min(float(search_seconds), 8.0)
    window_start = max(0.0, start - search_seconds)
    try:
        result = subprocess.run(
            [ffmpeg, "-nostdin", "-hide_banner", "-threads", "2", "-i", str(path),
             "-ss", str(window_start),
             "-t", str(end + search_seconds - window_start),
             "-map", "0:a:0?", "-vn", "-af", "silencedetect=noise=-38dB:d=0.4", "-f", "null", "-"],
            check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr or ""
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        if ("does not contain any stream" in detail or "matches no streams" in detail
                or "Output file is empty" in detail):
            return None
        raise
    intervals = _silence_intervals(result.stderr)
    # Include the nearest edge of each gap. Before an edge this can extend the
    # cut to preserve setup/payoff; after it, this can trim dead air.
    start_candidates = []
    end_candidates = []
    for index, (quiet_start, quiet_end) in enumerate(intervals):
        absolute_start = window_start + quiet_start
        absolute_end = window_start + quiet_end
        start_candidates.extend((index, edge) for edge in (absolute_start, absolute_end)
                                if start - search_seconds <= edge <= start + search_seconds)
        end_candidates.extend((index, edge) for edge in (absolute_start, absolute_end)
                              if end - search_seconds <= edge <= end + search_seconds)
    best = None
    for start_index, suggested_start in start_candidates:
        for end_index, suggested_end in end_candidates:
            if start_index == end_index:
                continue
            if suggested_end - suggested_start < 8:
                continue
            score = abs(suggested_start - start) + abs(suggested_end - end)
            if best is None or score < best[0]:
                best = (score, suggested_start, suggested_end)
    return (best[1], best[2]) if best else None
