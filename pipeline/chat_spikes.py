#!/usr/bin/env python3
"""Chat spike detection over a TwitchDownloader chat JSON.

Input: chat JSON from `TwitchDownloaderCLI chatdownload --id <vod> -o chat.json`
Output: candidate highlight windows (start/end/score) as JSON, written to
stdout or --out. Uses the Python standard library.

Usage:
  python3 -B chat_spikes.py chat.json --top 10 --z 3.0 --window 75 --pad 15 \
      [--out windows.json]

Method: per-second message and emote counts -> short moving average ->
z-score against a rolling baseline -> local maxima above the threshold,
merged when closer than --min-gap seconds, ranked by combined score.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import sys


def load_comments(path: str) -> list[dict]:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        data = json.load(fh)
    comments = data.get("comments") or []
    # Defensive: keep only well-formed entries.
    out = []
    for c in comments:
        try:
            offset = float(c["content_offset_seconds"])
        except (KeyError, TypeError, ValueError):
            continue
        msg = c.get("message") or {}
        body = msg.get("body") or ""
        emotes = msg.get("emoticons") or []
        out.append({"t": offset, "body": body, "emotes": len(emotes)})
    out.sort(key=lambda c: c["t"])
    return out


def per_second_series(comments: list[dict]) -> tuple[list[int], list[int]]:
    if not comments:
        return [], []
    duration = int(comments[-1]["t"]) + 1
    msgs = [0] * duration
    emotes = [0] * duration
    for c in comments:
        s = int(c["t"])
        msgs[s] += 1
        emotes[s] += c["emotes"]
    return msgs, emotes


def moving_average(series: list[float], span: int) -> list[float]:
    if span <= 1 or not series:
        return list(series)
    half = span // 2
    prefix = [0.0]
    for v in series:
        prefix.append(prefix[-1] + v)
    out = []
    for i in range(len(series)):
        lo, hi = max(0, i - half), min(len(series), i + half + 1)
        out.append((prefix[hi] - prefix[lo]) / (hi - lo))
    return out


def z_scores(series: list[float], baseline_span: int) -> list[float]:
    """Z-score of each point against a rolling baseline ending just before it."""
    n = len(series)
    zs = [0.0] * n
    prefix = [0.0]
    prefix_sq = [0.0]
    for v in series:
        prefix.append(prefix[-1] + v)
        prefix_sq.append(prefix_sq[-1] + v * v)
    for i in range(n):
        lo = max(0, i - baseline_span)
        cnt = i - lo
        if cnt < 10:  # need a real baseline before scoring
            continue
        mean = (prefix[i] - prefix[lo]) / cnt
        var = max(0.0, (prefix_sq[i] - prefix_sq[lo]) / cnt - mean * mean)
        std = math.sqrt(var)
        zs[i] = (series[i] - mean) / std if std > 1e-9 else 0.0
    return zs


def pick_windows(z_msg: list[float], z_emote: list[float], smooth_msg: list[float],
                 smooth_emote: list[float], threshold: float, top: int,
                 window: int, pad: int, min_gap: int) -> list[dict]:
    combined = [zm + 0.5 * ze for zm, ze in zip(z_msg, z_emote)]
    candidates = []
    for t, score in enumerate(combined):
        if score < threshold:
            continue
        left = combined[t - 1] if t > 0 else -1.0
        right = combined[t + 1] if t + 1 < len(combined) else -1.0
        if score >= left and score >= right:  # local maximum
            candidates.append((t, score))
    # Merge peaks closer than min_gap, keeping the higher-scoring one.
    merged: list[tuple[int, float]] = []
    for t, score in candidates:
        if merged and t - merged[-1][0] < min_gap:
            if score > merged[-1][1]:
                merged[-1] = (t, score)
        else:
            merged.append((t, score))
    merged.sort(key=lambda p: p[1], reverse=True)
    windows = []
    for t, score in merged[:top]:
        start = max(0, t - window // 2 - pad)
        end = t + window // 2 + pad
        windows.append({
            "start": start,
            "end": end,
            "peak": t,
            "score": round(score, 2),
            "msg_rate_per_s": round(smooth_msg[t], 2),
            "emote_rate_per_s": round(smooth_emote[t], 2),
        })
    windows.sort(key=lambda w: w["start"])
    return windows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chat_json")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--z", type=float, default=3.0, help="z-score threshold")
    ap.add_argument("--window", type=int, default=75, help="clip length seconds")
    ap.add_argument("--pad", type=int, default=15, help="padding seconds each side")
    ap.add_argument("--min-gap", type=int, default=60, help="merge peaks closer than this")
    ap.add_argument("--smooth", type=int, default=10, help="moving average span (s)")
    ap.add_argument("--baseline", type=int, default=300, help="rolling baseline span (s)")
    ap.add_argument("--out", help="write JSON here instead of stdout")
    args = ap.parse_args()

    comments = load_comments(args.chat_json)
    if not comments:
        print("no usable comments found", file=sys.stderr)
        return 1
    msgs, emotes = per_second_series(comments)
    smooth_msg = moving_average([float(m) for m in msgs], args.smooth)
    smooth_emote = moving_average([float(e) for e in emotes], args.smooth)
    z_msg = z_scores(smooth_msg, args.baseline)
    z_emote = z_scores(smooth_emote, args.baseline)
    windows = pick_windows(z_msg, z_emote, smooth_msg, smooth_emote,
                           args.z, args.top, args.window, args.pad, args.min_gap)
    report = {
        "source": args.chat_json,
        "comments": len(comments),
        "duration_s": len(msgs),
        "windows": windows,
    }
    text = json.dumps(report, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"wrote {args.out} ({len(windows)} windows)", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
