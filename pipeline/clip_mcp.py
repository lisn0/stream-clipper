"""Browse completed live clips and optionally save a selected PostFling draft."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
from itertools import islice
from pathlib import Path
from typing import Annotated
from urllib.error import HTTPError

try:  # resolved by FastMCP when it evaluates the tool signatures
    from pydantic import Field
except ImportError:  # the optional MCP extra is not installed; make_server reports it
    Field = None

if __package__:
    from .live_drafts import PostFlingDrafts
    from .push_to_postfling import INSIGHTS_SUFFIX, LIVE_CLIP_NAME, call_json, media_index, save_clip_draft
else:
    from live_drafts import PostFlingDrafts
    from push_to_postfling import INSIGHTS_SUFFIX, LIVE_CLIP_NAME, call_json, media_index, save_clip_draft


MAX_INSIGHTS_BYTES = 128 * 1024
TRIGGERS = ("chat", "chat_reaction", "viewer_clip", "elimination", "knock", "win")


def completed_clip(clips_dir: Path, clip_id: str) -> tuple[Path, dict[str, str | float]]:
    if not LIVE_CLIP_NAME.fullmatch(clip_id):
        raise ValueError("Use a clip ID returned by list_clips")
    channel, timestamp = clip_id.split("-", 1)
    mp4 = clips_dir / f"{clip_id}.mp4"
    sidecar = clips_dir / f"{clip_id}{INSIGHTS_SUFFIX}"
    try:
        video = mp4.lstat()
        insights_file = sidecar.lstat()
        if (not stat.S_ISREG(video.st_mode) or video.st_size == 0
                or not stat.S_ISREG(insights_file.st_mode)
                or insights_file.st_size > MAX_INSIGHTS_BYTES):
            raise ValueError("Invalid clip or sidecar")
        with sidecar.open("rb") as source:
            raw_insights = source.read(MAX_INSIGHTS_BYTES + 1)
        if len(raw_insights) > MAX_INSIGHTS_BYTES:
            raise ValueError("Clip sidecar exceeds size limit")
        insights = json.loads(raw_insights)
        if not isinstance(insights, dict) or insights.get("channel") != channel:
            raise ValueError("Clip sidecar does not match the channel")
    except (OSError, ValueError, RecursionError):
        raise ValueError("Completed clip unavailable") from None

    metadata = {"clip_id": clip_id, "channel": channel, "timestamp": float(timestamp)}
    for field in ("duration", "score"):
        value = insights.get(field)
        if type(value) in (int, float) and -sys.float_info.max <= value <= sys.float_info.max:
            metadata[field] = float(value)
    if insights.get("trigger") in TRIGGERS:
        metadata["trigger"] = insights["trigger"]
    return mp4, metadata


def list_completed_clips(clips_dir: Path, limit: int, offset: int) -> list[dict[str, str | float]]:
    if not 1 <= limit <= 100 or not 0 <= offset <= sys.maxsize - limit:
        raise ValueError("Use a limit from 1 to 100 and a nonnegative offset")
    try:
        candidates = (entry.name.removesuffix(INSIGHTS_SUFFIX) for entry in clips_dir.iterdir()
                      if entry.name.endswith(INSIGHTS_SUFFIX))
        clip_ids = [clip_id for clip_id in candidates if LIVE_CLIP_NAME.fullmatch(clip_id)]
    except OSError:
        raise ValueError("Configured clips directory unavailable") from None
    clip_ids.sort(key=lambda clip_id: (float(clip_id.split("-", 1)[1]), clip_id), reverse=True)

    def completed_metadata():
        for clip_id in clip_ids:
            try:
                _, metadata = completed_clip(clips_dir, clip_id)
            except ValueError:
                continue
            yield metadata

    return list(islice(completed_metadata(), offset, offset + limit))


def save_selected_draft(clips_dir: Path, postfling: PostFlingDrafts, clip_id: str) -> dict[str, str | bool]:
    mp4, metadata = completed_clip(clips_dir, clip_id)
    channel = metadata["channel"]
    destination = postfling.destinations.get(channel)
    if destination is None:
        raise ValueError("No PostFling destination is configured for this Twitch channel")
    try:
        opener = postfling.connect()
        channels, _ = call_json(opener, postfling.origin, "/api/channels")
        if not any(item["id"] == destination for item in channels["channels"]):
            raise ValueError("PostFling destination is unavailable in the configured workspace")
        draft_id, saved = save_clip_draft(
            opener, postfling.origin, destination, clip_id, mp4, "",
            f"https://www.twitch.tv/{channel}", media_index(opener, postfling.origin),
        )
    except HTTPError as exc:
        raise ValueError(f"PostFling draft request failed (HTTP {exc.code})") from None
    except (OSError, TypeError, KeyError, json.JSONDecodeError):
        raise ValueError("Could not save the PostFling draft") from None
    return {"draft_id": draft_id, "saved": saved}


def make_server(clips_dir: Path, postfling: PostFlingDrafts | None):
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    server = FastMCP(
        "Stream Clipping",
        instructions=(
            "Browse completed Twitch live clips from a local clips directory. "
            "Call list_clips first to get clip IDs. If save_postfling_draft is available, "
            "pass it one of those IDs to upload the clip to PostFling as an unpublished draft; "
            "nothing is ever published, and the draft is reviewed and published in PostFling."
        ),
    )

    @server.tool(
        title="List completed clips",
        annotations=ToolAnnotations(
            title="List completed clips", readOnlyHint=True, destructiveHint=False,
            idempotentHint=True, openWorldHint=False,
        ),
    )
    async def list_clips(
        limit: Annotated[int, Field(ge=1, le=100, description="Page size, 1 to 100.")] = 25,
        offset: Annotated[int, Field(ge=0, description="Number of newest clips to skip, for paging.")] = 0,
    ) -> dict[str, list[dict[str, str | float]]]:
        """List completed live clips from the local clips directory, newest first, as {"clips": [...]}.

        Each entry has clip_id (a stable ID for the clip; when the server runs with
        --postfling-config, a save_postfling_draft tool also accepts it), channel (Twitch login),
        timestamp (Unix seconds), and, when recorded, duration (seconds), score, and trigger
        (chat, chat_reaction, viewer_clip, elimination, knock, or win). Only clips with a
        valid MP4 and insights sidecar are listed; incomplete or malformed ones are skipped.
        Returns metadata only, never video or chat content. Page with offset until the list
        comes back empty."""
        return {"clips": await asyncio.to_thread(list_completed_clips, clips_dir, limit, offset)}

    if postfling is not None:
        @server.tool(
            title="Save clip as PostFling draft",
            annotations=ToolAnnotations(
                title="Save clip as PostFling draft", readOnlyHint=False, destructiveHint=False,
                idempotentHint=True, openWorldHint=True,
            ),
        )
        async def save_postfling_draft(
            clip_id: Annotated[str, Field(description=(
                "Clip ID exactly as returned by list_clips, e.g. 'somechannel-1731900000.5'."
            ))],
        ) -> dict[str, str | bool]:
            """Upload one completed clip's MP4 to the configured PostFling workspace and save it as an
            unpublished draft on the destination mapped to the clip's Twitch channel.

            Use after list_clips; use list_clips alone to only browse. Never publishes, and never
            modifies or deletes local files or existing posts. Safe to retry: if a draft for this
            clip and destination already exists, nothing is uploaded again. Returns draft_id and
            saved (true if a new draft was created, false if it already existed). Fails if the
            clip is not completed, the channel has no configured destination, or PostFling
            rejects the request."""
            return await asyncio.to_thread(save_selected_draft, clips_dir, postfling, clip_id)

    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clips-dir", type=Path, required=True,
                        help="directory with completed clips and their insights sidecars")
    parser.add_argument("--postfling-config", type=Path,
                        help="enable draft uploads using this PostFling configuration file")
    args = parser.parse_args()

    try:
        clips_dir = args.clips_dir.resolve(strict=True)
        if not clips_dir.is_dir():
            parser.error("--clips-dir must be a directory")
        postfling = PostFlingDrafts(args.postfling_config, os.environ) if args.postfling_config else None
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    try:
        server = make_server(clips_dir, postfling)
    except ModuleNotFoundError as exc:
        if exc.name != "mcp":
            raise
        parser.error("install 'stream-clipper[mcp]' or the source checkout's requirements-mcp.txt")
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
