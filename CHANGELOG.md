# Changelog

## Unreleased

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
