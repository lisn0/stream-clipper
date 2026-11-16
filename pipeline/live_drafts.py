"""Recover completed live clips from staging and save private PostFling drafts."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from push_to_postfling import (
    INSIGHTS_SUFFIX, LIVE_CLIP_NAME, call_json, caption_from, media_index,
    postfling_opener, postfling_origin, save_clip_draft,
)


class PostFlingDrafts:
    def __init__(self, config_path, credentials, *, channels=None):
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        if not isinstance(config, dict) or not isinstance(config.get("origin"), str):
            raise ValueError("PostFling config must specify an origin")
        self.origin = postfling_origin(config["origin"])
        self.workspace_id = config["workspace_id"]
        self.destinations = config["destinations"]
        if not isinstance(self.workspace_id, str) or not self.workspace_id.strip():
            raise ValueError("PostFling workspace_id must be a nonempty string")
        if not isinstance(self.destinations, dict) or not self.destinations:
            raise ValueError("PostFling destinations must map Twitch logins to channel IDs")
        for channel, destination in self.destinations.items():
            if not LIVE_CLIP_NAME.fullmatch(f"{channel}-0"):
                raise ValueError("PostFling destinations must use Twitch logins")
            if channels is not None and channel not in channels:
                raise ValueError(f"unknown monitored Twitch source {channel!r}")
            if not isinstance(destination, str) or not destination.strip():
                raise ValueError(f"PostFling destination ID is required for {channel}")
        self.credentials = {
            "email": credentials.get("POSTFLING_EMAIL"),
            "password": credentials.get("POSTFLING_PASSWORD"),
        }
        if not all(self.credentials.values()):
            raise ValueError("set POSTFLING_EMAIL and POSTFLING_PASSWORD in the environment")

    def connect(self):
        opener = postfling_opener(self.origin)
        call_json(opener, self.origin, "/api/login", method="POST", payload=self.credentials)
        session, _ = call_json(opener, self.origin, "/api/session")
        if (session.get("workspace") or {}).get("id") != self.workspace_id:
            call_json(opener, self.origin, "/api/workspaces/select", method="POST",
                      payload={"id": self.workspace_id})
            session, _ = call_json(opener, self.origin, "/api/session")
        if (session.get("workspace") or {}).get("id") != self.workspace_id:
            raise ValueError("could not verify the configured PostFling workspace")
        return opener


class LiveDrafts(threading.Thread):
    def __init__(self, config_path, clips, channels, credentials, log):
        super().__init__(name="postfling-drafts", daemon=True)
        self.postfling = PostFlingDrafts(config_path, credentials, channels=channels)
        self.clips = clips
        self.log = log
        self.stopped = threading.Event()

    def sync(self):
        postfling = self.postfling
        pending = []
        for sidecar in sorted(self.clips.glob(f"*{INSIGHTS_SUFFIX}")):
            stem = sidecar.name.removesuffix(INSIGHTS_SUFFIX)
            if not LIVE_CLIP_NAME.fullmatch(stem):
                continue
            channel = stem.split("-", 1)[0]
            destination = postfling.destinations.get(channel)
            if destination is None:
                continue
            target = json.dumps([postfling.origin, postfling.workspace_id, destination])
            target_id = hashlib.sha256(target.encode("utf-8")).hexdigest()[:16]
            receipt = sidecar.with_name(f"{stem}.postfling-{target_id}.json")
            if not receipt.exists():
                pending.append((stem, channel, destination, sidecar, receipt))
        if not pending:
            return

        opener = postfling.connect()
        channels, _ = call_json(opener, postfling.origin, "/api/channels")
        available = {channel["id"] for channel in channels["channels"]}
        media_by_hash = media_index(opener, postfling.origin)
        for stem, channel, destination, sidecar, receipt in pending:
            if self.stopped.is_set():
                return
            try:
                if destination not in available:
                    raise ValueError(f"destination {destination!r} is absent from configured workspace")
                insights = json.loads(sidecar.read_text(encoding="utf-8"))
                if not isinstance(insights, dict) or insights.get("channel") != channel:
                    raise ValueError("insights channel does not match the Twitch source")
                mp4 = self.clips / f"{stem}.mp4"
                if not mp4.is_file():
                    raise ValueError("finished MP4 is missing")
                caption = caption_from(insights, None, f"https://www.twitch.tv/{channel}")
                draft_id, saved = save_clip_draft(
                    opener, postfling.origin, destination, stem, mp4, "", caption, media_by_hash,
                )
                partial = receipt.with_suffix(".json.partial")
                partial.write_text(json.dumps({"draft_id": draft_id}) + "\n", encoding="utf-8")
                partial.replace(receipt)
                self.log(f"{channel}: PostFling {'saved' if saved else 'found'} draft {draft_id}")
            except (OSError, ValueError, KeyError) as exc:
                self.log(f"{stem}: PostFling draft pending: {exc}")

    def run(self):
        while not self.stopped.is_set():
            try:
                self.sync()
            except (OSError, ValueError, KeyError) as exc:
                self.log(f"PostFling drafts pending: {exc}")
            self.stopped.wait(30)
