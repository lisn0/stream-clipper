#!/usr/bin/env python3
"""Score chat-spike windows against Twitch's own viewer clips (ground truth).

Fetches the app token via client credentials, pulls the VOD metadata and its
Twitch clips from the Helix API, and reports how many spike windows
overlap a Twitch clip. Stdlib only.

Credentials come from the environment, never from code:
  TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET  (an app registered at
  https://dev.twitch.tv/console — no broadcaster cooperation needed)

Usage:
  TWITCH_CLIENT_ID=... TWITCH_CLIENT_SECRET=... python3 -B clips_ground_truth.py \
      --vod 123456789 --windows windows.json [--tolerance 15] [--out report.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.twitch.tv/helix"


def http_json(url: str, params: dict | None = None, headers: dict | None = None) -> dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_form(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode("ascii")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def app_token(client_id: str, client_secret: str) -> str:
    data = post_form("https://id.twitch.tv/oauth2/token", {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
    })
    return data["access_token"]


def helix(path: str, token: str, client_id: str, params: dict) -> dict:
    return http_json(f"{API}/{path}", params,
                     {"Authorization": f"Bearer {token}", "Client-Id": client_id})


def parse_duration(text: str) -> int:
    """Helix durations appear as '8:30:12' or '8h30m12s'; tolerate both, return seconds."""
    if not text:
        return 0
    clock = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{2})", text)
    if clock:
        h = int(clock.group(1) or 0)
        return h * 3600 + int(clock.group(2)) * 60 + int(clock.group(3))
    rel = re.findall(r"(\d+)([hms])", text)
    mult = {"h": 3600, "m": 60, "s": 1}
    return sum(int(v) * mult[u] for v, u in rel)


def load_windows(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)["windows"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vod", required=True, help="Twitch VOD id")
    ap.add_argument("--windows", required=True, help="windows.json from chat_spikes.py")
    ap.add_argument("--tolerance", type=int, default=15,
                    help="count a window as a hit if it overlaps a clip by this margin (s); "
                         "must be shorter than the window length")
    ap.add_argument("--clip-window-days", type=int, default=14,
                    help="only fetch clips created within N days after the broadcast")
    ap.add_argument("--max-clips", type=int, default=500)
    ap.add_argument("--out", help="write JSON report here instead of stdout")
    args = ap.parse_args()
    if args.tolerance < 1 or args.max_clips < 1 or args.clip_window_days < 1:
        ap.error("tolerance, max clips and clip window days must be positive")

    client_id = os.environ.get("TWITCH_CLIENT_ID", "")
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print("set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in the environment",
              file=sys.stderr)
        return 2
    token = app_token(client_id, client_secret)

    videos = helix("videos", token, client_id, {"id": args.vod}).get("data", [])
    if not videos:
        print(f"VOD {args.vod} not found", file=sys.stderr)
        return 1
    video = videos[0]
    broadcaster = video["user_id"]
    duration = parse_duration(video.get("duration", ""))

    # Bound the clip fetch to clips created around the broadcast — popular
    # channels have far more clips than one page, and old clips never match.
    created = datetime.fromisoformat(video["created_at"].replace("Z", "+00:00"))
    started_at = (created - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ended_at = (created + timedelta(days=args.clip_window_days)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")

    clips = []
    cursor = None
    seen = set()
    fetched = 0
    while len(clips) < args.max_clips:
        params: dict = {"broadcaster_id": broadcaster, "first": 100,
                        "started_at": started_at, "ended_at": ended_at}
        if cursor:
            params["after"] = cursor
        page = helix("clips", token, client_id, params)
        batch = page.get("data", [])
        fetched += len(batch)
        for clip in batch:
            if clip.get("video_id") != args.vod or clip["id"] in seen:
                continue
            seen.add(clip["id"])
            clips.append(clip)
            if len(clips) == args.max_clips:
                break
        cursor = page.get("pagination", {}).get("cursor")
        if not cursor or not batch:
            break

    truth = []
    for c in clips:
        offset = c.get("vod_offset")
        if offset is None:
            continue
        truth.append({"title": c.get("title", ""), "start": float(offset),
                      "end": float(offset) + float(c.get("duration", 0)),
                      "views": c.get("view_count", 0)})

    windows = load_windows(args.windows)
    hits = 0
    center_hits = 0
    rows = []
    for w in windows:
        w_center = (w["start"] + w["end"]) / 2
        match = None
        nearest = None
        nearest_dist = None
        for t in truth:
            t_center = (t["start"] + t["end"]) / 2
            dist = abs(t_center - w_center)
            if nearest_dist is None or dist < nearest_dist:
                nearest_dist = dist
                nearest = t
            overlap = min(w["end"], t["end"]) - max(w["start"], t["start"])
            if match is None and overlap >= args.tolerance:
                match = t
        hits += bool(match)
        center_hit = nearest_dist is not None and nearest_dist <= 60
        center_hits += center_hit
        rows.append({"start": w["start"], "end": w["end"], "score": w["score"],
                     "hit": bool(match), "center_dist_s": round(nearest_dist) if nearest_dist is not None else None,
                     "center_hit": center_hit,
                     "clip": (match or nearest or {}).get("title"),
                     "clip_views": (match or nearest or {}).get("views")})

    report = {
        "vod": args.vod,
        "vod_title": video.get("title"),
        "vod_duration_s": duration,
        "clips_matched_to_vod": len(truth),
        "vod_clips_without_offsets": len(clips) - len(truth),
        "clip_query": {
            "started_at": started_at,
            "ended_at": ended_at,
            "fetched": fetched,
            "potentially_truncated": bool(cursor) or fetched >= 1000,
        },
        "windows": len(windows),
        "hits": hits,
        "hit_rate": round(hits / len(windows), 2) if windows else 0.0,
        "tolerance_s": args.tolerance,
        "center_hits_within_60s": center_hits,
        "rows": rows,
        "clips": truth,
    }
    text = json.dumps(report, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"wrote {args.out}: {hits}/{len(windows)} windows hit a Twitch clip",
              file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
