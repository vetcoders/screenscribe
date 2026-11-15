# Publishing reviews to Screenscribe Cloud

`screenscribe publish` turns a review into a live, shareable page on
Screenscribe Cloud: findings, screenshots, transcript, review state and the
recording, behind one share URL. Agents get the same review through the
`agent_manifest_url` returned with every publish.

Three ways in:

| You have | Command | What runs where |
|---|---|---|
| A finished review directory | `screenscribe publish demo_review` | Packs and uploads the review. |
| A recording, local toolchain | `screenscribe publish demo.mov` | Reviews locally (same pipeline as `review`), then publishes. |
| A recording, nothing else | `screenscribe publish demo.mov --cloud-run` | Uploads the recording; Screenscribe Cloud runs the whole review. No ffmpeg, no API key. |

`screenscribe review demo.mov --publish` does the second row as part of a
normal review run.

## 1. Sign in

Publishing needs a Screenscribe Cloud subscription session:

```bash
screenscribe auth login cloud
```

This is the OAuth device flow: the CLI prints a verification URL and a short
code, opens the browser (skip with `--no-browser`) and waits until you approve.
On success it prints the account, the host and the plan:

```text
Signed in to cloud as you@example.com (app.screenscribe.vetcoders.io).
  plan: pro (active, stripe); minutes: 15 / 120 (105 remaining)
```

The session lives in `~/.config/screenscribe/accounts.json` (mode `0600`),
next to the provider accounts. Access tokens are refreshed automatically;
refresh only ever goes to the host that issued the session.

```bash
screenscribe auth status          # every provider + the cloud line
screenscribe auth logout cloud    # forget the cloud session
```

`auth status` makes no network request when you are signed out. When signed
in it asks the server for the current plan and quota; if the server is
unreachable it shows the last known values marked
`[cached — server unreachable]`.

### Which host

The Cloud host is resolved in this order:

1. `--cloud-url` on the command,
2. the `SCREENSCRIBE_CLOUD_URL` environment variable,
3. the host you signed in to,
4. `https://app.screenscribe.vetcoders.io`.

A session is bound to the host that issued it: publishing to a different host
asks you to sign in there first. Hosts must use `https://`; plain `http://` is
accepted only for loopback (local development servers).

## 2. Publish a review directory

```bash
screenscribe publish demo_review
screenscribe publish demo_review --visibility private
screenscribe publish demo_review --no-video
screenscribe publish demo_review --video renamed_demo.mov
```

The CLI finds the review's JSON report and its recording the same way
`screenscribe serve` does. If the recording was moved or renamed, pass it with
`--video`; if you do not want to upload it at all, pass `--no-video`.

What gets uploaded:

| Archive entry | Source |
|---|---|
| `<name>_report.json` | The JSON report, sanitized (see below). Required. |
| `<name>_report.md` | The Markdown report, if present. |
| `<name>_transcript.txt` | The transcript file, or the transcript embedded in the report. |
| `review_state.json` | Human review decisions and work items, when the report carries them. |
| `screenshots/*`, `manual_frames/*` | Images only (`.jpg`, `.jpeg`, `.png`, `.webp`, `.gif`). |
| recording | Sent as a separate upload part, unless `--no-video`. |

The local HTML report is not uploaded; Screenscribe Cloud renders its own
review page from the JSON report. Anything outside this list stays on your
machine.

The bundle is a deterministic `bundle.tar.gz` described by a
`publish_manifest.v1` manifest (artifact list with sizes and SHA-256, summary,
video metadata). Uploads are streamed in 1 MiB chunks, so memory use stays flat
for multi-gigabyte recordings.

On success the CLI prints the share URL and writes `publish_receipt.json` into
the review directory. Add `--open` to open the share page in the browser.

### Visibility

| Value | Meaning |
|---|---|
| `unlisted` (default) | Shared by link: the share URL opens the review. |
| `private` | Kept to your Screenscribe Cloud account. |

Screenscribe Cloud enforces the access rules; the CLI sends the value with the
upload and records it in the receipt. Any other value exits with code `2`
before anything is packed.

### Re-publishing

Every upload carries an `Idempotency-Key` derived from the bundle hash, the
visibility and the recording (name and hash). The bundle is byte-for-byte
reproducible, so publishing the same unchanged review again sends the same key
and the server can treat it as a repeat rather than a new review. Changing the
review, the recording or the visibility produces a new key.

## 3. Publish a recording

### Local review, then publish

```bash
screenscribe publish demo.mov
screenscribe publish demo.mov --lang pl --preset veterinary -o demo_review
```

The recording goes through the regular local pipeline first (`--output`,
`--lang`, `--preset`, `--prompt`, `--vision/--no-vision`, `--local` mean the
same as in `screenscribe review`; the local report server is not started).
When the review finishes, its directory is published as in section 2. If the
local review fails, nothing is uploaded.

### Cloud review (`--cloud-run`)

```bash
screenscribe publish demo.mov --cloud-run
screenscribe publish demo.mov --cloud-run --lang en --preset programming --json
```

The recording is uploaded as a cloud job and Screenscribe Cloud runs
transcription, detection and visual analysis. The CLI follows the job's live
progress stream and falls back to polling the job status if the stream drops.
It waits up to two hours; the share URL is printed when the job completes.

`--cloud-run` needs a recording (not a review directory) and cannot be
combined with `--no-video`, since the cloud pipeline works from the uploaded
recording. Job URLs returned by the server are followed only when they point
back at the same Cloud host.

## 4. `review --publish`

```bash
screenscribe review demo.mov --publish
screenscribe review demo.mov --publish --visibility private
screenscribe review a.mov b.mov --publish --serve
```

- The sign-in is checked before the review starts, so a missing session or
  bad `--cloud-url` fails fast, before any paid transcription runs.
- Each reviewed recording is published once its review is written.
- The local report server is not started unless you pass `--serve`
  explicitly; then it starts after publishing.
- `--estimate` and `--dry-run` produce no review, so nothing is published.
- `--publish` needs the JSON report: it cannot be combined with `--no-json`.

## 5. Machine output (`--json`)

With `--json`, `screenscribe publish` prints exactly one JSON object on
stdout; progress and notes go to stderr. Use it from scripts and agents:

```bash
share_url=$(screenscribe publish demo_review --json | jq -r .share_url)
```

Success object (`publish_receipt.v1`, also saved as `publish_receipt.json`
for directory publishes):

```json
{
  "schema_version": "publish_receipt.v1",
  "mode": "publish",
  "cloud_url": "https://app.screenscribe.vetcoders.io",
  "review_id": "rev_…",
  "share_id": "shr_…",
  "share_url": "https://app.screenscribe.vetcoders.io/r/shr_…",
  "video_stream_url": "https://app.screenscribe.vetcoders.io/r/shr_…/video",
  "agent_manifest_url": "https://app.screenscribe.vetcoders.io/r/shr_…/api/export/agent-manifest",
  "report_json_url": null,
  "visibility": "unlisted",
  "created_at": "2026-11-15T12:00:00Z",
  "published_at": "2026-11-15T12:00:03Z",
  "idempotency_key": "…",
  "bundle_sha256": "…",
  "video_filename": "demo.mov",
  "video_included": true,
  "video_sha256": "…",
  "job_id": null
}
```

For `--cloud-run`, `mode` is `cloud_run`, `job_id` is set, `report_json_url`
points at the cloud report and `bundle_sha256` is `null`. Relative URLs from
the server are made absolute against `cloud_url`. The receipt never contains
tokens, your email or local paths.

Error object:

```json
{"error": "auth_required", "message": "…", "hint": "Run: screenscribe auth login cloud"}
```

Subscription errors carry the server's details:

```json
{
  "error": "subscription_required",
  "code": "quota_exceeded",
  "message": "…",
  "upgrade_url": "https://app.screenscribe.vetcoders.io/…"
}
```

Other `error` values: `invalid_usage`, `invalid_cloud_url`, `invalid_review`,
`invalid_video`, `upload_failed`, `payload_too_large`, `redirect_refused`,
`unsafe_url`, `unexpected_response`, `http_error`, `job_failed`,
`review_failed`, `auth_unavailable`.

Exit codes: `0` published, `1` publish failed, `2` invalid usage or
configuration, `130` interrupted.

## 6. Privacy and sanitization

Before anything leaves the machine:

- Absolute paths in the report, Markdown and transcript are rewritten: paths
  inside the review directory or the recording's folder become relative
  (`screenshots/01.jpg`), any other path inside a home directory becomes
  `<local-path>/<file name>`. The account name never survives.
- The report's `video` field is reduced to the file name.
- Inline frame data (`frameDataUrl`) is dropped from the report.
- Symlinked optional artifacts are skipped; a symlinked review directory,
  report or receipt is refused.
- Bearer and refresh tokens are sent only in the `Authorization` header,
  never in URLs, and never printed, logged or written to the receipt.
- Redirects are never followed, so a misconfigured host cannot bounce the
  upload (and its credentials) elsewhere.

## 7. Troubleshooting

| Symptom | What to do |
|---|---|
| `Screenscribe Cloud sign-in required` / `auth_required` | `screenscribe auth login cloud` (for the same host you publish to). An expired access token is refreshed and the request retried once before this appears. |
| `Subscription required` / HTTP 402 | No active plan, or the quota is used up. The panel shows the server's message, your minutes used (when known) and the upgrade URL; `screenscribe auth status` shows the plan. |
| `payload_too_large` | The server refused the upload size (HTTP 413). Publish with `--no-video`, or trim the recording. |
| `redirect_refused` | The host answered with a redirect. Use the final URL in `--cloud-url` / `SCREENSCRIBE_CLOUD_URL`. |
| `…but not the recording it was made from` | The recording was moved, renamed or deleted. Pass `--video <file>` or `--no-video`. |
| `Multiple videos match…` | More than one recording fits the report. Pass `--video <file>`. |
| `The stored Screenscribe Cloud session belongs to…` | You are publishing to a different host than you signed in to. Sign in with the `--cloud-url` shown in the message. |
| Network hiccups | Timeouts, dropped connections and HTTP `408`, `429`, `500`, `502`, `503`, `504` are retried up to 3 times with backoff (honouring `Retry-After`) before the command fails. |

---

_𝚅𝚒𝚋𝚎𝚌𝚛𝚊𝚏𝚝𝚎𝚍. with AI Agents by Vetcoders (c)2024-2026 LibraxisAI_
