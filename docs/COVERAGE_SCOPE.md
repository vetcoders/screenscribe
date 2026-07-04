# Coverage scope

This declares, explicitly, what the `make verify` quality gate measures and
what it does not — so a reader never mistakes a silent gap for full coverage.

## Python — line coverage, floored

The `tests+coverage` check in `scripts/ss_verify.py` runs
`pytest -m "not integration" --cov=screenscribe --cov-fail-under=80`.

- **Scope:** the `screenscribe` Python package only.
- **Floor:** 80% (a regression ratchet; measured ~82% at the floor's last
  update). Raise the floor as coverage climbs; never lower it to make a gate
  green.
- **Excluded from the floor:** tests marked `integration` (they need external
  API access) and `e2e` (installed wheel + real browser; opt in with
  `--run-e2e`).

## JavaScript — runtime canary, not line coverage

The served report ships JavaScript (`review_app.js`, `video_player.js`,
`analyze_dashboard.js`, and the vendored `jszip.min.js`). That code is **not**
measured by a line-coverage instrument, and that is a deliberate scope choice,
not an oversight:

- A line-coverage tool for the JS (e.g. `nyc` / Istanbul) is **out of scope**.
  Wiring one in is future work, not a gate requirement here.
- Instead, the JS runtime surface has a **load + behaviour canary** in
  `tests/test_f0_js_runtime_smoke.py`, executed by the same `pytest` run the
  Python coverage gate uses:
  - a **deep** canary over `review_app.js` (export shape, verdict handling,
    draft/quota/reload paths, session-token wrapper), and
  - a **fast load-array** over `video_player.js`, `analyze_dashboard.js`, and
    vendored JSZip — each must evaluate its top-level scope without a
    `ReferenceError` or a redeclaration collision and expose its entry points
    (`JSZip` constructible with `generateAsync`; the player class defined).

The canary is the **runtime witness** for the JS: it proves the served scripts
load and the core paths behave, which a static line count cannot. It does not
claim per-line JS coverage, and this document is the place that says so.

## Fail-closed under CI

The JS canary is a hard gate, not a courtesy. When it runs under CI
(`CI=true`) and `node` is missing, it **fails** rather than skipping — so the
verifier can never print READY while the JS runtime is unverified. Locally,
where a developer may not have `node`, the canary skips so `make verify` stays
runnable. CI provisions `node` via `setup-node` (see `.github/workflows/ci.yml`).
