"""End-to-end tests for the screenscribe review app.

These tests exercise the INSTALLED artifact (a wheel built from current HEAD,
installed into an isolated venv outside the repo) plus the real generated HTML
report driven by a real Chromium via Playwright. They are the regression net for
runtime bugs that unit / node-vm tests cannot see (token-reload 403, base64
frame stripping on export, wholesale-replace wiping manual-frame images on a
cross-window storage sync).

Skipped by default; opt in with ``--run-e2e`` (mirrors ``--run-integration``).
"""
