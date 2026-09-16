# Changelog

## [0.1.20.dev0] - Unreleased

- **Added: `SCREENSCRIBE_LLM_REASONING_EFFORT=none`.** `none` is now a legal
  reasoning-effort value alongside `minimal|low|medium|high`; it turns
  reasoning off on providers that support it (OpenAI Responses, LibraxisAI)
  and is sent to the wire verbatim as `reasoning.effort = "none"`. `medium`
  stays the default, and truly invalid values (e.g. `off`) still warn and
  fall back to `medium`.

- **Added: annotation objects in the HTML report editor can be selected, moved,
  resized, recoloured, and deleted.** Each annotation now has a stable `id`
  (legacy saves without one are migrated on load). The lightbox toolbar gains
  Select / Delete plus stroke and font-size controls; Delete/Backspace removes
  the active object. Overlay position re-reads `getActualImageRect` on every
  pointer sample and on scroll/resize so a rectangle drawn next to on-screen
  text no longer lands in a different place after the player layout shifts.
- **Added: transcript source abstraction for `review` — audio STT or frame OCR.**
  New flags `--transcript-source auto|audio|ocr`, `--no-audio` (alias for
  `ocr`), and `--frame-interval <s>` (default 5). The transcript is now an
  abstraction over where timestamped segments come from: the classic STT path
  (`audio`, unchanged) or VLM OCR of frames taken every N seconds (`ocr`),
  where visually identical frames are deduplicated before any paid call and
  every surviving frame with readable text becomes one segment in the exact
  STT shape (`start`, `end`, `text`). The default `auto` probes each video and
  routes silent recordings to OCR, so a recording without an audio track now
  reaches semantic analysis and the report instead of dying at the
  "has no audio track" gate; an explicit `--transcript-source audio` keeps
  that readable fail-fast error. `--prompt` instructions reach the OCR stage
  and all analysis stages, OCR results are cached per frame content hash, and
  the rest of the pipeline (semantic pre-filter, screenshots, unified VLM
  analysis, reports, response chaining) works unchanged on OCR segments.
- **Added: `review --preset` — analysis presets for new domains.** A preset
  bundles the keyword dictionary, the finding categories, and a prompt
  fragment (who the viewer is, what counts as a finding). Shipped presets:
  `programming` (the default — bit-for-bit the historical behavior), `casual`
  (informal product feedback), `medical`, and `veterinary` (clinical
  consultations, procedures, and clinic systems such as Vista, with
  `finding/observation/risk/followup/other` categories and Polish+English
  dictionaries). `--preset custom` builds the profile from your own
  `--keywords-file` (its top-level keys become the categories) and fails with
  instructions when the flag is missing. Keyword priority is now
  `--keywords-file` > global file > preset dictionary > built-in default;
  non-default presets record `preset` (name + categories) in the JSON report
  and their category badges render in the HTML report.
- **Added: review-agent chat on the report server.** `POST /api/agent/chat/stream`
  (SSE) and `POST /api/agent/chat` continue a conversation about the loaded
  report with tools (`list_findings`, `get_transcript`, `seek`, `show_frame`,
  `get_report_summary`, optional `open_repo_file`). Primary provider is the
  configured LLM Responses endpoint (`previous_response_id` chaining); Anthropic
  is an optional fallback extra. Screen recordings are treated as secrets:
  `SCREENSCRIBE_AGENT_EGRESS` defaults to `deny` for `trust=external` hosts.
  xAI is external unless `SCREENSCRIBE_AGENT_PRIMARY_TRUST=internal`. The JSON
  report now stores `analysis_passes.unified_analysis.response_id` so the next
  review can resume the last VLM pass for free.
- **Added: floating screenscribe agent chat on the HTML review report.** A
  collapsed 『s』 chip docks in the corner; expanding it places a draggable
  panel beside the player (never over it) and streams `POST /api/agent/chat/stream`.
  Offline file:// reports show "run `screenscribe serve`" instead of a console
  error. Tool calls `seek` and `show_frame` jump the player and highlight the
  matching finding. Cut `w1-05-agent-floating`.

- **Fixed: 『s』 agent panel send, coverage, and empty error bubbles.** Enter
  sends (Shift+Enter keeps a newline; IME composition does not send);
  overlapping restored positions are discarded and the panel is recomputed
  beside the player (or docks as a side sheet that shrinks `.app-container`
  when no clear spot fits); an error turn paints the assistant bubble instead
  of leaving an empty one.
  A host that already analyzed the recording (STT/LLM/vision) is
  `trust=processor` and is kept under that default, so the xAI preset chats
  without an extra env var. `SCREENSCRIBE_AGENT_PRIMARY_TRUST=external` remains
  the opt-out; a fallback on a different host (e.g. Anthropic) is still skipped
  under `deny`. The JSON report now stores
  `analysis_passes.unified_analysis.response_id` so the next review can resume
  the last VLM pass for free.
- **Added: review-patch write tools on the review agent.** The agent can
  `set_verdict`, `set_severity`, `edit_finding`, and `add_finding`, or
  `propose_review` a plan for a broad request. Tools never write `report.json`;
  they return a `review_patch` / `review_plan` in the existing SSE `tool_result`
  envelope so the browser stays the single writer (existing `/api/save` lock and
  Undo/Reset). `review_finding_state` hydrates additive `summary_override` and
  `category_override`. Auth is unchanged: empty API key + signed-in xAI account
  bearer is enough. `merge_findings` / `unmerge_finding` return
  `{"unsupported": true}` until the panel grows a patch-callable merge.
- **Added: the 『s』 panel applies agent review patches and plans.** A
  `review_patch` in `tool_result` updates the finding card through the existing
  verdict/severity/notes setters, marks the report modified, and saves with the
  same `Zapisz recenzję` path (`resetGeneration` included). A `review_plan`
  renders per-op checkboxes with Apply/Cancel; Apply posts “Zastosowano N z M”
  into the chat. Offline `file://` reports announce that patches cannot be saved
  and never apply silently. Save 409/network failures show a Retry instead of
  looping. Merge ops stay unsupported. Cut `w2-02-review-panel`.

- **Internal: refresh runtime and development dependencies.** Updated the lockfile
  to the latest compatible releases, including mypy 2.3.1 and Rich 15.0.0.
  Normalized the development version to PEP 440's `0.1.20.dev0` spelling so
  project metadata, installed metadata, and this changelog agree.

- **Internal: bandit pre-commit hook runs from the project environment.** The
  remote `PyCQA/bandit` hook's pbr-based build ran `git describe` against our
  tags from inside git hooks and broke on the non-PEP440 recovery tag; the
  hook is now `repo: local` and calls the same `uv run bandit` used by
  `make verify`.

- **Fixed: STT models that reject `response_format=verbose_json` no longer abort
  the review.** The file transcription path (`review`, `transcribe`, and every
  chunk of a long recording) asks for `verbose_json` to get per-segment timing.
  OpenAI's `gpt-transcribe` / `gpt-4o-transcribe` family answers HTTP 400
  (`param=response_format`, `code=unsupported_value`), which previously killed
  the run at chunk 1/32 with "Speech-to-text failed (HTTP 400)". Screenscribe
  now retries that one request with `json`, marks the resulting timeline as
  synthetic, and remembers the refusal per endpoint+model for the rest of the
  process so a chunked run does not repeat the rejected request per chunk.
  Whisper-family models keep `verbose_json` and their real segment timing; any
  other 400 still fails loudly.

- **Fixed: `review -o <existing folder>` no longer mistakes an ordinary folder
  for a previous review.** A folder is a previous review only when it holds a
  `.screenscribe_cache/` checkpoint or this video's own `<video>_report.*`
  (legacy `report.json`/`report.html` still count); an unrelated
  `*_report.md` no longer qualifies. An existing ordinary folder is now used as
  a parent (`<folder>/<video>_review`, re-runs version inside it), matching
  batch mode, instead of creating a `<folder>_2` sibling. A path that does not
  exist, or a real previous review, behaves as before.
- **Fixed: `preprocess -o <existing folder>` follows the same folder contract.**
  Only a folder whose `preprocess.json` is screenscribe's own manifest counts as
  a previous bundle (a stray `transcript.txt` no longer does); any other existing
  folder is used as a parent (`<folder>/<video>_preprocess`), and an output
  directory that cannot be created shows the same "Output Directory Error".
  Like `review`, a file or non-empty non-bundle folder at `<video>_preprocess`
  is skipped for the next free version, `-o` naming an existing file is an
  error, and `--force` refuses (exit 1, nothing changed) a target that is not
  a screenscribe preprocess bundle. Running out of versions and failing to
  write a bundle file (permission denied, disk full) also print an "Output
  Directory Error" instead of a traceback; partial files are left in place.
  The output folder is reserved and checked for writability before audio
  extraction and transcription, so folder creation, reservation, writability
  and version-limit errors stop before any STT call; a bundle write failure
  (e.g. disk full) is still reported after transcription has run.
- **Fixed: `review --estimate` is read-only.** It prints the time table before
  any output-folder handling, so it no longer creates an empty output directory,
  shows the rerun prompt, or refuses a `--force` target.
- **Fixed: an output directory that cannot be created is a clear error.**
  Permission denied, read-only volumes and file-in-the-way paths now print an
  "Output Directory Error" with the path and reason and exit with code 1,
  instead of a raw traceback.
- **Fixed: `review` never writes into a folder screenscribe does not own.** An
  existing file or non-empty non-review folder at `<video>_review` is skipped
  (the next free `_2`, `_3`, … is used, without the overwrite/resume prompt),
  a version slot is used only when missing or empty, and running out of
  versions prints an "Output Directory Error" instead of a traceback.
  `--force` refuses (exit 1, nothing changed) when the target is a file or a
  non-empty folder that is not a screenscribe review, and a checkpoint cache it
  cannot remove is reported as an error instead of a traceback. The output
  folder is reserved right before use (created exclusively, re-checked, and
  probed for writability), so a slot taken in the meantime or a read-only
  folder stops with an error instead of being written into or crashing.
- **Fixed: semantic pre-filter failures name the real cause.** Provider error
  events inside a 200 response stream (`error`, `response.failed`,
  `response.incomplete`) are captured and shown with their message and code
  instead of "Empty response"; transient ones (server error, overload, rate
  limit) that arrive before any model output are retried, also for the
  per-finding vision stream. Empty streams and
  unreachable hosts (connect/TLS handshake timeout) get explicit reasons, and
  the "Issue Detection Failed" panel shows the LLM endpoint host.
- **Fixed: truncated LLM answers fail loudly instead of passing as results.**
  When a stream ends with `error`, `response.failed` or `response.incomplete`
  after some text arrived, the pre-filter now fails (no partial findings, not
  checkpointed as complete) even if that text parses, and per-finding analysis
  falls back instead of accepting the truncated answer.
- **Hardening: pre-filter failure reasons never include the endpoint URL.**
  HTTP errors are reported as status, host and a short provider message, and
  transport errors as type and host, so credentials or query parameters in a
  configured endpoint URL cannot leak into output. The same defense in depth now
  covers logs: retry messages, verbose endpoint and analysis-failure lines,
  summary/LLM-merge warnings, model-validation errors and config mismatch
  messages redact URLs (userinfo and query values masked, fragment dropped),
  including URLs inside provider-supplied error messages and response bodies
  (speech-to-text failure details included).
  Screenscribe never reads credentials from endpoint URLs; keys are sent only in
  the Authorization header.
- **Fixed: semantic pre-filter no longer hangs reasoning without an answer.**
  Root cause: the pre-filter sent no reasoning effort, so on a full transcript
  the default LLM reasoned in a loop for 11-20 minutes, emitted no text and
  ended with `response.failed`. All text-LLM Responses API requests (pre-filter,
  text-only finding analysis, executive summaries, LLM merge) now send
  `reasoning.effort`, default `medium`,
  configurable with the new `SCREENSCRIBE_LLM_REASONING_EFFORT`
  (`minimal`/`low`/`medium`/`high`); Chat Completions endpoints and the vision
  request are unchanged. An in-stream provider error is retried only if it
  arrives before the model streamed any output, so such a failure is reported
  once (with a hint to lower the effort) instead of being retried for close to
  an hour. Non-streaming summary and merge calls also treat a 200 response with
  status `failed` or `incomplete` as a failure (local summary / merge skipped)
  instead of using its partial text.


## [0.1.19] - 2026-08-23

- **Security: provider endpoints are classified by canonical DNS host boundaries.**
  OpenAI and LibraxisAI detection now parses the URL hostname and accepts only
  the provider's exact domain or a real subdomain after IDNA normalization, so
  lookalike hosts and Unicode-equivalent DNS separators cannot bypass the
  credential boundary. The development-only Semgrep dependency chain is also
  refreshed to patched MCP and cryptography releases; these tools are not
  included in the published runtime wheel.
- **Fixed: HTML Pro review changes can be safely undone or reset.** Human merge
  groups now expose Unmerge, preserve source review snapshots through chained
  merges, restore the survivor's pre-merge verdict without losing later notes,
  priorities, or annotations, distinguish actual survivor edits from the merged
  union after reload, and rebind visible annotation previews after the split.
  Reset review atomically returns to generated findings, clears the
  reviewer and manual review state, and reports success once the canonical JSON
  commit lands even if stale frame cleanup can only emit a warning. A monotonic
  reset generation is persisted across server restarts and now invalidates stale
  Save as well as delayed manual-frame Add/Analyze/note/priority and voice-note
  work without discarding annotation edits during ordinary same-generation
  synchronization. A stale Add still returns its generation conflict if
  best-effort image cleanup can only emit a warning. Review-state snapshots are
  serialized with reset and older-epoch responses are ignored by the browser.
  Reset also discards active manual-frame recordings without sending them to STT,
  and invalidates any transcription already running in the prior generation.
- **Fixed: the shared review/analyze shell stays usable and accessible at narrow
  widths.** Oversized moment previews are bounded, analyze header controls wrap
  on phones, active tabs keep their roving `tabindex` in sync, the sidebar
  separator reports consistent pixel bounds, and the live Moments counter
  counts every visible logical card (including rejected cards) plus manual
  moments. Switching language now also relocalizes all dynamically rendered
  merged-card labels, controls, provenance, hints, and screenshot text
  alternatives.

## [0.1.18] - 2026-08-08

- **Fixed: `review` opens the report when the video name contains spaces.** The
  report URL is now percent-encoded before the `#token=...` fragment is appended,
  so the default macOS recording name ("Screen Recording … at 14.09.22.mp4") no
  longer produces `{"detail":"Not Found"}`. Previously the raw URL was escaped
  wholesale by the OS handler, turning `#` into `%23` and pushing the session
  token into the request path (and the access log) instead of the fragment.
- **Fixed: concurrent saves and analyses no longer lose work in the review and
  analyze servers.** Two overlapping saves each loaded, merged and rewrote
  `report.json` independently, so the second writer could overwrite a reviewer's
  verdict with a snapshot taken before the first save landed; the whole
  read-modify-write cycle is now serialized per report. Chained VLM/STT calls
  advance the conversation head only when it has not moved since the call
  started, so a slow analysis returning late no longer clobbers the newer
  response id and leaves the next request chaining off a stale one.
- **Fixed: the review report's "Moments" tab counter includes manual moments.**
  The header count was a server-side snapshot of AI findings only, so marking a
  manual moment updated the manual panel while the tab stayed frozen. The count
  now flows through a single live renderer that sums non-rejected AI findings
  and manual moments, and it survives verdict changes and language switches
  instead of being overwritten on every re-render.
- **Fixed: provider API keys are classified by shape and origin.** LibraxisAI
  keys (`sk-vista`, legacy `vista-`) are recognized as such, and any other `sk-`
  key is treated as ambiguous rather than assumed to be OpenAI — OpenAI-compatible
  gateways share that prefix. Only the provably wrong combinations still block a
  run: a LibraxisAI key aimed at `openai.com`, or a key taken from an explicit
  `OPENAI_API_KEY` aimed at a LibraxisAI endpoint. An ambiguous `sk-` key on a
  LibraxisAI endpoint is now a non-blocking warning that points at
  `screenscribe config setup` instead of exiting. The outgoing STT/LLM/VLM
  request shape is covered by new contract tests.
- **Fixed: `--embed-video` explains itself when the video is too large.** Videos
  at or above the 50MB embed limit fall back to linking by filename; that
  fallback is now reported in the report's errors section instead of happening
  silently. The check runs where the real source path is known, so it actually
  fires for saved reports, and the warning reaches the caller's error list
  rather than a discarded copy.

## [0.1.17] - 2026-07-13

- **Changed: adopted the Business Source License 1.1.** The project now ships
  under BUSL-1.1 (SPDX `BUSL-1.1`, converting to Apache-2.0 on the Change Date);
  the full license terms are included in `LICENSE`.
- **Security: bumped Starlette to `>=1.3.1`** to pick up the fixes for
  PYSEC-2026-248 and PYSEC-2026-249.
- **Security: the review server guards `/video` endpoints with a signed session
  token.** Video is served only against a valid per-session token; the
  by-filename endpoint is restored behind the same guard.
- **Security: hardened HTML render and WebVTT output.** Frame MIME handling,
  degraded-i18n slots, and video paths are hardened in the report render, and
  WebVTT cue text is escaped.
- **Fixed: provider configuration is coherent across mixed setups.** Environment
  routing overrides are honored, custom provider base URLs are normalized, mixed
  providers are preserved as a custom config, provider checks are scoped to the
  active stages, `config.env` is read/written as UTF-8, and `--set-key` preserves
  all previously configured values.
- **Fixed: onboarding guidance for first-time and shared-machine users.** The
  wizard walks shared-Mac users through FFmpeg setup and gives safe, coherent
  provider-onboarding guidance without leaking secrets into shell history.
- **Fixed: `make install` / user install flow.** Working tool installs are
  preserved on reinstall and the user install path stays lightweight.
- **Fixed: pipeline reliability under flaky networks and load.** Streaming VLM
  requests retry on 429/5xx honoring `Retry-After`, httpx transport errors are
  treated as retriable, ffmpeg/ffprobe calls are bounded by timeouts with unique
  temp paths, concurrent marker analysis is guarded and finalize jobs are
  bounded, orphaned manual frames are swept on save, and merged-frame thumbnails
  are embedded as base64 so the single-file report promise holds.
- **Fixed: CLI validation and honesty.** Model validation and CLI UX are
  hardened, and the documented CLI flags and key requirements match actual
  behavior.
- **Changed: packaging metadata for the first PyPI publish.** Dropped the
  redundant `License :: Other/Proprietary License` trove classifier — the SPDX
  `license = "BUSL-1.1"` expression is now the single license source (PEP 639).
  Added a `make release-verify` packaging gate that builds the sdist/wheel with
  `uv build --no-sources`, runs `twine check --strict`, inspects the artifact
  contents for the bundled runtime assets, and smoke-installs the wheel in an
  isolated environment.

## [0.1.16] - 2026-07-03

- **Added: operators can set a moment's priority.** In both the analyze
  dashboard and the review flow, a manual moment carries an editable
  priority/severity — set it when marking, change it later — and its note is
  optional and can be edited after capture.
- **Changed: report polish layer.** The review/report viewer gained an operator
  polish layer — the viewfinder wordmark lockup, tuned scrollbars, a footer echo,
  and small-caps case chips — for a more finished, forensic look.
- **Changed: typography and brand refresh.** The type scale is tokenized and
  JetBrains Mono is embedded (rendered offline, no network fetch); logos,
  favicons, the social banner, and the OG image were swapped for the final brand
  assets; the package description and CLI banner were aligned to the canonical
  tagline.
- **Changed: "moment" is the canonical term.** Marked items are consistently
  called "moments" across both the analyze and review modes, replacing the older
  "finding/frame" wording in the UI copy.
- **Fixed: hallucinated speech segments are dropped on more paths.** The
  STT-confidence hallucination filter now also runs on the `transcribe` and
  `preprocess` lanes, so no-speech artifacts are removed there too.
- **Fixed: Markdown export no longer prints a "none" severity.** An explicit
  `none` severity is collapsed instead of being surfaced.
- **Fixed: the analyze session token survives a reload.** It is cached in
  `sessionStorage`, so reloading the dashboard no longer drops the session.
- **Fixed: browser STT runs off the event loop**, so a long transcription no
  longer blocks the review server.
- **Fixed: un-analyzed manual moments show a proper empty state** instead of a
  placeholder that could be persisted as a transcript.
- **Fixed: localized category badges and copy.** Finding-category badges are
  localized instead of leaking the raw English enum, the degraded-summary
  action-items header is localized, and PL copy was naturalized.
- **Fixed: analysis output honors the selected language.** Analysis JSON values
  are forced into the chosen language.
- **Fixed: the reviewer name is optional** and never blocks export; the review
  header reads "review" and renders the executive summary as Markdown.
- **Added: architecture map for devs and agents** under `docs/`, with the e2e
  suite documented in CONTRIBUTING.

## [0.1.15] - 2026-06-27

- **Monochrome report-viewer foundation.** The shared review/report UI is now a
  pure monochrome (grayscale) foundation: saturated accent colors are gone in
  favor of a calmer, forensic look that reads consistently across tabs.
- **Removed: the review Statistics tab.** The Statistics tab and its now-dead
  stat-card filter machinery were dropped; the review flow is Summary →
  Findings → Export.
- **Export actions are gated to the final Export tab.** Export/download controls
  now live on (and enable from) the dedicated Export tab instead of being
  scattered across the shell.
- **Internal: report render hardened against attribute injection.** Finding
  `severity` is clamped to an allowlist before it reaches a class attribute,
  finding ids and timestamps are escaped/float-coerced before entering HTML, and
  the inline `onclick` timestamp seek was replaced with a `data-timestamp`
  attribute plus event delegation (CSP-friendlier, no inline handlers).
- **Fixed: review edits could be lost when the same report was open in two tabs.**
  A background tab syncing its older state over a live edit clobbered fresh work.
  Cross-tab sync now applies an incoming snapshot only when it is strictly newer by
  `savedAt` (last-writer-wins), so a stale tab can no longer overwrite newer edits.
- **Fixed: manual-frame edits made after marking were dropped on reload** and a later
  analyze ran against the stale server copy. Editing a marked frame now persists the
  change to the server (new `PATCH /api/manual-mark/{id}`).
- **Fixed: analyze on a marker reported "Ready" even when it failed.** Analyze now
  surfaces a distinct error state on an HTTP failure or an error payload instead of
  masking it.
- **Fixed: rapid mic press/release, double-marking, and overlapping voice notes.** The
  recorder serializes press/release (one start, one stop), concurrent mark requests
  collapse to a single create, and a finished voice note no longer tears down a newer one.
- **Internal: review server hardened.** Blocking STT and long VLM analysis are offloaded
  off the event loop; `report.json` is written atomically (temp + `os.replace`); browser
  STT now passes the same audio-quality guard as the pipeline; auth/missing-key errors
  fail fast instead of pointless ffmpeg retries; empty response ids no longer break the
  conversation chain.
- **Internal: config + stream correctness.** The vision env flag matches its exact key
  (no `*_VISION` catch-all); streamed analyze extracts the response id before content
  reconciliation and honors the provider's final text.
- **Internal: gate truth + tooling.** The JS runtime canary fails closed under CI when
  `node` is missing (skips locally), the fast canary also loads `video_player.js`, JSZip,
  and `analyze_dashboard.js`, CI provisions node and runs a non-blocking e2e job, and a
  Living Tree race-protected commit helper (`make commit-safe`) is available. JS coverage
  scope and the definition of done are documented under `docs/`.

## [0.1.14] - 2026-06-17

- **Manual frames are now first-class review items.** Captured frames render in a
  readable stacked card (matching the AI finding cards instead of a cramped
  side-by-side column), expose a delete control with confirmation, and show a
  localized severity badge. Voice notes no longer report success when the
  recognizer returned no recognized text.
- **Fixed: review decisions could be resurrected on reload.** Local review state
  lives in two localStorage snapshots — a periodic draft and a live sync — and
  restore always preferred the draft, so a stale draft could bring back a rejected
  finding or a deleted manual frame. Restore now picks the freshest snapshot by
  `savedAt`, with a safe draft-first fallback when timestamps are missing or
  invalid, and parses each snapshot independently so bad local data no longer
  breaks the restore.
- **Fixed: "Export TODO" and "Export ZIP" crashed when any finding was rejected.**
  Building the rejected section referenced an undefined symbol, throwing a
  ReferenceError that took both exports down. The rejected section now renders
  through the normal i18n path.
- **Fixed: reviewer verdicts and notes could silently fail to persist on large
  reports.** Manual-frame images were serialized into the browser's localStorage
  draft, overflowing the ~5 MB quota so the write threw and the decisions were
  lost on reload. Frame pixels now live server-side and are restored from
  `/api/review-state`; the localStorage draft carries decisions only, and a quota
  overflow shows a gentle warning instead of dropping work.
- **Internal: `make verify` now exercises the report's JavaScript.** A node-based
  runtime canary loads `review_app.js` and asserts the core review paths (load,
  verdict click, reviewed export, TODO export) behave — closing the gap where the
  gate could report READY while the report's JS was broken.
- **Honest visual-analysis flag (breaking).** Renamed `--ai` / `--no-ai` to
  `--vision` / `--no-vision` (canonical), with `--no-vlm` as a power-user alias.
  The old `--no-ai` name lied — the semantic LLM detection is AI too. `--no-vision`
  skips only the VLM visual/screenshot reasoning step; semantic LLM detection still
  runs, and screenshots are still extracted as evidence. The config env var
  `SCREENSCRIBE_AI` is likewise renamed to `SCREENSCRIBE_VISION`
  (config field `use_ai_analysis` → `use_vision_analysis`).
- **Keywords are now always-on AI vocabulary hints, not a mode (breaking).**
  Keywords are passed to the AI as hints during detection (they help the model
  read a user's/team's language); they never replace the LLM and never trigger a
  finding on their own. The keyword-only detection mode and the `--keywords-only`
  flag were removed — detection is always the semantic LLM pre-filter. Keywords
  now live in a single global file `~/.config/screenscribe/keywords.yaml` (no more
  current-directory auto-search; analysis no longer depends on your terminal's
  cwd), with `--keywords-file` for a per-run override and a built-in default.
  Six categories (bug, change, ui, performance, accessibility, other). New CLI:
  `screenscribe keywords init | edit | add | list`. Empty/missing/malformed
  dictionaries are safe (warn + fall back, never break the pipeline).
- **Removed the legacy `--no-pro` static HTML report (breaking).** The pro,
  self-contained interactive report (with embedded base64 frames, openable
  offline) is now the only report path; the duplicate 850-line static generator
  was deleted. The `--no-pro` flag is gone.
- **Removed two unreachable HTTP routes.** `POST /api/analyze-all` and
  `GET /api/manual-markers` had no caller (the dashboard uses `/api/finalize`
  and builds markers from `/api/review-state`).
- **One verification gate.** `make verify` (a portable `scripts/ss_verify.py`)
  is now the single source of truth — it prints `RESULT: READY|NOT READY` and
  runs lint, format, types, security, tests + coverage, leak-scan, secrets,
  build, and an effect-level packaged-wheel render smoke. It replaces the old
  `release-check` / `ship-verify` / ZIP-rooted seed-audit scripts; CI runs it.
- **Internal: large modules split** (behavior-preserving). `report.py`,
  `unified_analysis.py`, and `cli.py` were broken into cohesive packages/modules
  behind unchanged public facades.

## 0.1.13 — Shared HTML shell, work-item persistence, honest CLI (2026-06-13)

- **One shared HTML shell.** Both the `review` report and the `analyze` dashboard
  now render through a single `render_surface(config)` skeleton — a new surface is
  a small config, not a new generator. Removes the duplicated, divergent layouts.
- **Single i18n runtime.** Replaced two parallel translation dictionaries with one
  namespaced runtime; fixes the heavy PL/EN language mix in both HTML surfaces, with
  a regression guard that forbids hardcoded UI strings.
- **Human decisions persist.** A unified work-item shape + adapters; accepted/rejected
  findings, severity overrides, notes, annotations and manual frames now survive
  save → reload (no longer lost to a fresh browser load).
- **Add manual frames without forced AI.** In the review report you can capture a
  frame and keep it without immediately running VLM analysis; analysis is now a
  separate, optional action.
- **Honest CLI flags.** Removed the misleading `--no-semantic` / `--no-vision` flags
  (vestigial after the unified pipeline); a single `--ai` / `--no-ai` does exactly
  what it says (`--no-ai` = detection-only, no VLM). `--keywords-only` unchanged.
- **Single design-token source** and shared JS modules (tab keyboard, language
  control, STT transport) consumed by both surfaces.
- **Fixes:** marker-list crash on a failed markers fetch; manual-frame card layout
  (content no longer renders behind the thumbnail); fully redacted API keys in
  `config --show`.
- **Quality:** test coverage raised to ~82% with an enforced floor; CI now runs the
  full release gate (lint, format, tests+coverage, leak-scan, secrets, build).

## 0.1.12 — Initial public release (2026-06-06)

- Initial clean public release of screenscribe.
- CLI for video review, transcription, preprocessing, and interactive analysis.
- Interactive HTML reports enabled by default.
