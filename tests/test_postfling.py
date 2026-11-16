"""Exercise the uploader CLI with a strict, in-memory PostFling API."""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from live_drafts import LiveDrafts  # noqa: E402
import push_to_postfling as uploader  # noqa: E402


BASE = "https://workspace.example"
SOURCE = "https://www.twitch.tv/test_stream"
CLIP = "test_stream-1794660000.123456"
VIDEO = b"\0\0\0\x10ftypmp42test"


class Response(io.BytesIO):
    def __init__(self, data, etag=None):
        super().__init__(json.dumps(data).encode())
        self.headers = {"ETag": etag} if etag else {}


class PostFlingAPI:
    def __init__(self, test):
        self.test = test
        self.requests = []
        self.state = {"posts": [{"id": "original", "title": "Keep this"}],
                      "replied": "notes", "resolved": True}
        self.revision = 1
        self.channels = [
            {"id": "wrong", "name": "Destination", "platform": "instagram", "can_publish": True},
            {"id": "correct", "name": "Destination", "platform": "facebook", "can_publish": True},
        ]
        self.media = []
        self.deliveries = {}
        self.workspace = "default"
        self.conflict = False
        self.lose_delivery_response = False

    @property
    def etag(self):
        return f'"ws-{self.revision}"'

    def open(self, req, timeout):
        path = urlsplit(req.full_url).path
        method = req.get_method()
        route = (method, path)
        self.requests.append(route)
        if method != "GET":
            self.test.assertEqual(req.get_header("Origin"), BASE)
        if route == ("POST", "/api/login"):
            self.test.assertEqual(json.loads(req.data), {"email": "user@example.com", "password": "test"})
            return Response({"ok": True})
        if route == ("GET", "/api/session"):
            return Response({"workspace": {"id": self.workspace}})
        if route == ("POST", "/api/workspaces/select"):
            self.workspace = json.loads(req.data)["id"]
            return Response({"ok": True})
        if route == ("GET", "/api/channels"):
            return Response({"channels": self.channels})
        if route == ("GET", "/api/media"):
            return Response({"media": self.media})
        if route == ("POST", "/api/media"):
            self.test.assertEqual(req.get_header("Content-type"), "video/mp4")
            body = req.data.read()
            self.test.assertEqual(body[4:8], b"ftyp")
            self.test.assertEqual(int(req.get_header("Content-length")), len(body))
            media = {"id": f"media-{len(self.media)}", "sha256": hashlib.sha256(body).hexdigest(),
                     "name": req.get_header("X-filename")}
            self.media.insert(0, media)
            return Response(media)
        if route == ("GET", "/api/state"):
            return Response(self.state, self.etag)
        if route == ("PUT", "/api/state"):
            self.test.assertEqual(req.get_header("If-match"), self.etag)
            if self.conflict:
                self.conflict = False
                self.revision += 1
                self.state["posts"].append({"id": "concurrent", "title": "Keep me too"})
                raise HTTPError(req.full_url, 409, "Conflict", {}, None)
            self.revision += 1
            self.state = json.loads(req.data)
            return Response({"ok": True}, self.etag)
        if route == ("POST", "/api/deliveries"):
            intent = json.loads(req.data)
            key = intent["idempotency_key"]
            self.test.assertEqual(intent["channel_id"], "correct")
            self.test.assertEqual(intent["platform"], "facebook")
            self.test.assertIn(intent["media_id"], [media["id"] for media in self.media])
            self.test.assertIn(SOURCE, intent["caption"])
            self.test.assertLessEqual(len(key), 128)
            if key in self.deliveries:
                self.test.assertEqual(intent, self.deliveries[key])
                return Response({"id": "receipt", "status": "processing", "replayed": True})
            self.deliveries[key] = intent
            if self.lose_delivery_response:
                self.lose_delivery_response = False
                raise OSError("response lost after delivery")
            return Response({"id": "receipt", "status": "processing"})
        self.test.fail(f"Unexpected request {route}")


class TestPostFling(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.api = PostFlingAPI(self)

    def make_clip(self, stem=CLIP, *, basename=None, media=VIDEO, **insights):
        if basename is None:
            basename = stem
        (self.root / f"{basename}.mp4").write_bytes(media)
        (self.root / f"{stem}_insights.json").write_text(json.dumps(insights), encoding="utf-8")

    def run_cli(self, *args, credentials=True):
        argv = ["push_to_postfling.py", "--clips-dir", str(self.root),
                "--channel", "destination", "--platform", "facebook",
                "--base-url", BASE, *args]
        env = {"POSTFLING_EMAIL": "user@example.com", "POSTFLING_PASSWORD": "test"} if credentials else {}
        output = io.StringIO()
        errors = io.StringIO()
        with (patch.object(sys, "argv", argv), patch.dict(os.environ, env, clear=True),
              redirect_stdout(output), redirect_stderr(errors)):
            with patch.object(uploader.urllib.request, "build_opener", return_value=self.api):
                result = uploader.main()
        return result, output.getvalue(), errors.getvalue()

    def test_origin_accepts_https_and_loopback(self):
        for url, expected in (
            (BASE + "/", BASE), ("http://127.0.0.1:8477", "http://127.0.0.1:8477"),
            ("http://localhost:8477", "http://localhost:8477"), ("http://[::1]", "http://[::1]"),
        ):
            with self.subTest(url=url):
                self.assertEqual(uploader.postfling_origin(url), expected)

    def test_cli_rejects_unsafe_origins_before_connecting(self):
        self.make_clip(score=4.2, source_video=SOURCE)
        for url in (
            "http://workspace.example", "http://localhost.evil.example", "http://127.0.0.1@evil.example",
            "http://[::1]evil.example", "http://localhost\\evil.example",
            "https://", "https://workspace.example:bad", "https://workspace.example:0",
            "https://workspace.example/path", "https://workspace.example?", "https://workspace.example#",
            "https://user:password@workspace.example", "https://workspace.example\n",
            "file:///dev/null",
        ):
            with self.subTest(url=url), self.assertRaises(SystemExit) as error:
                self.run_cli("--base-url", url)
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(self.api.requests, [])

    def test_missing_environment_password_stops_before_connecting(self):
        self.make_clip(score=4.2, source_video=SOURCE)
        with self.assertRaises(SystemExit) as error:
            self.run_cli("--email", "user@example.com", credentials=False)
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.api.requests, [])

    def test_loopback_upload_ignores_proxy_and_rejects_redirect(self):
        requests = []

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/should-not-follow")
                self.end_headers()

            def do_GET(self):
                requests.append(self.path)
                self.send_error(500)

            def log_message(self, *args):
                pass

        with HTTPServer(("127.0.0.1", 0), RedirectHandler) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1"}, clear=True):
                    with self.assertRaises(HTTPError) as error:
                        uploader.call_json(uploader.postfling_opener(url), url, "/api/login",
                                           method="POST", payload={"password": "fixture"})
                self.assertEqual(error.exception.code, 302)
                self.assertEqual(requests, ["/api/login"])
            finally:
                server.shutdown()
                thread.join()

    def test_review_default_saves_draft_and_preserves_concurrent_changes(self):
        self.make_clip(score=4.2, category="clutch", chat_peak=50, source_video=SOURCE)
        self.api.conflict = True

        result, _, errors = self.run_cli()

        self.assertEqual(result, 0, errors)
        original, concurrent, draft = self.api.state["posts"]
        self.assertEqual(original["id"], "original")
        self.assertEqual(concurrent["id"], "concurrent")
        self.assertEqual(self.api.state["replied"], "notes")
        self.assertTrue(self.api.state["resolved"])
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["mediaId"], "media-0")
        self.assertEqual(draft["destinations"], ["correct"])
        self.assertEqual(draft["publishChannel"], "correct")
        self.assertIn("clutch", draft["copy"])
        self.assertIn("50", draft["copy"])
        self.assertIn(SOURCE, draft["copy"])
        self.assertEqual(self.api.requests.count(("PUT", "/api/state")), 2)
        self.assertNotIn(("POST", "/api/deliveries"), self.api.requests)

        draft["copy"] = "Edited in PostFling"
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(self.api.state["posts"][-1]["copy"], "Edited in PostFling")
        self.assertEqual(self.api.requests.count(("POST", "/api/media")), 1)
        self.assertEqual(self.api.requests.count(("PUT", "/api/state")), 2)

    def test_live_worker_recovers_a_draft_and_records_receipt(self):
        self.make_clip(channel="test_stream", source_video=SOURCE)
        self.make_clip("unmapped-1794660000.000000", channel="unmapped")
        config = self.root / "drafts.json"
        config.write_text(json.dumps({
            "origin": BASE, "workspace_id": "target", "destinations": {"test_stream": "correct"},
        }))
        credentials = {"POSTFLING_EMAIL": "user@example.com", "POSTFLING_PASSWORD": "test"}
        logs = []
        worker = LiveDrafts(config, self.root, ["test_stream"], credentials, logs.append)

        with patch.object(uploader.urllib.request, "build_opener", return_value=self.api):
            with patch("live_drafts.Path.replace", side_effect=OSError("receipt lost")):
                worker.sync()
            self.assertIn("receipt lost", logs[-1])
            self.assertEqual(len(self.api.state["posts"]), 2)
            worker.sync()
            receipt, = self.root.glob("*.postfling-*.json")
            draft = self.api.state["posts"][-1]
            self.assertEqual(json.loads(receipt.read_text())["draft_id"], draft["id"])
            self.assertEqual(draft["destinations"], ["correct"])
            self.assertIn(SOURCE, draft["copy"])
            request_count = len(self.api.requests)
            worker.sync()

        self.assertEqual(self.api.workspace, "target")
        self.assertLess(self.api.requests.index(("POST", "/api/workspaces/select")),
                        self.api.requests.index(("POST", "/api/media")))
        self.assertEqual(len(self.api.requests), request_count)
        self.assertEqual(self.api.requests.count(("POST", "/api/media")), 1)
        self.assertEqual(self.api.requests.count(("PUT", "/api/state")), 1)
        self.assertFalse(self.api.deliveries)

    def test_direct_retry_reuses_media_and_delivery_intent(self):
        self.make_clip(score=4, source_video=SOURCE)
        self.api.lose_delivery_response = True

        result, _, errors = self.run_cli("--mode", "direct")
        self.assertEqual(result, 1)
        self.assertIn("response lost", errors)

        result, output, errors = self.run_cli("--mode", "direct")
        self.assertEqual(result, 0, errors)
        self.assertIn("(replayed)", output)
        self.assertEqual(self.api.requests.count(("POST", "/api/media")), 1)
        self.assertEqual(self.api.requests.count(("POST", "/api/deliveries")), 2)
        self.assertNotIn(("PUT", "/api/state"), self.api.requests)

    def test_preview_pairs_vod_and_live_clips_without_network(self):
        self.make_clip("clip_01", basename="clip_01_score_2.90", score=2.90,
                       viral_category="🔥 clutch", source_video="/videos/v123456.mp4")
        self.make_clip(score=4.2, source_video=SOURCE)
        self.make_clip("clip_02", score=0.5, source_video=SOURCE)
        (self.root / "unfinished-1794660001.mp4").write_bytes(VIDEO)

        result, output, errors = self.run_cli("--dry-run", "--min-score", "2",
                                             credentials=False)

        self.assertEqual(result, 0, errors)
        self.assertIn("clip_01_score_2.90.mp4", output)
        self.assertIn("https://www.twitch.tv/videos/123456", output)
        self.assertIn(f"{CLIP}.mp4", output)
        self.assertIn(SOURCE, output)
        self.assertIn("1 below the minimum score", output)
        self.assertNotIn("unfinished", output)
        self.assertEqual(self.api.requests, [])

    def test_direct_rejects_disconnected_destination_before_mutating_content(self):
        self.api.channels[1]["can_publish"] = False
        self.make_clip(score=4.2, source_video=SOURCE)

        result, _, errors = self.run_cli("--mode", "direct")

        self.assertEqual(result, 1)
        self.assertIn("cannot publish", errors)
        self.assertEqual(self.api.requests, [("POST", "/api/login"), ("GET", "/api/channels")])


if __name__ == "__main__":
    unittest.main()
