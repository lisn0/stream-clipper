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

No package installation is needed for the standard-library pipeline and tests.

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

## Tests

With FFmpeg and ffprobe installed, run the full suite from the repository root:

```sh
python3 -B -m unittest discover -s tests -v
```

## License

Project code is under the [MIT License](LICENSE). The external tools and model
weights retain their own licenses.
