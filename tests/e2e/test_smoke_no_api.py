"""C4.1 — deterministic no-API Playwright smoke for the review + analyze UI.

Why: the browserless READY gate (``not integration`` + the F0 node-vm canary)
never renders a real browser, so a UI regression — a re-introduced Statistics
tab, ungated exports, a Save button that drifts out of the footer, an analyze
surface that no longer opens on Capture — ships green (the A.4 contract
blind-spot). This smoke renders BOTH surfaces straight from the source tree (no
STT/LLM/VLM keys, no network, no ffmpeg), loads them over ``file://`` in the
cached headless Chromium, and asserts the post-PKG-1 / PKG-3 runtime truth of
the DOM.

No-API contract: the review surface is built by
``render_html_report_pro(..., findings=[], segments=[])`` and the analyze surface
by ``render_surface(ANALYZE_SURFACE, context)`` — both pure functions over plain
args, so this smoke never touches the pipeline fixtures (``generated_review`` /
``review_server``) that need ffmpeg + an API key. It reuses only the lightweight
``browser_context`` fixture from ``conftest.py``.

Assertions key on stable ids / ``data-tab`` ids, never on visible tab text: the
labels are i18n-variable (PL/EN), the structure is not.

Surface note (runtime truth over the brief): the two surfaces gate exports
DIFFERENTLY and this smoke asserts each as it actually is.
  * review  (``render_html_report_pro``): the artifact-download buttons
    (``exportReviewedJSON`` / ``exportTodoList`` / ``exportReviewedZIP``) live
    inside ``#tab-export``, a non-active ``tab-content`` on load — gated by tab
    visibility, NOT by a ``disabled`` attribute. Save-to-disk is the only export
    control surfaced outside the Export tab, and it lives in the footer.
  * analyze (``render_surface(ANALYZE_SURFACE)``): the export buttons
    ``#exportJsonBtn`` / ``#finalizeBtn`` carry ``disabled`` on load and an
    ``#exportGateHint`` explains why (no markers yet).

Wiring this smoke as a blocking gate (make verify / CI) is the SEPARATE cut
C4.2; this file only delivers the smoke.
"""

from __future__ import annotations

import html as _html

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.browser, pytest.mark.requires_playwright]


# --- no-API source renders -------------------------------------------------


def _render_review_html() -> str:
    """Self-contained REVIEW report HTML from the source tree (no API/ffmpeg)."""
    from screenscribe.html_pro.renderer import render_html_report_pro

    return render_html_report_pro(
        video_name="smoke-demo.mov",
        video_path=None,
        generated_at="2026-06-27T10:00:00Z",
        executive_summary="Deterministic no-API smoke fixture.",
        findings=[],
        segments=[],
    )


def _render_analyze_html() -> str:
    """ANALYZE dashboard HTML from the source tree (context shape from
    analyze_server.index, no server/API)."""
    from screenscribe.shell import ANALYZE_SURFACE, render_surface

    lang = "en"
    context = {
        "document_language": _html.escape(lang),
        "ui_language": _html.escape(lang),
        "video_name": "smoke-demo.mov",
        "video_name_escaped": _html.escape("smoke-demo.mov"),
        "speech_lang_label": _html.escape(lang.upper()),
        "body_mode": "analyze",
        "body_default_lang": lang,
        "body_speech_lang": lang,
        "body_has_markers": "false",
        "findings_json": "[]",
        "segments_json": "[]",
    }
    return render_surface(ANALYZE_SURFACE, context)


@pytest.fixture(scope="module")
def review_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Render the REVIEW report once and return a ``file://`` URL."""
    path = tmp_path_factory.mktemp("smoke_review") / "review_report.html"
    path.write_text(_render_review_html(), encoding="utf-8")
    return path.as_uri()


@pytest.fixture(scope="module")
def analyze_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Render the ANALYZE dashboard once and return a ``file://`` URL."""
    path = tmp_path_factory.mktemp("smoke_analyze") / "analyze.html"
    path.write_text(_render_analyze_html(), encoding="utf-8")
    return path.as_uri()


def _install_console_capture(page) -> tuple[list, list]:
    """Collect console.error texts + uncaught pageerrors (JS-runtime regression
    guard). Mirrors test_review_browser_runtime._install_console_capture."""
    console_errors: list = []
    page_errors: list = []
    page.on(
        "console",
        lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
    )
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    return console_errors, page_errors


# --- REVIEW surface --------------------------------------------------------


def test_review_tabs_present(review_url, browser_context) -> None:
    """A3: exactly the summary / findings / export tabs are present (by id)."""
    page = browser_context.new_page()
    page.goto(review_url, wait_until="load")
    for tab in ("summary", "findings", "export"):
        assert page.locator(f'.tab-btn[data-tab="{tab}"]').count() == 1, (
            f"review tab button '{tab}' missing"
        )
        assert page.locator(f"#tab-{tab}").count() == 1, f"review tab panel '{tab}' missing"
    page.close()


def test_review_has_no_statistics_tab(review_url, browser_context) -> None:
    """A4: the removed Statistics tab must not come back (PKG-3 monochrome)."""
    page = browser_context.new_page()
    page.goto(review_url, wait_until="load")
    assert page.locator('[data-tab="statistics"]').count() == 0, "Statistics tab re-introduced"
    assert page.locator("#tab-statistics").count() == 0, "Statistics panel re-introduced"
    assert "Statistics" not in page.content(), "literal 'Statistics' present in review DOM"
    page.close()


def test_review_exports_gated_to_export_tab(review_url, browser_context) -> None:
    """A5 (review gating = tab visibility, not a disabled attr): the artifact
    downloads live inside #tab-export, which is NOT the active tab on load, while
    Summary IS active. So the export buttons are not interactive until the user
    opens the Export tab."""
    page = browser_context.new_page()
    page.goto(review_url, wait_until="load")

    # Summary is the default-active tab; Export is gated (hidden) on load.
    assert page.locator("#tab-summary.active").count() == 1, "Summary tab not active on load"
    assert page.locator("#tab-export.active").count() == 0, "Export tab unexpectedly active on load"
    assert not page.locator("#tab-export").is_visible(), (
        "Export panel visible before its tab is opened"
    )

    # The three artifact-download buttons live inside the (gated) Export tab.
    # Post-C7.2 they are wired via data-action + event delegation, not inline onclick.
    for action in ("export-todo", "export-json", "export-zip"):
        assert page.locator(f'#tab-export button[data-action="{action}"]').count() == 1, (
            f"review export button '{action}' not inside #tab-export"
        )
    page.close()


def test_review_save_in_footer(review_url, browser_context) -> None:
    """A6: Save-to-disk (review-state persistence) sits in the sidebar footer,
    deliberately outside the Export tab."""
    page = browser_context.new_page()
    page.goto(review_url, wait_until="load")
    assert page.locator('.sidebar-footer button[data-action="save-review"]').count() == 1, (
        "save-review button not in .sidebar-footer"
    )
    page.close()


def test_review_tab_switching_works(review_url, browser_context) -> None:
    """Basic tab switching: clicking the Export tab button activates #tab-export
    and deactivates #tab-summary (initTabs -> activateTab toggles .active)."""
    page = browser_context.new_page()
    page.goto(review_url, wait_until="load")

    assert page.locator("#tab-summary.active").count() == 1
    page.locator('.tab-btn[data-tab="export"]').click()
    assert page.locator("#tab-export.active").count() == 1, "Export tab did not activate on click"
    assert page.locator("#tab-summary.active").count() == 0, "Summary stayed active after switch"
    assert page.locator("#tab-export").is_visible(), (
        "Export panel not visible after switching to it"
    )
    page.close()


# --- ANALYZE surface -------------------------------------------------------


def test_analyze_capture_is_default_tab(analyze_url, browser_context) -> None:
    """A8: the analyze dashboard opens on the Capture tab."""
    page = browser_context.new_page()
    page.goto(analyze_url, wait_until="load")
    assert page.locator("#tab-capture.tab-content.active").count() == 1, (
        "analyze Capture tab not default-active"
    )
    assert page.locator('[data-tab="statistics"]').count() == 0, "analyze has a Statistics tab"
    page.close()


def test_analyze_exports_disabled_with_hint(analyze_url, browser_context) -> None:
    """A9: with no markers, the analyze export buttons are disabled on load and
    an export-gate hint explains why."""
    page = browser_context.new_page()
    page.goto(analyze_url, wait_until="load")
    assert page.locator("#exportJsonBtn").is_disabled(), "#exportJsonBtn not disabled on load"
    assert page.locator("#finalizeBtn").is_disabled(), "#finalizeBtn not disabled on load"
    assert page.locator("#exportGateHint").count() == 1, "#exportGateHint missing"
    page.close()


# --- JS-runtime regression guard (both surfaces) ---------------------------


# Console-error allowlist (substring match). WHY: both surfaces are normally
# served over http where the app fetches live state and media. Under the no-API
# `file://` smoke there is no backend, so the browser rejects:
#   * review_app's `/api/review-state` poll — "file scheme not supported".
#   * the analyze `<video src="/video">` element — net::ERR_FILE_NOT_FOUND
#     (the live /video endpoint is absent offline).
# Both are artifacts of the deterministic offline harness, NOT JS-runtime
# regressions — a real bug surfaces as an uncaught pageerror (always asserted
# empty, never allowlisted) or as a DIFFERENT console error.
_CONSOLE_ERROR_ALLOWLIST = (
    'URL scheme "file" is not supported',
    "Fetch API cannot load file:",
    "net::ERR_FILE_NOT_FOUND",
)


def _unexpected(errors: list) -> list:
    return [e for e in errors if not any(allowed in e for allowed in _CONSOLE_ERROR_ALLOWLIST)]


def test_no_console_or_page_errors_on_load(review_url, analyze_url, browser_context) -> None:
    """A10: loading either surface produces no uncaught pageerror and no console
    error beyond the documented file:// fetch artifact (no-JS-runtime-regression).
    Both surfaces are self-contained (no external <script src>)."""
    for label, url in (("review", review_url), ("analyze", analyze_url)):
        page = browser_context.new_page()
        console_errors, page_errors = _install_console_capture(page)
        page.goto(url, wait_until="load")
        # Give late microtasks (i18n apply, init) a beat to surface any throw.
        page.wait_for_timeout(250)
        assert not page_errors, f"{label}: uncaught page errors on load: {page_errors}"
        unexpected = _unexpected(console_errors)
        assert not unexpected, f"{label}: unexpected console errors on load: {unexpected}"
        page.close()


def test_analyze_frame_modal_closes_on_x_via_delegation(analyze_url, browser_context) -> None:
    """C7.2b: the frame-modal close X is wired by event delegation, NOT an inline
    onclick. Static grep proves the attribute is gone; this proves the runtime
    behavior survives the migration — activating the modal then clicking the X
    must actually close it (delegated handler -> closeFrameModal)."""
    page = browser_context.new_page()
    page.goto(analyze_url, wait_until="load")

    modal = page.locator("#frameModal")
    assert modal.count() == 1, "frame modal partial missing from analyze surface"
    # CSP-readiness: the close button carries no inline onclick attribute.
    assert page.locator(".frame-modal-close[onclick]").count() == 0, (
        "frame-modal close still carries an inline onclick"
    )

    # Activate the modal as openFrameModal would, then click the visible X.
    page.evaluate("document.getElementById('frameModal').classList.add('active')")
    assert "active" in (modal.get_attribute("class") or ""), "modal failed to activate"
    page.locator(".frame-modal-close").click()
    assert "active" not in (modal.get_attribute("class") or ""), (
        "clicking the X did not close the modal — delegated close is broken"
    )
    page.close()
