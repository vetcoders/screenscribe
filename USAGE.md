# screenscribe Usage Guide

A comprehensive reference for every screenscribe command, flag, workflow, and
common troubleshooting case. For a quick overview and installation, see the
[README](README.md).

## Table of contents

- [Concepts](#concepts)
- [Global behavior](#global-behavior)
- [Commands](#commands)
  - [screenscribe review](#screenscribe-review)
  - [screenscribe analyze](#screenscribe-analyze)
  - [screenscribe transcribe](#screenscribe-transcribe)
  - [screenscribe preprocess](#screenscribe-preprocess)
  - [screenscribe config](#screenscribe-config)
  - [screenscribe version](#screenscribe-version)
- [Configuration reference](#configuration-reference)
- [Local and offline STT](#local-and-offline-stt)
- [Workflows](#workflows)
- [Output artifacts](#output-artifacts)
- [Troubleshooting](#troubleshooting)

---

## Concepts

screenscribe turns a narrated screen recording into structured engineering
findings through a **STT → LLM → VLM** pipeline:

- **STT** (speech-to-text) transcribes the narration into timestamped segments.
- **LLM** reads the transcript and surfaces actionable moments.
- **VLM** (vision-language model) confirms each moment against a captured frame.

There are two ways to drive it:

- **Automatic** (`review`): the pipeline processes the whole recording for you.
- **Human-first** (`analyze`): an interactive browser dashboard where you point
  the AI at exactly the frames that matter.

All AI calls go to an **OpenAI-compatible** provider, configured via environment
variables or a config file (see [Configuration reference](#configuration-reference)).
Input videos can be anything FFmpeg decodes, including `.mp4`, `.mov`, `.mkv`,
and `.webm`.

---

## Global behavior

- In a source checkout, prefix copy-paste commands with `uv run`, for example
  `uv run screenscribe review demo.mov`.
- If you installed the package or activated its virtualenv, the bare
  `screenscribe` command is equivalent.
- **No command** → `uv run screenscribe` opens an interactive prompt.
- **Video shortcut** → `uv run screenscribe demo.mov` is treated as
  `uv run screenscribe review demo.mov`.
- **Version** → `uv run screenscribe --version` (or `-V`) prints the version and
  exits.
- **Open config** → `uv run screenscribe --config` opens the config file in your
  editor (creating a default if missing).

---

## Commands

### `screenscribe review`

Analyze one or more screencasts and generate interactive review reports.

```bash
uv run screenscribe review VIDEOS... [OPTIONS]
```

**Arguments**

- `VIDEOS...` — one or more video files. Multiple files are processed with
  shared context across the batch.

**Options**

| Option | Default | Description |
|--------|---------|-------------|
| `--output`, `-o` | `<video>_review` next to the video | Output directory for screenshots and reports. |
| `--prompt`, `-P` | none | Append custom instructions to the semantic, semantic-prefilter, and vision prompts. |
| `--lang`, `-l` | `en` | Language code for transcription. |
| `--local` | off | Use a local STT server instead of the cloud provider. |
| `--vision` / `--no-vision` (alias `--no-vlm`) | on | Skip visual/screenshot analysis. Semantic LLM detection still runs. |
| `--json` / `--no-json` | on | Save the JSON report. |
| `--markdown` / `--no-markdown` (`--md`) | on | Save the Markdown report. |
| `--html` / `--no-html` | on | Save the interactive HTML report. |
| `--embed-video` | off | Embed the video as base64 in the HTML report (only for files < 50 MB). |
| `--keywords-file`, `-k` | global file | Per-run keywords YAML. Keywords are always-on AI hints (never replace the LLM, safe when empty); overrides the global `~/.config/screenscribe/keywords.yaml`. |
| `--resume` | off | Resume from a previous checkpoint if available. |
| `--force` | off | Force reprocessing and overwrite the existing review instead of versioning. |
| `--estimate` | off | Show a time estimate (from video duration) without processing. |
| `--dry-run` | off | **Not free.** Still runs paid transcription (STT, unless `--local`) and LLM issue detection, then stops before writing reports. For a zero-cost preview use `--estimate` instead. |
| `--skip-validation` | off | Skip the model-availability check (faster start, may fail mid-pipeline). |
| `--serve` / `--no-serve` | **on** | Start an HTTP server and open the report in the browser after processing. |
| `--port`, `-p` | `8765` | Port for the review HTTP server. |
| `--verbose`, `-v` | off | Show detailed progress and debug information. |

> **The interactive HTML report is on by default.** `review` emits JSON +
> Markdown + HTML and opens the report in your browser unless you opt out. Use
> `--no-html` to skip HTML entirely, and `--no-serve` to write files without
> launching the browser.

**Detection**

Detection is always the **semantic pre-filter**: the LLM reads the whole
transcript and selects the actionable moments. There is no keyword-only or
no-LLM mode.

Your [keywords](#screenscribe-keywords) are passed to that LLM as **always-on
hints** — the words and phrases your team uses to describe problems.
They are hints, not rules: the model still judges context and negation, never
auto-creates a finding from a keyword alone, and an empty or missing dictionary
is a safe no-op. Use the global file by default, or `--keywords-file` for a
per-run dictionary.

**Examples**

```bash
uv run screenscribe review demo.mov
uv run screenscribe review clip1.mov clip2.mov clip3.mov
uv run screenscribe review ./recordings/session.mov --no-serve
uv run screenscribe review demo.mov --force --keywords-file my-keywords.yaml
uv run screenscribe review demo.mov --no-vision           # skip the VLM step
uv run screenscribe review demo.mov --lang en --prompt "Focus on accessibility issues"
uv run screenscribe review demo.mov --estimate            # just the time estimate
```

> `review` and `transcribe` require an **audio track**. On a silent recording
> they fail fast and point you to `uv run screenscribe analyze` (vision-only). See
> [Troubleshooting](#no-audio-track).

---

### `screenscribe analyze`

Start the interactive, human-first analysis dashboard. screenscribe boots a
local FastAPI server (via uvicorn) and opens a browser with a video player.

```bash
uv run screenscribe analyze VIDEO [OPTIONS]
```

**Arguments**

- `VIDEO` — path to the video file to analyze interactively.

**Options**

| Option | Default | Description |
|--------|---------|-------------|
| `--port`, `-p` | `8766` | Port for the analysis server. |
| `--lang`, `-l` | `en` | Language code for voice transcription and the default dashboard language. The PL/EN toggle controls the UI and the language of new frame analyses. |
| `--keywords-file`, `-k` | global file | Per-run keywords YAML. In `analyze` the active keywords reach the live server session and are passed to the AI as hints when it interprets your marker comments and voice notes (never replaces the LLM, safe when empty). |

**What you can do in the dashboard**

- Watch the video and pause at interesting moments.
- Record voice comments describing issues (transcribed in-browser via STT).
- Mark frames for AI analysis.
- Trigger VLM analysis on a single marker or on all pending markers at once.
- Finalize the session to analyze everything and produce an export payload.
- Export findings as JSON, or download a Markdown report.

This is the recommended mode when you want to **guide** the AI rather than let
it process the whole video. Because it is frame-driven, it also works on
recordings that have **no audio track** — mark frames and add optional text or
voice notes for an interactive vision-only review.

The server validates that a vision API key is configured before starting. Press
`Ctrl+C` to stop the server and exit.

**Examples**

```bash
uv run screenscribe analyze demo.mov
uv run screenscribe analyze demo.mov --port 9000
uv run screenscribe analyze demo.mov --lang pl
```

---

### `screenscribe transcribe`

Transcribe a video's audio to text, with no analysis.

```bash
uv run screenscribe transcribe VIDEO [OPTIONS]
```

**Arguments**

- `VIDEO` — path to the video file.

**Options**

| Option | Default | Description |
|--------|---------|-------------|
| `--output`, `-o` | stdout | Output file for the transcript. If omitted, the transcript prints to the console. |
| `--lang`, `-l` | `en` | Language code for transcription. |
| `--local` | off | Use a local STT server. |

**Examples**

```bash
uv run screenscribe transcribe demo.mov
uv run screenscribe transcribe demo.mov -o transcript.txt
uv run screenscribe transcribe demo.mov --local --lang en
```

---

### `screenscribe preprocess`

Build a transcript-first artifact bundle — the **non-AI handoff lane**. It
extracts audio, transcribes it, writes stable transcript artifacts, and stops
before any semantic or vision analysis. Ideal for handing transcripts to a
downstream model or agent.

```bash
uv run screenscribe preprocess VIDEO [OPTIONS]
```

**Arguments**

- `VIDEO` — path to the video file.

**Options**

| Option | Default | Description |
|--------|---------|-------------|
| `--output`, `-o` | `<video>_preprocess` next to the video | Output directory for the bundle. |
| `--lang`, `-l` | `en` | Language code for transcription. |
| `--local` | off | Use a local STT server. |
| `--audio` / `--no-audio` | on | Include the extracted `audio.mp3` in the bundle. |
| `--force` | off | Reuse the output directory even if a preprocess bundle already exists (otherwise a new `_2`, `_3`, … version is created). |

**Examples**

```bash
uv run screenscribe preprocess demo.mov
uv run screenscribe preprocess demo.mov -o ./demo_preprocess
uv run screenscribe preprocess demo.mov --no-audio --lang en
```

**Bundle contents**

- `transcript.txt` — plain text transcript.
- `transcript.timestamped.txt` — transcript with timestamps.
- `transcript.segments.json` — structured segments.
- `transcript.vtt` — WebVTT subtitles.
- `preprocess.json` — manifest (language, duration, timeline-coverage stats,
  word/segment counts, artifact paths).
- `audio.mp3` — the extracted audio (unless `--no-audio`).

---

### `screenscribe config`

Manage configuration and API keys. The config file lives at
`~/.config/screenscribe/config.env`.

```bash
uv run screenscribe config [OPTIONS]
```

**Options**

| Option | Description |
|--------|-------------|
| `--show` | Display the current configuration (keys are masked). |
| `--init` | Create a default config file (prompts before overwriting). |
| `--set-key KEY` | Save an API key into the config file. |

With no options, `config` prints a short usage hint.

> Editing your keywords lives under `screenscribe keywords`
> (see below), not `config`. The dictionary is global at
> `~/.config/screenscribe/keywords.yaml`, not per-directory.

**Examples**

```bash
uv run screenscribe config --show
uv run screenscribe config --init
uv run screenscribe config --set-key YOUR_API_KEY
```

`--show` lists the active config source, masked API keys, the STT/LLM/vision
endpoints and models, and the processing options (language, semantic, vision).

---

### `screenscribe keywords`

**Keywords are always-on hints for the AI** — the words and phrases
your team uses to describe problems (e.g. "klikam i nic" = bug, "potworek" = UI,
"za ciężkie" = perf). During detection they are passed to the LLM as hints; they
never replace the semantic analysis, never auto-create a finding on their own,
and an empty or missing dictionary is a safe no-op. They are used by default if
present.

The active dictionary is a single global file at
`~/.config/screenscribe/keywords.yaml`, grouping phrases under six categories
(`bug`, `change`, `ui`, `performance`, `accessibility`, `other`). It may mix
languages. A built-in default is used until you create your own.

```bash
uv run screenscribe keywords [COMMAND]
```

**Subcommands**

| Subcommand | Description |
|------------|-------------|
| `init` | Create the global keywords file from the built-in defaults (prompts before overwriting). |
| `edit` | Open the global keywords file in `$EDITOR` (creating it from defaults first if missing). |
| `add CATEGORY "PHRASE"` | Append one phrase to a supported category; creates the file if missing; never duplicates. |
| `list` | Show the active dictionary: source (default/global/custom), per-category counts, and sample phrases. |

**Examples**

```bash
uv run screenscribe keywords init
uv run screenscribe keywords edit
uv run screenscribe keywords add bug "klikam i nic"
uv run screenscribe keywords add performance "za ciężkie"
uv run screenscribe keywords list
```

For a one-off run with a different keywords file, pass `--keywords-file /path` to
`review` or `analyze` instead of editing the global file.

---

### `screenscribe version`

```bash
uv run screenscribe version      # full version line
uv run screenscribe --version    # short form (also -V)
```

---

## Configuration reference

screenscribe reads configuration from (in order) a config file and then
environment variables, where **environment variables always win**.

**Config file locations** (first existing file wins):

1. `~/.config/screenscribe/config.env` (primary user config)
2. `~/.screenscribe.env` (alternative user config)
3. `/etc/screenscribe/config.env` (system-wide)

A local `.env` is **not** auto-loaded; use environment variables for per-run
overrides.

Bring an API key from an OpenAI-compatible provider — this is the self-serve
onboarding path. For OpenAI (or any other OpenAI-compatible provider), set the
key and point the endpoints or `SCREENSCRIBE_API_BASE` at that provider (see
[Switching to OpenAI](#switching-to-openai-byo) below). LibraxisAI is the
first-party default endpoint, so a LibraxisAI key works without endpoint
changes.

**Billing is bring-your-own-key (BYOK).** screenscribe never proxies or resells
AI capacity: you supply your own key and you pay your provider directly —
LibraxisAI (the default) or OpenAI — for the STT, LLM, and vision calls a run
makes. There is no screenscribe account or charge in between; the tool only
forwards requests to the endpoints you configure.

### API keys

| Variable | Maps to | Notes |
|----------|---------|-------|
| `SCREENSCRIBE_API_KEY` | generic key (all endpoints) | First non-empty key wins. |
| `OPENAI_API_KEY` | LLM + vision keys | Convenience for OpenAI users. |
| `LIBRAXIS_API_KEY` | STT key (and generic fallback) | Convenience for LibraxisAI users. |
| `SCREENSCRIBE_STT_API_KEY` | STT key | Explicit per-endpoint (highest priority). |
| `SCREENSCRIBE_LLM_API_KEY` | LLM key | Explicit per-endpoint. |
| `SCREENSCRIBE_VISION_API_KEY` | vision key | Explicit per-endpoint. |

Per-endpoint keys fall back to the generic `api_key` when empty, so a single
`SCREENSCRIBE_API_KEY` is enough for a single-provider setup.

### Endpoints

| Variable | Maps to | Notes |
|----------|---------|-------|
| `SCREENSCRIBE_API_BASE` | base URL | Derives the three endpoints with standard paths (only those still at defaults). |
| `LIBRAXIS_API_BASE` | base URL | Alias for the above. |
| `SCREENSCRIBE_STT_ENDPOINT` | STT endpoint | Full URL, used as-is. |
| `SCREENSCRIBE_LLM_ENDPOINT` | LLM endpoint | Full URL, used as-is. |
| `SCREENSCRIBE_VISION_ENDPOINT` | vision endpoint | Full URL, used as-is. |

Defaults (LibraxisAI):

- STT: `https://api.libraxis.cloud/v1/audio/transcriptions`
- LLM: `https://api.libraxis.cloud/v1/responses`
- Vision: `https://api.libraxis.cloud/v1/responses`

The LLM and vision endpoints use the **Responses API** (`/v1/responses`), which
enables response-ID chaining across pipeline stages. To use OpenAI, set
`SCREENSCRIBE_API_BASE=https://api.openai.com` (or set each endpoint explicitly).

#### Switching to OpenAI (BYO)

screenscribe defaults to the **LibraxisAI** first-party provider. To run against
**OpenAI** instead, set the base URL **and** the key together — this is a single
coherent step, not two independent options:

```bash
export SCREENSCRIBE_API_BASE=https://api.openai.com
export SCREENSCRIBE_API_KEY=YOUR_OPENAI_KEY
```

Use `SCREENSCRIBE_API_KEY` here, not `OPENAI_API_KEY`: the latter maps only to the
LLM and vision endpoints, leaving the STT key empty, so a `review` run (which
transcribes audio) would fail at the STT step. `SCREENSCRIBE_API_KEY` is the
generic key that covers all three endpoints (LLM, vision, STT).

Setting only the key (an `sk-...` value) while the endpoints are still on the
LibraxisAI default is a **key/endpoint mismatch**: screenscribe does not
re-route silently, so it emits a mismatch warning and blocks the run rather than
sending your OpenAI key to the wrong provider. Always set both variables when
moving to a new provider.

### Models

| Variable | Default | Notes |
|----------|---------|-------|
| `SCREENSCRIBE_STT_MODEL` | `whisper-1` | OpenAI-Whisper-compatible. |
| `SCREENSCRIBE_LLM_MODEL` | `programmer` | LibraxisAI default — change to your provider's model (e.g. `gpt-4o`). |
| `SCREENSCRIBE_VISION_MODEL` | `programmer` | LibraxisAI default — change to your provider's vision model. |

### Processing options

| Variable | Default | Notes |
|----------|---------|-------|
| `SCREENSCRIBE_LANGUAGE` | `en` | Default transcription language (`pl` for Polish). |
| `SCREENSCRIBE_VISION` | `true` | Enable visual/screenshot (VLM) analysis (`false` = LLM-only; semantic detection still runs). |
| `SCREENSCRIBE_LLM_MERGE` | `true` | Semantic LLM-merge pass that dedups cross-category paraphrases after the cheap heuristic dedup (`false`/`0`/`no` = heuristic-only dedup). A missing LLM API key also makes it a no-op. |

### Optional STT fallback (opt-in)

A second STT provider, tried **only** if the primary STT endpoint fails (e.g. a
429 rate limit). Off by default because it routes your audio to another
provider. Set all three to enable:

| Variable | Notes |
|----------|-------|
| `SCREENSCRIBE_STT_FALLBACK_ENDPOINT` | Full STT URL of the second provider. |
| `SCREENSCRIBE_STT_FALLBACK_API_KEY` | API key for the fallback provider. |
| `SCREENSCRIBE_STT_FALLBACK_MODEL` | Defaults to `whisper-1` if unset. |

---

## Local and offline STT

screenscribe can transcribe entirely on your own machine, with no calls to a
cloud provider, by pointing speech-to-text (STT) at a local server. There are
two ways to do this:

1. **`--local` flag** — a built-in shortcut that targets a fixed loopback URL.
2. **`SCREENSCRIBE_STT_ENDPOINT`** — point at any OpenAI-compatible STT server
   (local or remote) you run yourself.

### The `--local` flag

`--local` is accepted by `screenscribe transcribe` and `screenscribe review`.
When set, STT requests go to the hard-coded loopback endpoint:

```
http://localhost:7237/transcribe
```

`--local` takes precedence over `SCREENSCRIBE_STT_ENDPOINT`. Because the target
is loopback, screenscribe sends **no** `Authorization` header and does **not**
require an API key for STT — so you can run fully offline.

#### Request shape (what your server receives)

screenscribe POSTs a `multipart/form-data` request:

- **File field:** `audio` (for loopback/local URLs). A custom cloud endpoint
  receives the field as `file` instead — see below.
- **Form fields:** `model` (e.g. `whisper-1`), `language` (e.g. `en`),
  `response_format` (`verbose_json` for the full `transcribe` path).
- **No `Authorization` header** for `--local` / loopback targets.

#### Response shape (what your server must return)

Reply with JSON. screenscribe reads the following fields:

```json
{
  "text": "Full transcript text.",
  "language": "en",
  "segments": [
    {
      "id": 0,
      "start": 0.0,
      "end": 3.2,
      "text": "Full transcript text.",
      "no_speech_prob": 0.01
    }
  ]
}
```

- `segments` — list of cues with `id`, `start`, `end` (seconds), `text`, and
  `no_speech_prob`. Returning real `start`/`end` is strongly recommended: if you
  return only `text` and omit `segments`, screenscribe synthesizes a single
  segment from a ~150-words-per-minute estimate and flags the timestamps as
  synthetic (less accurate seeking in the report).
- `text` — the full transcript (used as a fallback and for the summary).
- `language` — optional; echoes/overrides the detected language.
- `response_id` — optional; if present it is reused to chain the follow-up LLM
  call.

#### Minimal example server

A tiny FastAPI server that satisfies the contract above (replace the stub with
your real Whisper/faster-whisper inference):

```python
# local_stt.py — run: uvicorn local_stt:app --host 127.0.0.1 --port 7237
from fastapi import FastAPI, UploadFile, Form

app = FastAPI()


@app.post("/transcribe")
async def transcribe(
    audio: UploadFile,
    model: str = Form("whisper-1"),
    language: str = Form("en"),
    response_format: str = Form("verbose_json"),
):
    raw = await audio.read()
    # ... run your local STT model on `raw` and build real segments ...
    return {
        "text": "your transcript here",
        "language": language,
        "segments": [
            {"id": 0, "start": 0.0, "end": 3.2,
             "text": "your transcript here", "no_speech_prob": 0.01}
        ],
    }
```

#### End-to-end example

```bash
# 1. start your local STT server (separate terminal)
uvicorn local_stt:app --host 127.0.0.1 --port 7237

# 2. transcribe a recording against it, fully offline
uv run screenscribe transcribe demo.mov --local --lang en
```

### Custom OpenAI-compatible endpoint (`SCREENSCRIBE_STT_ENDPOINT`)

To use your own server on a different host/port, or any OpenAI-compatible STT
service, set `SCREENSCRIBE_STT_ENDPOINT` to its full transcription URL instead
of using `--local`:

```bash
export SCREENSCRIBE_STT_ENDPOINT=https://stt.internal.example.com/v1/audio/transcriptions
```

This path uses the OpenAI-compatible `/v1/audio/transcriptions` contract: the
file is sent as the `file` field, and an `Authorization: Bearer` header is sent
when `SCREENSCRIBE_API_KEY` is set and the endpoint is not a loopback address.
See [Configuration reference](#configuration-reference) for the related
endpoint/key variables.

---

## Workflows

### Automatic review pipeline

The fastest path: record, narrate, review.

```bash
uv run screenscribe config --init
uv run screenscribe config --set-key YOUR_API_KEY
uv run screenscribe review demo.mov
```

screenscribe transcribes, finds actionable moments, captures screenshots,
confirms them with the vision model, writes JSON/Markdown/HTML reports, and
opens the HTML report in your browser. Re-running preserves the prior report as
`_2`, `_3`, …; pass `--force` to overwrite instead.

For a batch with shared context:

```bash
uv run screenscribe review session1.mov session2.mov session3.mov
```

### Interactive analyze (human-first)

When you want to guide the AI to specific frames:

```bash
uv run screenscribe analyze demo.mov
```

Scrub the video, mark the frames that matter, add voice or text notes, then run
VLM analysis on those markers (one at a time, all pending at once, or via the
finalize action). Export the result as JSON or download a Markdown report. This
mode also works on **silent** recordings.

### Transcript-first preprocess (agent handoff)

When you want a clean transcript bundle before (or instead of) AI analysis:

```bash
uv run screenscribe preprocess demo.mov
```

Hand the resulting `transcript.txt` / `transcript.segments.json` /
`preprocess.json` to a downstream model or agent. Use `--no-audio` to keep the
bundle text-only, and `--force` to reuse a directory in place.

---

## Output artifacts

### Review reports

Written per video into the output directory (default `<video>_review`):

- `<video>_report.json` — findings, transcript, transcript segments, executive
  summary, and any errors. Machine-readable for ticketing or agent workflows.
- `<video>_report.md` — Markdown report designed to be readable by humans and
  AI fixer agents.
- `<video>_report.html` — interactive HTML report (video player, subtitle
  sync, screenshot annotations, human review workflow).
- Captured screenshots for each finding.
- `TODO_<video>.md` — a Markdown task list you can export from the interactive
  HTML report ("Export TODO"), handy for dropping findings into a sprint.

### Preprocess bundle

See [preprocess bundle contents](#screenscribe-preprocess).

---

## Troubleshooting

### FFmpeg not found

screenscribe needs both `ffmpeg` and `ffprobe` on your `PATH`. If either is
missing it tells you the exact install command for your platform:

- macOS: `brew install ffmpeg`
- Debian/Ubuntu: `sudo apt install ffmpeg`
- Windows: `choco install ffmpeg`

### STT rate-limited or at capacity (HTTP 429)

If the speech-to-text service is rate-limited, screenscribe reports it clearly
(no raw traceback) and explains that this is a temporary server-side limit, not
a problem with your video. You can:

- wait a moment and re-run with `--resume` to retry from the checkpoint, or
- point `SCREENSCRIBE_STT_ENDPOINT` at a different OpenAI-compatible STT
  endpoint, or
- configure the [opt-in STT fallback](#optional-stt-fallback-opt-in) so a second
  provider is tried automatically on failure.

Other STT errors are handled the same friendly way: `500/502/503/504` (temporary
server error → retry with `--resume`), `401/403` (credentials rejected → check
your STT key/endpoint), and network failures (check connection and endpoint).

### No audio track

`review` and `transcribe` require audio. On a silent recording they fail fast
with a clear message and suggest the vision-only path:

```bash
uv run screenscribe analyze <video-path>
```

In `analyze`, mark frames manually and add optional text/voice notes for an
interactive, vision-only review.

### Transcript timeline coverage warning

For recordings longer than 5 minutes, screenscribe checks that the STT
timestamps cover enough of the video (minimum **80%** coverage). If coverage is
low, it warns that end-of-video screenshots may not line up with findings —
usually because STT timestamps drifted or compressed on that recording. The
review still continues; for tighter alignment, try chunked transcription or a
shorter recording. The same coverage stats are recorded in the `preprocess.json`
manifest.

### Model availability / slow start

`review` validates that the configured models are available before processing.
Use `--skip-validation` for a faster start (at the risk of failing later in the
pipeline). Use `--estimate` for a zero-cost preview of time and scope (no API
calls). `--dry-run` is **not** free — it still runs paid transcription and LLM
detection, only skipping the report artifacts at the end.

---

Built by [Vetcoders](https://github.com/vetcoders).
