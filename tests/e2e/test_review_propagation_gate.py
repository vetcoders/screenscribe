"""Layer 1 — propagation gate (no browser).

Proves the runtime fixes from this session actually live in the INSTALLED wheel
and in a FRESHLY generated report, not just in the repo source. This is the
cheap, fast guard against the cwd-shadow trap (a stale install masked by the repo
cwd) and against the HTML-inlining trap (old reports never get fixes; only newly
generated reports do).

Markers asserted (one per fix):
- ``screenscribe:token:``                  -> e79e515 (token cached per-tab in sessionStorage)
- ``mergeManualFrameImages``               -> ae3db9e (merge-preserve frameDataUrl on hydrate)
- ``frameDataUrl, ...rest }) => rest``      -> de41ef1 (export strips base64 for the light JSON)
"""

from __future__ import annotations

from pathlib import Path

import pytest

MARKERS = (
    "screenscribe:token:",
    "mergeManualFrameImages",
    "frameDataUrl, ...rest }) => rest",
)

pytestmark = pytest.mark.e2e


def test_installed_wheel_js_carries_fix_markers(installed_cli) -> None:
    """The JS shipped INSIDE the installed wheel must carry every fix marker."""
    js_path: Path = installed_cli.review_js
    assert js_path.exists(), f"installed review_app.js missing: {js_path}"
    js = js_path.read_text(encoding="utf-8")
    for marker in MARKERS:
        assert marker in js, f"installed wheel JS missing fix marker: {marker!r} ({js_path})"


def test_generated_report_html_inlines_fix_markers(generated_review) -> None:
    """A freshly generated report (HTML inlines the JS at generation time) must
    carry every fix marker — proves the fix reaches what the user actually opens."""
    html_files = list(generated_review.glob("*_report.html"))
    assert html_files, f"no generated report HTML under {generated_review}"
    html = html_files[0].read_text(encoding="utf-8")
    for marker in MARKERS:
        assert marker in html, f"generated report HTML missing fix marker: {marker!r}"


def test_installed_package_is_not_repo_source(installed_cli) -> None:
    """Defensive: the binary we drive resolves to the isolated venv, not the repo
    source tree — otherwise the whole gate would be testing the wrong artifact."""
    assert installed_cli.venv_dir in installed_cli.site_packages_pkg.parents
