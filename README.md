# Stream Clipping

A local Python tool that watches explicitly selected Twitch channels, detects
chat activity spikes, records live video, and cuts candidate clips. It can also
prepare completed clips as drafts in PostFling for human review.

## Requirements

- Python 3.10 or newer. The core pipeline uses only the Python standard library.
- A [Twitch application](https://dev.twitch.tv/docs/authentication/register-app/)
  client ID and secret for live-stream polling.
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) and
  [FFmpeg and ffprobe](https://ffmpeg.org/download.html) on `PATH` for recording
  and export. The chat-only checks do not require video tools.
- Optional dependencies include Tesseract with English data for
  `--fortnite-hud` and [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
  with a cached Whisper Small model for speech-assisted options.

## Installation

Install the CLI from PyPI in a Python virtual environment:

```sh
python -m pip install stream-clipper
```

Use `stream-clipping` to run the monitor and `stream-clipping-postfling` to
preview or upload clips for draft review. These commands accept the same
arguments as `pipeline/live_monitor.py` and `pipeline/push_to_postfling.py`
in the source examples below. You can also run those scripts from a checkout
without installing the package.

## Twitch use and authorization

The monitor reads stream status from Twitch Helix and chat from Twitch IRC.
Live recording uses yt-dlp. Use it only where you have authorization to record
the broadcast and retain the footage and chat under
[Twitch's Terms of Service](https://www.twitch.tv/p/terms-of-service).
Obtain the necessary rights before sharing clips, including rights for any
third-party material in the clip. Twitch also documents its
[official clip creation and retrieval API](https://dev.twitch.tv/docs/api/clips/).

The monitor requires an explicit `--channels` list; there is no implicit channel
selection. Use channel login names separated by commas.

## Configure and run

Create a local Twitch credential file (keep it outside source control):

```text
TWITCH_CLIENT_ID=your_client_id
TWITCH_CLIENT_SECRET=your_client_secret
```

Save it as `secrets/twitch.env` or another local path. Do not commit it. First
run a short dry-run to check Twitch polling and chat connectivity. This mode
does not record video:

```sh
python3 pipeline/live_monitor.py \
  --channels your_authorized_channel \
  --env secrets/twitch.env \
  --dry-run
```

After reviewing the dry-run output, start live recording and clipping:

```sh
python3 pipeline/live_monitor.py \
  --channels your_authorized_channel \
  --env secrets/twitch.env \
  --staging ./output/live
```

Stop with Ctrl-C. The staging directory defaults to `./output/live`. The
monitor keeps recordings and event logs there. Finished MP4s and their
`*_insights.json` sidecars go in `clips/` under staging. The default disk
reserve pauses recording as free space falls. Recordings and exported clips
can accumulate across sessions, so set a retention policy for staging.

By default, the chat log stores event metadata such as time and message rate.
`--save-chat-samples` adds short raw chat excerpts. Restrict access to staging
and retain recordings, chat logs, and clips only as long as necessary.

For a bounded connectivity check, use `--smoke SECONDS` with exactly one
explicit channel and the credential file. That channel must be live. Smoke mode
checks Twitch polling and chat but does not record. Use an active chat:

```sh
python3 pipeline/live_monitor.py \
  --channels your_authorized_channel \
  --env secrets/twitch.env \
  --smoke 30
```

## Optional PostFling draft review

PostFling is optional. Its uploader defaults to `review` mode, which uploads
the selected clips and saves editable workspace drafts for a person to review.
It does not initiate publishing in that mode. Preview locally first with
`--dry-run`:

```sh
python3 pipeline/push_to_postfling.py \
  --clips-dir ./output/live/clips \
  --channel YOUR_POSTFLING_CHANNEL \
  --platform YOUR_PLATFORM \
  --dry-run
```

To save drafts, load `POSTFLING_EMAIL` and `POSTFLING_PASSWORD` into the
process environment from your secret store and omit `--dry-run`:

```sh
python3 pipeline/push_to_postfling.py \
  --clips-dir ./output/live/clips \
  --channel YOUR_POSTFLING_CHANNEL \
  --platform YOUR_PLATFORM
```

The default origin is `http://127.0.0.1:8477` for local testing. For a remote
instance, set `POSTFLING_URL` to the HTTPS origin, such as
`https://workspace.example`, without an API path. The client rejects redirects.

Direct delivery is an explicit `--mode direct` action. Use it only when you
have the required rights and authorization for the content and destination.
Review each draft and destination before choosing direct delivery.

## Optional MCP server

The stdio server lets an MCP client browse completed live clips while the
monitor runs separately. Use an absolute path to a clips directory writable
only by you and the monitor. Create it or wait for the monitor to save a clip
before connecting the client.

With `uvx`, the client can launch the PyPI package with its MCP dependencies:

```sh
uvx --from 'stream-clipper[mcp]' stream-clipping-mcp \
  --clips-dir /absolute/path/to/output/live/clips
```

```json
{
  "mcpServers": {
    "stream-clipping": {
      "command": "uvx",
      "args": [
        "--from", "stream-clipper[mcp]", "stream-clipping-mcp",
        "--clips-dir", "/absolute/path/to/output/live/clips"
      ]
    }
  }
}
```

If your client cannot find `uvx` on its `PATH`, use the absolute path to it.
You can also run `python -m pip install 'stream-clipper[mcp]'` in a virtual
environment and point the client at the installed `stream-clipping-mcp`
executable with `--clips-dir` and the absolute directory path as arguments.

### Launch from a source checkout

Install the MCP Python SDK in a virtual environment for the client to launch.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-mcp.txt
```

Use absolute paths in your client's MCP server configuration.

```json
{
  "mcpServers": {
    "stream-clipping": {
      "command": "/absolute/path/to/stream-clipping/.venv/bin/python",
      "args": [
        "/absolute/path/to/stream-clipping/pipeline/clip_mcp.py",
        "--clips-dir", "/absolute/path/to/output/live/clips"
      ]
    }
  }
}
```

### MCP tools

`list_clips` shows the newest completed MP4 and insights pairs first. It returns
clip IDs, Twitch logins, Unix timestamps, and optional duration, score, and
trigger fields. Use `limit` (1–100, default 25) and `offset` (default 0) to page
through results.

To enable `save_postfling_draft`, append `--postfling-config` and the absolute
path to a JSON file to `args`. Use the same workspace and Twitch-login mapping
as the live monitor. Supply `POSTFLING_EMAIL` and `POSTFLING_PASSWORD` to the
server process through your client's secret environment or launch wrapper.

```json
{
  "origin": "https://workspace.example",
  "workspace_id": "YOUR_WORKSPACE_ID",
  "destinations": {
    "your_authorized_channel": "YOUR_DESTINATION_CHANNEL_ID"
  }
}
```

Call `save_postfling_draft` with a `clip_id` from `list_clips`. The tool uploads
that MP4 to the configured PostFling workspace and saves an editable draft for
the mapped destination. Review and publish through PostFling.

## Tests

With FFmpeg and ffprobe installed, run the full suite from the repository root:

```sh
python3 -B -m unittest discover -s tests -v
```

## License

Project code is under the [MIT License](LICENSE). The external tools and model
weights retain their own licenses.
