# Example artifacts

These are **neutral, fictional** example outputs — a placeholder demo review of a
made-up "Acme Notes" web app. They contain no real recordings, no API keys, and
no personal data. They exist so you can see what screenscribe produces without
running the full pipeline.

| File | What it is | Tracked? |
|------|------------|----------|
| [`example_report.json`](example_report.json) | The machine-readable report (same schema a real run writes). | yes |
| [`example_transcript.vtt`](example_transcript.vtt) | The WebVTT transcript track. | yes |
| `example_report.html` | The interactive, self-contained HTML report. | generated locally |

## Generating the HTML report

The interactive HTML inlines vendored minified JS (JSZip) and is large, so it is
**produced on demand** rather than tracked in git. Generate all three artifacts
deterministically (no API, no network, no video) with:

```bash
uv run python examples/generate_example.py
```

Then open the result directly in a browser — it is fully self-contained (inline
CSS/JS, no server, no video, no network):

```bash
open examples/example_report.html      # macOS
# or just double-click it in your file manager
```

Re-running the generator overwrites the artifacts identically (the
`generated_at` timestamp is pinned), so the example stays reproducible.

> Note: the content here is a placeholder default for review. The final showcase
> sample (demo subject, finding wording, hero screenshot) is an operator/brand
> decision — see `../docs/SHOWCASE.md`.
