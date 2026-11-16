#!/usr/bin/env python3
"""Send completed clips to PostFling as workspace drafts or direct deliveries.

The default review mode saves an editable draft for the selected destination.
Use --mode direct to initiate delivery through the connected account.
Set POSTFLING_EMAIL and POSTFLING_PASSWORD in the environment.
"""
from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


INSIGHTS_SUFFIX = "_insights.json"
LIVE_CLIP_NAME = re.compile(r"[a-zA-Z0-9_]+-\d+(?:\.\d+)?")


def postfling_origin(value: str) -> str:
    try:
        url = urllib.parse.urlsplit(value)
        port = url.port
    except ValueError:
        raise ValueError("PostFling origin must be a bare HTTP(S) origin") from None
    if (not re.fullmatch(r"(?:[a-z0-9.-]+|\[[a-f0-9:.]+\])(?::[0-9]+)?", url.netloc, re.I)
            or port == 0 or url.path not in ("", "/")
            or re.search(r"[\s?#\x00-\x1f\x7f]", value)):
        raise ValueError("PostFling origin must be a bare HTTP(S) origin")
    if url.scheme != "https" and (
        url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("PostFling HTTPS is required outside loopback")
    return f"{url.scheme}://{url.netloc}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Credentials and media stay on the explicitly configured origin.
        return None


def postfling_opener(origin: str):
    handlers = [NoRedirect(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())]
    if origin.lower().startswith("http:"):
        # A local test origin must not send its requests through an HTTP proxy.
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def call_json(opener, base: str, path: str, *, method="GET", payload=None,
              headers=None) -> tuple[dict, str | None]:
    req = urllib.request.Request(base + path, method=method, headers=headers or {})
    if payload is not None:
        req.data = json.dumps(payload).encode("utf-8")
        req.add_header("Content-Type", "application/json")
        req.add_header("Origin", base)
    with opener.open(req, timeout=60) as resp:
        return json.load(resp), resp.headers.get("ETag")


def resolve_channel(opener, base: str, name: str, platform: str) -> dict:
    data, _ = call_json(opener, base, "/api/channels")
    matches = [
        channel for channel in data["channels"]
        if channel["name"].casefold() == name.casefold()
        and channel["platform"] == platform
    ]
    if len(matches) != 1:
        raise ValueError(f"expected one PostFling channel named {name!r} on {platform}, "
                         f"found {len(matches)}")
    return matches[0]


def upload_media(opener, base: str, path: Path, media_by_hash: dict[str, str]) -> str:
    with path.open("rb") as body:
        size = os.fstat(body.fileno()).st_size
        digest = hashlib.sha256()
        for chunk in iter(lambda: body.read(1024 * 1024), b""):
            digest.update(chunk)
        sha256 = digest.hexdigest()
        if sha256 in media_by_hash:
            return media_by_hash[sha256]

        body.seek(0)
        req = urllib.request.Request(
            f"{base}/api/media", data=body, method="POST",
            headers={"Content-Type": "video/mp4", "Origin": base,
                     "Content-Length": str(size),
                     "X-Filename": urllib.parse.quote(path.name, safe="")})
        with opener.open(req, timeout=600) as resp:
            media_id = json.load(resp)["id"]
    media_by_hash[sha256] = media_id
    return media_id


def load_workspace(opener, base: str) -> tuple[dict, str]:
    state, revision = call_json(opener, base, "/api/state")
    if not revision:
        raise ValueError("PostFling did not return the workspace revision")
    return state, revision


def save_draft(opener, base: str, state: dict, revision: str, draft: dict) -> bool:
    for attempt in range(3):
        if any(post.get("id") == draft["id"] for post in state["posts"]):
            return False
        updated = {**state, "posts": [*state["posts"], draft]}
        try:
            call_json(opener, base, "/api/state", method="PUT", payload=updated,
                      headers={"If-Match": revision})
            return True
        except urllib.error.HTTPError as exc:
            if exc.code != 409 or attempt == 2:
                raise
        state, revision = load_workspace(opener, base)


def find_clip(sidecar: Path, stem: str) -> Path:
    exact = sidecar.with_name(f"{stem}.mp4")
    if exact.is_file():
        return exact
    if stem.startswith("clip_"):
        prefix = f"{stem}_score_"
        matches = sorted(path for path in sidecar.parent.iterdir()
                         if path.name.startswith(prefix) and path.suffix == ".mp4"
                         and path.is_file())
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise ValueError("multiple scored MP4s match this sidecar")
    raise ValueError("no matching MP4")


def source_from(insights: dict, source_url: str, directory: str) -> str:
    source = str(source_url or insights.get("source_video") or "").strip()
    url = urllib.parse.urlsplit(source)
    if url.scheme in {"http", "https"} and url.netloc:
        if len(source) > 2200:
            raise ValueError("source URL is too long for a PostFling caption")
        return source
    vod = re.search(r"(?:^|/)v?(\d+)(?:\.[^/]+)?$", source)
    if vod:
        return f"https://www.twitch.tv/videos/{vod[1]}"
    if directory.isdigit():
        return f"https://www.twitch.tv/videos/{directory}"
    raise ValueError("source URL missing or invalid; set --source-url")


def caption_from(insights: dict, template: str | None, source: str) -> str:
    category = str(insights.get("viral_category") or insights.get("category") or "").lstrip("🔥 ")
    chat_peak = insights.get("chat_peak")
    chat_peak = "" if chat_peak is None else str(chat_peak)
    if template is None:
        text = "\n".join(part for part in (
            f"🔥 {category} moment" if category else "",
            f"Chat peak: {chat_peak}" if chat_peak else "",
        ) if part)
    else:
        text = template.format(category=category, chat_peak=chat_peak,
                               signal=insights.get("dominant_signal") or "", source=source)
    text = text[:2200].strip()
    if source in text:
        return text
    room = max(0, 2199 - len(source))
    return "\n".join(part for part in (text[:room].rstrip(), source) if part)


def save_clip_draft(opener, base, channel_id, stem, mp4, clip_source, caption, media_by_hash):
    key = delivery_key(channel_id, stem, clip_source)
    state, revision = load_workspace(opener, base)
    if any(post.get("id") == key for post in state["posts"]):
        return key, False
    media_id = upload_media(opener, base, mp4, media_by_hash)
    draft = {
        "id": key, "status": "draft", "title": mp4.stem[:160],
        "mediaId": media_id, "copy": caption, "destinations": [channel_id],
        "publishChannel": channel_id, "deliveryIdempotencyKey": key,
    }
    return key, save_draft(opener, base, state, revision, draft)


def delivery_key(channel_id, stem, clip_source):
    identity = json.dumps([clip_source, stem, channel_id], ensure_ascii=False)
    return "clip:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def media_index(opener, base):
    library, _ = call_json(opener, base, "/api/media")
    # The library lists newest first. Prefer the oldest visible copy on replay.
    return {item["sha256"]: item["id"] for item in library["media"]}



def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default=os.environ.get("POSTFLING_URL", "http://127.0.0.1:8477"),
                    help="PostFling's canonical origin")
    ap.add_argument("--email", default=os.environ.get("POSTFLING_EMAIL", ""))
    ap.add_argument("--clips-dir", required=True, type=Path,
                    help="directory with completed MP4 clips and their *_insights.json sidecars")
    ap.add_argument("--channel", required=True, help="PostFling channel name")
    ap.add_argument("--platform", required=True, type=str.lower, help="PostFling channel's platform")
    ap.add_argument("--mode", choices=("review", "direct"), default="review",
                    help="save for workspace review (default) or initiate direct delivery")
    ap.add_argument("--source-url", default="", help="override source attribution in the sidecar")
    ap.add_argument("--caption-template", help="fields: {category}, {chat_peak}, {signal}, {source}")
    ap.add_argument("--min-score", type=float, default=0.0,
                    help="only process clips at or above this score")
    ap.add_argument("--dry-run", action="store_true",
                    help="preview clip captions locally")
    args = ap.parse_args()

    if not math.isfinite(args.min_score):
        ap.error("--min-score must be finite")
    clip_dir = args.clips_dir
    clips = []
    invalid = skipped = 0
    for sidecar in sorted(clip_dir.glob(f"*{INSIGHTS_SUFFIX}")):
        stem = sidecar.name.removesuffix(INSIGHTS_SUFFIX)
        if not (stem.startswith("clip_") or LIVE_CLIP_NAME.fullmatch(stem)):
            continue
        try:
            with sidecar.open(encoding="utf-8") as fh:
                insights = json.load(fh)
            if not isinstance(insights, dict):
                raise ValueError("insights must be a JSON object")
            score = float(insights.get("score", 0))
            if not math.isfinite(score):
                raise ValueError("score must be finite")
            if score < args.min_score:
                skipped += 1
                continue
            mp4 = find_clip(sidecar, stem)
            source = source_from(insights, args.source_url, clip_dir.name)
            caption = caption_from(insights, args.caption_template, source)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            invalid += 1
            print(f"{sidecar.name}: {exc}", file=sys.stderr)
            continue
        # Caption overrides must not create a new delivery intent for the same clip.
        clip_source = "" if LIVE_CLIP_NAME.fullmatch(stem) else (
            insights.get("source_video") or clip_dir.name
        )
        clips.append((stem, mp4, score, clip_source, caption))

    if not clips and not skipped and not invalid:
        print(f"no clip insights in {clip_dir}", file=sys.stderr)
        return 1
    if args.dry_run or not clips:
        for _, mp4, score, _, caption in clips:
            print(f"{mp4.name} score={score:.2f} mode={args.mode}\n{caption}")
        print(f"done: {len(clips)} clips, {skipped} below the minimum score, {invalid} invalid")
        return int(bool(invalid))

    password = os.environ.get("POSTFLING_PASSWORD")
    if not args.email or not password:
        ap.error("set POSTFLING_EMAIL and POSTFLING_PASSWORD in the environment")
    try:
        base = postfling_origin(args.base_url)
    except ValueError as exc:
        ap.error(str(exc))
    opener = postfling_opener(base)
    try:
        call_json(opener, base, "/api/login", method="POST",
                  payload={"email": args.email, "password": password})
        channel = resolve_channel(opener, base, args.channel, args.platform)
        channel_id = str(channel["id"])
        if args.mode == "direct" and not channel["can_publish"]:
            raise ValueError(f"{channel['name']} cannot publish on {args.platform}")
        media_by_hash = media_index(opener, base)

        for stem, mp4, score, clip_source, caption in clips:
            print(f"{mp4.name} score={score:.2f}")

            if args.mode == "review":
                key, saved = save_clip_draft(
                    opener, base, channel_id, stem, mp4, clip_source, caption, media_by_hash,
                )
                print(f"  {'saved' if saved else 'existing'} draft {key}")
                continue

            key = delivery_key(channel_id, stem, clip_source)
            media_id = upload_media(opener, base, mp4, media_by_hash)
            receipt, _ = call_json(opener, base, "/api/deliveries", method="POST",
                                   payload={"platform": args.platform, "channel_id": channel_id,
                                            "media_id": media_id, "caption": caption,
                                            "idempotency_key": key})
            replay = " (replayed)" if receipt.get("replayed") else ""
            print(f"  delivery {receipt['id']}: {receipt['status']}{replay}")
            if receipt["status"] in {"failed", "uncertain"}:
                return 1
    except urllib.error.HTTPError as exc:
        try:
            reason = json.load(exc).get("error", exc.reason)
        except (ValueError, OSError):
            reason = exc.reason
        print(f"PostFling HTTP {exc.code}: {reason}", file=sys.stderr)
        if args.mode == "direct":
            print("Check Publishing activity before retrying delivery.", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"PostFling: {exc}", file=sys.stderr)
        return 1

    print(f"done: {len(clips)} clips, {skipped} below the minimum score, {invalid} invalid")
    return int(bool(invalid))


if __name__ == "__main__":
    raise SystemExit(main())
