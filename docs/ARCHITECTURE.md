# Architecture overview

A map of the `screenscribe` package for developers and AI agents entering the
repository for the first time. It complements [shared-shell.md](shared-shell.md)
(which covers only the HTML shell) by describing the whole pipeline, the two
servers, the report layer, the CLI, and the shared hubs whose blast radius you
must respect before editing.

Scope: what each part does and where it lives. For user-facing commands, flags,
and configuration see [../USAGE.md](../USAGE.md); for the gate contract see
[DEFINITION_OF_DONE.md](DEFINITION_OF_DONE.md) and
[COVERAGE_SCOPE.md](COVERAGE_SCOPE.md).

## The pipeline

A `review` run turns a narrated screencast into a structured report through a
fixed sequence of stages. Each stage is checkpointed, so a `--resume` run
restarts from the last completed stage rather than the top.

```
  video file
      |
      v
  [ audio ]        audio.py          extract the audio track via ffmpeg
      |
      v
  [ transcribe ]   transcribe.py     speech-to-text (STT); optional second
      |                              provider tried only on primary failure
      v
  [ detect ]       semantic_filter.py  LLM semantic pre-filter: read the full
      |            (+ detect.py,        transcript, surface the actionable
      |             keywords.py)        moments as detections
      v
  [ screenshots ]  screenshots.py    capture frames at each moment's timestamp
      |
      v
  [ analyze ]      unified/          per-moment analysis: LLM reasoning + VLM
      |                              (vision) confirmation against the frame,
      |                              concurrent, dedup + optional LLM-merge
      v
  [ report ]       report/, html_pro/  JSON / Markdown / interactive HTML
```

Orchestration for this whole chain lives in `review_pipeline.py::run_review`
(audio -> transcribe -> detection -> screenshots -> unified analysis -> report),
which also owns checkpoint restore/save and the empty-state / failure handling.
The default transcript source is `auto`: an audio track uses STT, while a
recording without one uses interval-based frame OCR. `transcript_sources.py`
validates/resolves this choice; its protocol registry is an extension helper,
not the runtime dispatcher. OCR is screen evidence, not narrator instructions.
Checkpoint schema v3 binds the source/interval, preset, vocabulary and prompt.
OCR cache entries are private and bind frame content, endpoint, model and
effective prompt.
The `analyze` mode is frame-driven instead of transcript-driven: the user marks
moments in the browser and the server runs the same unified analysis per marker
(see the servers section).

## Core modules

### Pipeline stages

| Module | Role |
|--------|------|
| `audio.py` | ffmpeg audio extraction (`FFmpegNotFoundError`, normalization). |
| `transcribe.py` | STT: `Segment`, audio chunking, upload to the transcription API, fallback provider. **Hub — see blast radius below.** |
| `detect.py` | Detection / keyword-matching primitives (`Detection`, `get_keywords_config`). |
| `keywords.py` | Keyword configuration used as hints for the semantic pre-filter. |
| `semantic_filter.py` | LLM semantic pre-filter that turns the transcript into detections (the "detection" stage; `pois_to_detections`). |
| `screenshots.py` | Frame extraction at moment timestamps. |
| `preprocess.py` | Optional video/audio preprocessing before a run. |
| `vtt_generator.py` | WebVTT subtitle generation for the report player. |
| `checkpoint.py` | Resume/checkpointing of long runs (atomic writes, per-stage completion). |
| `frame_ocr.py`, `transcript_sources.py` | Frame OCR and validated audio/OCR source selection. |
| `presets/` | Programming, casual, medical, veterinary and custom vocabularies/prompts. |

### Analysis engine (`unified/`)

The multi-provider LLM/VLM analysis engine (10 files). Entry points:
`analyze_finding_unified`, `analyze_finding_unified_streaming`,
`analyze_all_findings_unified`, and the `UnifiedFinding` model.

- `orchestrator.py` — concurrency (worker pool, stagger delay, failure-ratio guard).
- `analyze_one.py` — single-finding streaming analysis.
- `response_parsing.py`, `wire.py` — provider response parsing and wire format.
- `dedup.py`, `llm_merge.py` — heuristic dedup, then optional semantic LLM-merge
  (`SCREENSCRIBE_LLM_MERGE`) of cross-category paraphrases.
- `summaries.py`, `finding.py` — executive summaries and the finding data model.

`unified_analysis.py` is a legacy re-export facade over `unified/`; prefer
importing from `unified/` directly in new code.

### Supporting modules

| Module | Role |
|--------|------|
| `api_utils.py` | Retry/backoff and request-body building for OpenAI-compatible + chat-completions APIs. |
| `prompts.py` | LLM/VLM prompt templates. |
| `text_similarity.py` | Similarity scoring used by dedup. |
| `validation.py` | Input validation for CLI/API paths. |
| `config.py` | Global configuration (`ScreenScribeConfig`, paths, model defaults, env loading). **Hub — see blast radius below.** |

## Servers

Two FastAPI servers back the interactive browser surfaces. They share helpers
but each registers its own routes.

| Server | Default port | Role |
|--------|--------------|------|
| `review_server.py` | `8765` | Serve and interactively review a generated report. |
| `analyze_server.py` | `8766` | Frame-driven `analyze` workspace: mark moments, per-marker analysis, finalize, export. |

- `server_common.py` — helpers shared by both servers (a partial dedup effort;
  a few routes — `GET /`, `GET /video`, `POST /api/stt` — still exist in both
  servers and must be kept in sync when patched).
- `server_security.py` — session token, authorization, and CORS for both servers.
- `agent/` — report-seeded chat, guarded tools, provider fallback and SSE.
  The browser applies proposed edits through the existing review state/save
  path and records the exact applied user request in reviewer notes. Cursor
  identity is bound to provider/protocol/host; report finding IDs do not become
  a shared conversation head.
  xAI uses stateless full history so trusted instructions can be repeated on
  every user/tool round.
  Manual captures persist their image path; cold start restores markers/results
  before the next browser save. Legacy image lookup stays within the report's
  `manual_frames/` directory and never writes the JSON during startup.
- `processing_provenance.llm` records an actually successful semantic request.
  Agent processor trust uses this receipt, not the currently configured
  endpoint list. Missing/legacy receipts establish no processor trust.

## Report and HTML layer

The report is a single self-contained HTML file: all CSS/JS/fonts are inlined as
`data:` URIs at generation time (a strict-CSP-friendly artifact that opens from
`file://`).

The client-rendered manual-moment card identifies human words/notes, the AI
paraphrase and additional vision-model suggestions as separate sections. This
presentation uses the existing transcript, notes, summary and issues fields;
it does not rewrite source data or change the model prompt.
Without human input, the summary is attributed to image analysis; the server's
synthetic capture description is never presented as a reviewer paraphrase.

| Package | Role |
|---------|------|
| `report/` | Output writers: `data.py` (finding/frame folding), `json_report.py`, `markdown_report.py`, `html_report.py`, `console.py`. |
| `html_pro/` | HTML renderer: `renderer.py` (template engine, server-side i18n `_t()`), `assets.py` (CSS/JS/favicon loaders as data-URIs), `data.py` (report-id / JSON prep). |
| `shell/` | Declarative shared shell: `surface.py` (`REVIEW_SURFACE`, `ANALYZE_SURFACE`), `renderer.py` (`render_surface`). See [shared-shell.md](shared-shell.md). |
| `html_pro_assets/` | Source frontend assets (JS, CSS, HTML templates/partials) that are inlined into the report at generation time. |

## CLI

`screenscribe` is a Typer app. The entry point is `screenscribe.bootstrap:main`
(see `pyproject.toml`); `cli.py` defines the commands, with logic split across
`cli_estimate.py`, `cli_messages.py`, `cli_paths.py`, `cli_reporting.py`, and
`cli_serve.py`.

Commands: `review`, `analyze`, `transcribe`, `preprocess`, `keywords`, `config`,
`auth`, `tts`, `version`. `account_auth/` implements device-code sign-in and
private token storage; xAI accounts can supply the API bearer, while OpenAI
account sign-in remains identity-only. See [../USAGE.md](../USAGE.md).

The report/ZIP handoff keeps source segments, source kind, reviewer notes and
review state alongside model proposals. `source_role=screen_text` distinguishes
OCR from narrator feedback. Shared `count_report_categories` powers active
preset counts without replacing the legacy JSON summary keys.

## Internationalization: three sources

User-facing text is resolved by **three** independent i18n stores, EN + PL. A
new key must be added to every source whose surface renders it (parity is
enforced by `tests/test_i18n_no_hardcoded_dom.py`):

1. `shell/renderer.py::_SERVER_I18N` — server-side render of the shared shell
   chrome (tabs, header cells, language toggle).
2. `html_pro/renderer.py::_I18N` (via `_t()`) — server-side render of the review
   report body (findings, verdicts, labels).
3. `html_pro_assets/scripts/i18n.js` — the client runtime that resolves
   `data-i18n` anchors in the browser.

See [shared-shell.md](shared-shell.md) for the "adding a tab/panel safely" rule.

## Hubs and blast radius

Two modules are imported almost everywhere. Changing their public shape has a
wide blast radius — check consumers (`loct impact`, or grep) before editing:

- `config.py` — ~50 importers, no internal dependencies. A stable data/env hub:
  high fan-in, but a flat container, so risk is *reach*, not fragility.
- `transcribe.py` — ~51 importers and real business logic. Changing the
  `Segment` signature ripples across the whole tree.

Note also that several symbols have legacy import aliases (`UnifiedFinding`,
`Segment`, `Detection`, `render_html_report_pro`). Prefer the canonical path in
new code to avoid widening the drift surface.

## Tests

- `tests/` — unit tests (Python) plus node-vm JS runtime smoke tests
  (`test_f0_*`, `test_review_app_*`) that exercise the frontend logic headlessly.
- `tests/e2e/` — Playwright end-to-end suite (`make e2e-review`), opt-in via
  `--run-e2e`; drives a real report in headless Chromium.

The single release gate is `make verify` (`scripts/ss_verify.py`): no-junk,
secrets, leak-scan, branding, compile, lint + format + types, security, tests +
coverage floor, build, and an isolated-wheel report render. See
[DEFINITION_OF_DONE.md](DEFINITION_OF_DONE.md) and
[COVERAGE_SCOPE.md](COVERAGE_SCOPE.md) for exactly what it does and does not cover.
