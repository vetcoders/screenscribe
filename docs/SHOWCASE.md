# screenscribe Showcase

screenscribe transforms video commentary into structured engineering artifacts. It's built for developers, QA engineers, and product owners who want to "just speak" their findings and get a professional report.

## Key Features

### 1. Unified VLM Analysis
screenscribe doesn't just transcribe; it **sees**. By sending both the transcript segment and a captured frame to a Vision-Language Model, it can confirm if what you said matches what's on screen.

### 2. Interactive Review Server
The `review` command launches a local web application that allows you to:
- Play the video with synchronized transcript.
- Split the player and findings into a dedicated review window for dual-screen workflows.
- Annotate screenshots with drawing tools.
- Capture manual frames on the fly.
- Transcribe voice notes directly in the browser.
- **Persist everything** back to your project workspace.

### 3. Transcript-First Workflow
With the `preprocess` command, you can extract audio and transcribe it *before* running expensive AI analysis. This allows for a fast, iterative review of the narrative.

## How It Works

1. **Record**: Capture a video of your app and narrate the issues.
2. **Analyze**: Run `screenscribe review my_video.mp4`.
3. **Review**: Open the interactive report, detach review into a second window if needed, refine findings, and add annotations.
4. **Export**: Get a `<video>_report.json` for your ticket system or a `TODO_<base>.md` for your sprint.

## Sample Artifacts

A neutral, fictional example (a placeholder review of a made-up "Acme Notes" web
app) is checked into [`examples/`](../examples/) so you can see real output
without running the pipeline. It contains no real recordings, keys, or personal
data. See [`examples/README.md`](../examples/README.md) for details and how to
regenerate it deterministically.

[![screenscribe example report — interactive dashboard with an executive summary and synchronized transcript](showcase/example_report.png)](../examples/example_report.html)

*A rendered view of the [example report](../examples/example_report.html). The demo
subject and finding wording are a neutral DRAFT pending brand/content sign-off;
regenerate the image deterministically with `uv run python examples/generate_hero.py`.*

### Interactive HTML Report
A modern, responsive dashboard with a synchronized video player and annotated
findings. The self-contained example report is generated locally with
`uv run python examples/generate_example.py` (it inlines vendored JS, so it is
produced on demand rather than tracked) — then opens directly in a browser, no
server required. See [`examples/README.md`](../examples/README.md).

### JSON Report
A machine-readable audit trail of every issue detected, analyzed, and reviewed.
**[See the example JSON →](../examples/example_report.json)**

### WebVTT Subtitles
Searchable transcript synchronized with the video, compatible with standard
players. **[See the example transcript →](../examples/example_transcript.vtt)**

---
Built by [Vetcoders](https://github.com/vetcoders).
