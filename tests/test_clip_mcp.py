"""Check the MCP adapter's file scope, draft route, and optional stdio startup."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from unittest.mock import patch

PIPELINE = Path(__file__).resolve().parents[1] / "pipeline"
sys.path.insert(0, str(PIPELINE))

import clip_mcp  # noqa: E402


CLIP = "test_stream-1794660000.123456"
ORIGIN = "https://workspace.example"

try:
    mcp_major, mcp_minor, *_ = version("mcp").split(".", 2)
    HAS_SUPPORTED_MCP = mcp_major == "1" and int(mcp_minor) >= 28
except (PackageNotFoundError, ValueError):
    HAS_SUPPORTED_MCP = False


class ClipMCPTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.clips = self.root / "clips"
        self.clips.mkdir()

    def make_pair(self, clip_id=CLIP, **fields):
        video = self.clips / f"{clip_id}.mp4"
        video.write_bytes(b"\0\0\0\x10ftypmp42test")
        sidecar = self.clips / f"{clip_id}_insights.json"
        sidecar.write_text(json.dumps({"channel": clip_id.split("-", 1)[0], **fields}),
                           encoding="utf-8")
        return video

    def make_config(self):
        config = self.root / "postfling.json"
        config.write_text(json.dumps({"origin": ORIGIN, "workspace_id": "workspace",
                                      "destinations": {"test_stream": "target"}}), encoding="utf-8")
        return config

    def test_list_safe_metadata_newest_first(self):
        self.make_pair("later-1794660010", duration=4, score=float("nan"), trigger="private title")
        self.make_pair(duration=24, score=3.5, trigger="viewer_clip",
                       category="private category", source_video="https://secret.example",
                       viewer_clip={"title": "private title"}, chat_samples=["raw chat"])
        self.make_pair("earlier-1794659999")
        self.make_pair("wrong-1794660100", channel="another_stream")
        self.make_pair("malformed-1794660200")
        (self.clips / "malformed-1794660200_insights.json").write_text("{", encoding="utf-8")
        self.make_pair("empty-1794660300").write_bytes(b"")
        (self.clips / "orphan-1794660400_insights.json").write_text(
            '{"channel": "orphan"}', encoding="utf-8",
        )
        (self.clips / "orphan-1794660400.partial.mp4").write_bytes(b"unfinished")

        self.assertEqual(clip_mcp.list_completed_clips(self.clips, 2, 0), [
            {"clip_id": "later-1794660010", "channel": "later", "timestamp": 1794660010.0,
             "duration": 4.0},
            {"clip_id": CLIP, "channel": "test_stream", "timestamp": 1794660000.123456,
             "duration": 24.0, "score": 3.5, "trigger": "viewer_clip"},
        ])
        later_page = clip_mcp.list_completed_clips(self.clips, 2, 2)
        self.assertEqual([clip["clip_id"] for clip in later_page], ["earlier-1794659999"])
        for limit, offset in ((0, 0), (101, 0), (1, -1)):
            with self.subTest(limit=limit, offset=offset), self.assertRaises(ValueError):
                clip_mcp.list_completed_clips(self.clips, limit, offset)

    def test_rejects_arbitrary_paths_and_symlink_pairs(self):
        video = self.root / "outside.mp4"
        video.write_bytes(b"outside video")
        sidecar = self.root / "outside.json"
        sidecar.write_text('{"channel": "outside"}', encoding="utf-8")
        (self.clips / "outside-1.mp4").symlink_to(video)
        (self.clips / "outside-1_insights.json").write_text('{"channel": "outside"}', encoding="utf-8")
        (self.clips / "outside-2.mp4").write_bytes(b"video")
        (self.clips / "outside-2_insights.json").symlink_to(sidecar)
        self.make_pair("oversized-1")
        (self.clips / "oversized-1_insights.json").write_bytes(b" " * (clip_mcp.MAX_INSIGHTS_BYTES + 1))

        self.assertEqual(clip_mcp.list_completed_clips(self.clips, 25, 0), [])
        for clip_id in ("outside-1", "outside-2", "oversized-1", "../outside-1", str(video), f"{CLIP}.mp4"):
            with self.subTest(clip_id=clip_id), self.assertRaises(ValueError):
                clip_mcp.completed_clip(self.clips, clip_id)

    def test_selected_draft_uses_only_configured_destination(self):
        video = self.make_pair(source_video="https://secret.example", category="private title")
        self.make_pair("unmapped-1794660000")
        postfling = clip_mcp.PostFlingDrafts(self.make_config(), {
            "POSTFLING_EMAIL": "user@example.com", "POSTFLING_PASSWORD": "test",
        })
        opener = object()
        with (patch.object(postfling, "connect", return_value=opener) as connect,
              patch.object(clip_mcp, "call_json", return_value=({"channels": [{"id": "target"}]}, None)) as call,
              patch.object(clip_mcp, "media_index", return_value={}) as media,
              patch.object(clip_mcp, "save_clip_draft", return_value=("draft-id", True)) as save):
            for clip_id in ("unmapped-1794660000", "../test_stream-1794660000.123456"):
                with self.subTest(clip_id=clip_id), self.assertRaises(ValueError):
                    clip_mcp.save_selected_draft(self.clips, postfling, clip_id)
            connect.assert_not_called()

            result = clip_mcp.save_selected_draft(self.clips, postfling, CLIP)
            self.assertEqual(result, {"draft_id": "draft-id", "saved": True})
            connect.assert_called_once_with()
            call.assert_called_once_with(opener, ORIGIN, "/api/channels")
            media.assert_called_once_with(opener, ORIGIN)
            save.assert_called_once_with(opener, ORIGIN, "target", CLIP, video, "",
                                         "https://www.twitch.tv/test_stream", {})

            call.return_value = ({"channels": [{"id": "another"}]}, None)
            with self.assertRaisesRegex(ValueError, "destination is unavailable"):
                clip_mcp.save_selected_draft(self.clips, postfling, CLIP)
            self.assertEqual(save.call_count, 1)

    def test_draft_requires_the_configured_workspace(self):
        self.make_pair()
        postfling = clip_mcp.PostFlingDrafts(self.make_config(), {
            "POSTFLING_EMAIL": "user@example.com", "POSTFLING_PASSWORD": "test",
        })
        session = {"workspace": {"id": "wrong"}}
        with (patch("live_drafts.postfling_opener"),
              patch("live_drafts.call_json", side_effect=[({}, None), (session, None), ({}, None), (session, None)]),
              patch.object(clip_mcp, "save_clip_draft") as save):
            with self.assertRaisesRegex(ValueError, "could not verify the configured PostFling workspace"):
                clip_mcp.save_selected_draft(self.clips, postfling, CLIP)
        save.assert_not_called()

    @unittest.skipUnless(HAS_SUPPORTED_MCP, "optional MCP v1.28+ SDK is not installed")
    def test_stdio_tools_respect_startup_opt_in(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self.make_pair(trigger="chat")
        config = self.make_config()

        async def smoke(enabled):
            args = ["-B", str(PIPELINE / "clip_mcp.py"), "--clips-dir", str(self.clips)]
            env = {}
            if enabled:
                args.extend(("--postfling-config", str(config)))
                env = {"POSTFLING_EMAIL": "user@example.com", "POSTFLING_PASSWORD": "test"}
            server = StdioServerParameters(command=sys.executable, args=args, env=env)
            async with stdio_client(server) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                    expected = {"list_clips", "save_postfling_draft"} if enabled else {"list_clips"}
                    self.assertEqual(set(tools), expected)
                    listing = await session.call_tool("list_clips", arguments={"limit": 1})
                    self.assertFalse(listing.isError)
                    self.assertEqual(listing.structuredContent["clips"][0]["clip_id"], CLIP)
                    if enabled:
                        draft_tool = tools["save_postfling_draft"]
                        self.assertEqual(set(draft_tool.inputSchema["properties"]), {"clip_id"})
                        rejected = await session.call_tool("save_postfling_draft", arguments={"clip_id": "../x"})
                        self.assertTrue(rejected.isError)

        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                asyncio.run(asyncio.wait_for(smoke(enabled), timeout=15))
