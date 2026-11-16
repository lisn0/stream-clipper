"""Make a tighter review cut around a timestamped Twitch chat reaction."""

import argparse
import io
import math
import subprocess
from pathlib import Path


def speech_bounds(segments, reaction, duration, *, before=17, after=18,
                  viewer_duration=None, sentence_starts=()):
    """Choose nearby pauses around a window that includes the reaction."""
    if not math.isfinite(reaction) or not 0 <= reaction <= duration:
        raise ValueError("reaction must be a timestamp inside the input clip")

    starts = []
    ends = []
    for (_, end), (start, _) in zip(segments, segments[1:]):
        if start - end >= 0.55:
            starts.append(start - 0.25)
            ends.append(end + 0.25)
    if segments:
        starts.append(max(0, segments[0][0] - 0.25))
        ends.append(min(duration, segments[-1][1] + 0.25))

    # Chat follows the audio. Give the exchange space on both sides.
    start_target = max(0, reaction - before)
    end_target = min(duration, reaction + after)
    start = min((point for point in starts if abs(point - start_target) <= 5),
                key=lambda point: abs(point - start_target), default=start_target)
    if viewer_duration is not None:
        # Twitch clips often include several seconds after the moment. Prefer
        # the first break in speech late in the viewer's clip or just after it.
        earliest = max(start + 12, reaction - min(24, viewer_duration * 0.8))
        latest = min(duration, reaction + 3)
        end = next((point for point in sorted(ends) if earliest <= point <= latest),
                   latest)
        # Work back from the chosen ending. A sentence or pause near the
        # expected setup is a better start than the beginning of the buffer.
        setup_target = end - 16
        start = min(
            (point for point in (*starts, *sentence_starts)
             if max(start, end - 25) <= point <= end - 9),
            key=lambda point: (abs(point - setup_target), point), default=start,
        )
        return start, end
    end = min((point for point in ends if abs(point - end_target) <= 5),
              key=lambda point: abs(point - end_target), default=end_target)
    return start, end


def load_speech_model():
    from faster_whisper import WhisperModel

    return WhisperModel("small", device="cpu", compute_type="int8",
                        cpu_threads=4, local_files_only=True)


def transcribe(model, source, *, word_boundaries=False, sentence_starts=None):
    segments, _ = model.transcribe(str(source), beam_size=1, vad_filter=True,
                                   word_timestamps=word_boundaries)
    if word_boundaries:
        words = [word for segment in segments for word in segment.words or ()]
        if sentence_starts is not None:
            sentence_starts.extend(
                max(0, word.start - 0.15)
                for previous, word in zip(words, words[1:])
                if previous.word.rstrip().endswith((".", "!", "?"))
            )
        return [(word.start, word.end) for word in words]
    return [(segment.start, segment.end) for segment in segments]


def timed_speech_edges(words, *, start, end, latest_start, earliest_end):
    """Find safe opening and closing edges near the existing cut targets."""
    openings = []
    closings = []
    for previous, following in zip(words, words[1:]):
        gap = following.start - previous.end
        sentence_end = previous.word.rstrip().endswith((".", "!", "?"))
        if gap < 0.4 and not (sentence_end and gap >= 0.12):
            continue
        padding = min(0.15, gap / 2)
        opening = following.start - padding
        closing = previous.end + padding
        if abs(opening - start) <= 8 and opening <= latest_start:
            openings.append(opening)
        if abs(closing - end) <= 8 and closing >= earliest_end:
            closings.append(closing)
    if not openings or not closings:
        return None
    return (min(openings, key=lambda point: abs(point - start)),
            min(closings, key=lambda point: abs(point - end)))


def suggest_transcript_bounds(model, ffmpeg, source, *, start, end, latest_start, earliest_end):
    """Transcribe only the candidate window from a timestamped capture."""
    offset = max(0, start - 8)
    audio = subprocess.run([
        ffmpeg, "-nostdin", "-loglevel", "error", "-threads", "1", "-i", str(source),
        "-ss", str(offset), "-t", str(end + 8 - offset), "-map", "0:a:0", "-vn",
        "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1",
    ], check=True, timeout=25, capture_output=True, start_new_session=True).stdout
    segments, _ = model.transcribe(io.BytesIO(audio), beam_size=1, vad_filter=True,
                                   word_timestamps=True)
    words = [word for segment in segments for word in segment.words or ()]
    edges = timed_speech_edges(words, start=start - offset, end=end - offset,
                               latest_start=latest_start - offset, earliest_end=earliest_end - offset)
    return tuple(edge + offset for edge in edges) if edges else None


def render_excerpt(ffmpeg, source, output, start, end):
    subprocess.run([
        ffmpeg, "-nostdin", "-loglevel", "error", "-xerror",
        "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{end - start:.3f}",
        "-map", "0:v:0", "-map", "0:a:0", "-c:v", "libx264", "-preset", "veryfast",
        "-threads", "4", "-c:a", "aac", "-movflags", "+faststart", "-n", str(output),
    ], check=True, timeout=120, start_new_session=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--reaction", type=float, required=True,
                        help="chat reaction timestamp in seconds within the source clip")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists")

    duration = float(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(args.source),
    ], text=True))
    if not math.isfinite(args.reaction) or not 0 <= args.reaction <= duration:
        parser.error("reaction must be a timestamp inside the input clip")
    start, end = speech_bounds(transcribe(load_speech_model(), args.source),
                               args.reaction, duration)
    render_excerpt("ffmpeg", args.source, args.output, start, end)
    print(f"Selected {start:.2f}s–{end:.2f}s around reaction at {args.reaction:.2f}s")


if __name__ == "__main__":
    main()
